# pyright: reportOptionalMemberAccess=false, reportArgumentType=false
from flask import session

from helpers import (
    AUTH_SESSION_KEY,
    IDENTITY_KIND_TOKEN_ADMIN,
    IDENTITY_KIND_USER,
    clear_auth_session,
    current_identity,
    current_user,
    establish_user_session,
)
from models import USER_AUTH_SOURCE_PROXY, USER_ROLE_ADMIN, USER_ROLE_USER
from smoke_support import ActivityEvent, SmokeBaseTest, User, db


class AuthIdentityTests(SmokeBaseTest):
    def _add_local_user(self, username, *, role=USER_ROLE_USER):
        with self.app.app_context():
            user = User(username=username, role=role)
            user.set_password("correct horse battery staple")
            db.session.add(user)
            db.session.commit()
            return user.id, user.session_version

    def _set_named_session(self, user_id, session_version, *, csrf=False):
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": session_version,
            }
            if csrf:
                session["csrf_token"] = "test-csrf-token"

    def test_named_user_role_is_loaded_from_database(self):
        user_id, session_version = self._add_local_user(
            "named-admin", role=USER_ROLE_ADMIN
        )
        self._set_named_session(user_id, session_version)

        response = self.client.get("/admin/diagnostics", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            payload = session[AUTH_SESSION_KEY]
            self.assertNotIn("role", payload)
            self.assertNotIn("username", payload)

    def test_named_normal_user_can_access_private_but_not_admin_pages(self):
        user_id, session_version = self._add_local_user("named-user")
        self._set_named_session(user_id, session_version)

        private_response = self.client.get("/people", follow_redirects=False)
        admin_response = self.client.get("/admin/diagnostics", follow_redirects=False)

        self.assertEqual(private_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 302)
        self.assertEqual(admin_response.headers["Location"], "/")

    def test_role_change_takes_effect_without_reissuing_session(self):
        user_id, session_version = self._add_local_user(
            "demoted-admin", role=USER_ROLE_ADMIN
        )
        self._add_local_user("remaining-admin", role=USER_ROLE_ADMIN)
        self._set_named_session(user_id, session_version)

        self.assertEqual(self.client.get("/admin/diagnostics").status_code, 200)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            user.role = USER_ROLE_USER
            db.session.commit()

        admin_response = self.client.get("/admin/diagnostics", follow_redirects=False)
        private_response = self.client.get("/people", follow_redirects=False)
        self.assertEqual(admin_response.status_code, 302)
        self.assertEqual(private_response.status_code, 200)

    def test_inactive_user_session_is_rejected_and_cleared(self):
        user_id, session_version = self._add_local_user("inactive-user")
        self._set_named_session(user_id, session_version)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            user.is_active = False
            db.session.commit()

        response = self.client.get("/people", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_revoked_session_version_is_rejected_and_cleared(self):
        user_id, session_version = self._add_local_user("revoked-user")
        self._set_named_session(user_id, session_version)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            user.revoke_sessions()
            db.session.commit()

        response = self.client.get("/people", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_malformed_named_session_is_rejected_and_cleared(self):
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": "not-an-integer",
                "session_version": 1,
            }

        response = self.client.get("/people", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_non_mapping_session_payload_is_rejected_and_cleared(self):
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = "unexpected-payload"

        response = self.client.get("/people", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_legacy_admin_cookie_is_migrated_to_identity_payload(self):
        with self.client.session_transaction() as session:
            session["is_admin"] = True

        response = self.client.get("/admin/diagnostics", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(
                session[AUTH_SESSION_KEY],
                {"kind": IDENTITY_KIND_TOKEN_ADMIN},
            )
            self.assertNotIn("is_admin", session)

    def test_proxy_request_resolves_username_and_current_role(self):
        with self.app.app_context():
            user = User(
                username="proxy-admin",
                role=USER_ROLE_ADMIN,
                auth_source=USER_AUTH_SOURCE_PROXY,
                external_subject="proxy-admin",
            )
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        with self.app.test_request_context(headers={"X-Forwarded-User": "proxy-admin"}):
            identity = current_identity()

        self.assertIsNotNone(identity)
        self.assertEqual(identity.username, "proxy-admin")
        self.assertEqual(identity.role, USER_ROLE_ADMIN)
        self.assertEqual(identity.source, "proxy")
        self.assertEqual(identity.user_id, user_id)

    def test_named_user_session_helper_and_clearer(self):
        user_id, _session_version = self._add_local_user("helper-user")
        with self.app.test_request_context():
            user = db.session.get(User, user_id)
            establish_user_session(user)

            identity = current_identity()
            self.assertEqual(identity.username, "helper-user")
            self.assertEqual(current_user(), user)
            self.assertNotIn("role", session_payload := dict(session[AUTH_SESSION_KEY]))
            self.assertEqual(session_payload["kind"], IDENTITY_KIND_USER)

            clear_auth_session()
            self.assertIsNone(current_identity())
            self.assertIsNone(current_user())

    def test_named_user_is_attached_to_automatic_audit_events(self):
        user_id, session_version = self._add_local_user(
            "audit-user", role=USER_ROLE_ADMIN
        )
        self._set_named_session(user_id, session_version, csrf=True)

        response = self.client.post(
            "/people/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Identity Audit Collector",
                "notes": "Created by a named user",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.entity_name == "Identity Audit Collector"
                )
            ).scalar_one()
            self.assertEqual(event.actor, "audit-user")
            self.assertEqual(event.actor_user_id, user_id)
