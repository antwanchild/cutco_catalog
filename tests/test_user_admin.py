# pyright: reportOptionalMemberAccess=false

import json
from unittest import mock

from helpers import AUTH_SESSION_KEY, IDENTITY_KIND_USER
from models import (
    ActivityEvent,
    USER_AUTH_SOURCE_PROXY,
    USER_ROLE_ADMIN,
    USER_ROLE_USER,
    User,
)
from smoke_support import SmokeBaseTest, db


class UserAdminTests(SmokeBaseTest):
    def _add_user(
        self,
        username,
        *,
        role=USER_ROLE_USER,
        active=True,
        auth_source="local",
        password="correct horse battery staple",
    ):
        with self.app.app_context():
            user = User(
                username=username,
                role=role,
                is_active=active,
                auth_source=auth_source,
                external_subject=(
                    f"subject:{username}"
                    if auth_source == USER_AUTH_SOURCE_PROXY
                    else None
                ),
            )
            if auth_source != USER_AUTH_SOURCE_PROXY:
                user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return user.id, user.session_version

    def _set_named_session(self, user_id, session_version, *, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": session_version,
            }
            session["csrf_token"] = "test-csrf-token"

    def _login_admin(self, username="managing-admin"):
        user_id, session_version = self._add_user(
            username,
            role=USER_ROLE_ADMIN,
        )
        self._set_named_session(user_id, session_version)
        return user_id

    def test_user_management_requires_admin_and_csrf(self):
        anonymous = self.client.get("/admin/users", follow_redirects=False)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/admin/login", anonymous.headers["Location"])

        normal_id, normal_version = self._add_user("normal-user")
        self._set_named_session(normal_id, normal_version)
        denied = self.client.get("/admin/users", follow_redirects=False)
        self.assertEqual(denied.status_code, 302)

        self.client = self.app.test_client()
        self._login_admin()
        missing_csrf = self.client.post(
            "/admin/users/new",
            data={
                "username": "blocked-create",
                "role": USER_ROLE_USER,
                "password": "temporary password value",
                "password_confirm": "temporary password value",
            },
        )
        self.assertEqual(missing_csrf.status_code, 403)

    def test_admin_can_list_and_open_user_forms(self):
        self._login_admin()

        listing = self.client.get("/admin/users")
        create_form = self.client.get("/admin/users/new")

        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"Named Accounts", listing.data)
        self.assertIn(b"managing-admin", listing.data)
        self.assertEqual(create_form.status_code, 200)
        self.assertIn(b"Temporary password", create_form.data)

    def test_security_mutations_are_post_only(self):
        self._login_admin()
        target_id, _ = self._add_user("post-only-user")

        for action in (
            "activate",
            "deactivate",
            "reset-password",
            "revoke-sessions",
        ):
            with self.subTest(action=action):
                response = self.client.get(f"/admin/users/{target_id}/{action}")
                self.assertEqual(response.status_code, 405)

    def test_admin_creates_temporary_local_account_and_audit_event(self):
        actor_id = self._login_admin()
        password = "temporary password value"

        response = self.client.post(
            "/admin/users/new",
            data={
                "csrf_token": "test-csrf-token",
                "username": " New.User ",
                "display_name": "New User",
                "role": USER_ROLE_USER,
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/users/", response.headers["Location"])
        with self.app.app_context():
            user = db.session.execute(
                db.select(User).where(User.username == "new.user")
            ).scalar_one()
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Created account through user administration"
                )
            ).scalar_one()
            self.assertEqual(user.display_name, "New User")
            self.assertTrue(user.is_active)
            self.assertTrue(user.must_change_password)
            self.assertTrue(user.check_password(password))
            self.assertEqual(event.actor_user_id, actor_id)
            self.assertNotIn(password, event.payload or "")
            self.assertNotIn("password_hash", event.payload or "")

    def test_editing_role_revokes_target_sessions_and_is_audited(self):
        self._login_admin()
        target_id, original_version = self._add_user("promoted-user")

        response = self.client.post(
            f"/admin/users/{target_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "display_name": "Promoted User",
                "role": USER_ROLE_ADMIN,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(User, target_id)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Updated account through user administration"
                )
            ).scalar_one()
            self.assertEqual(user.role, USER_ROLE_ADMIN)
            self.assertEqual(user.display_name, "Promoted User")
            self.assertEqual(user.session_version, original_version + 1)
            payload = json.loads(event.payload)
            self.assertTrue(payload["sessions_revoked"])
            self.assertEqual(payload["changes"]["role"]["before"], USER_ROLE_USER)

    def test_named_admin_cannot_demote_or_deactivate_self(self):
        actor_id = self._login_admin()
        self._add_user("other-admin", role=USER_ROLE_ADMIN)

        demote = self.client.post(
            f"/admin/users/{actor_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "display_name": "Still Admin",
                "role": USER_ROLE_USER,
            },
            follow_redirects=True,
        )
        deactivate = self.client.post(
            f"/admin/users/{actor_id}/deactivate",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=True,
        )

        self.assertIn(b"cannot demote or deactivate your own account", demote.data)
        self.assertIn(b"cannot demote or deactivate your own account", deactivate.data)
        with self.app.app_context():
            user = db.session.get(User, actor_id)
            self.assertEqual(user.role, USER_ROLE_ADMIN)
            self.assertTrue(user.is_active)

    def test_domain_method_rejects_named_admin_self_lockout(self):
        actor_id = self._login_admin()
        self._add_user("other-admin", role=USER_ROLE_ADMIN)

        with self.app.app_context():
            user = db.session.get(User, actor_id)
            with self.assertRaisesRegex(ValueError, "cannot demote or deactivate"):
                user.update_access(role=USER_ROLE_USER, actor_user_id=actor_id)
            with self.assertRaisesRegex(ValueError, "cannot demote or deactivate"):
                user.update_access(is_active=False, actor_user_id=actor_id)

    def test_last_active_admin_invariant_blocks_proxy_admin_deactivation(self):
        target_id, _ = self._add_user("only-local-admin", role=USER_ROLE_ADMIN)
        self._set_csrf_token()
        headers = {
            "X-Forwarded-User": "proxy-admin",
            "X-Forwarded-Groups": "admins",
        }

        with mock.patch("helpers.TRUSTED_AUTH_ADMIN_GROUPS", ("admins",)):
            response = self.client.post(
                f"/admin/users/{target_id}/deactivate",
                data={"csrf_token": "test-csrf-token"},
                headers=headers,
                follow_redirects=True,
            )

        self.assertIn(b"last active admin", response.data)
        with self.app.app_context():
            self.assertTrue(db.session.get(User, target_id).is_active)

    def test_deactivate_and_activate_revoke_target_sessions_and_audit(self):
        self._login_admin()
        target_id, original_version = self._add_user("status-user")

        deactivated = self.client.post(
            f"/admin/users/{target_id}/deactivate",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        activated = self.client.post(
            f"/admin/users/{target_id}/activate",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(deactivated.status_code, 302)
        self.assertEqual(activated.status_code, 302)
        with self.app.app_context():
            user = db.session.get(User, target_id)
            self.assertTrue(user.is_active)
            self.assertEqual(user.session_version, original_version + 2)
            titles = set(
                db.session.scalars(
                    db.select(ActivityEvent.title).where(
                        ActivityEvent.entity_id == target_id,
                        ActivityEvent.entity_type == "User",
                    )
                ).all()
            )
            self.assertIn("Deactivated account through user administration", titles)
            self.assertIn("Activated account through user administration", titles)

    def test_password_reset_forces_change_and_invalidates_target_session(self):
        self._login_admin()
        target_id, original_version = self._add_user("reset-user")
        target_client = self.app.test_client()
        self._set_named_session(target_id, original_version, client=target_client)
        password = "temporary replacement password"

        response = self.client.post(
            f"/admin/users/{target_id}/reset-password",
            data={
                "csrf_token": "test-csrf-token",
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(User, target_id)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title
                    == "Reset account password through user administration"
                )
            ).scalar_one()
            self.assertTrue(user.check_password(password))
            self.assertTrue(user.must_change_password)
            self.assertEqual(user.session_version, original_version + 1)
            self.assertNotIn(password, event.payload or "")
            self.assertNotIn("password_hash", event.payload or "")
        stale = target_client.get("/people", follow_redirects=False)
        self.assertEqual(stale.status_code, 302)

    def test_self_and_proxy_password_resets_are_rejected(self):
        actor_id = self._login_admin()
        proxy_id, proxy_version = self._add_user(
            "proxy-user",
            auth_source=USER_AUTH_SOURCE_PROXY,
        )

        own_reset = self.client.post(
            f"/admin/users/{actor_id}/reset-password",
            data={
                "csrf_token": "test-csrf-token",
                "password": "temporary replacement password",
                "password_confirm": "temporary replacement password",
            },
            follow_redirects=True,
        )
        proxy_reset = self.client.post(
            f"/admin/users/{proxy_id}/reset-password",
            data={
                "csrf_token": "test-csrf-token",
                "password": "temporary replacement password",
                "password_confirm": "temporary replacement password",
            },
            follow_redirects=True,
        )

        self.assertIn(b"Use Change Password", own_reset.data)
        self.assertIn(b"identity provider", proxy_reset.data)
        with self.app.app_context():
            proxy_user = db.session.get(User, proxy_id)
            self.assertEqual(proxy_user.session_version, proxy_version)
            self.assertIsNone(proxy_user.password_hash)

    def test_revoke_sessions_invalidates_target_but_rejects_self(self):
        actor_id = self._login_admin()
        target_id, target_version = self._add_user("session-user")
        target_client = self.app.test_client()
        self._set_named_session(target_id, target_version, client=target_client)

        revoked = self.client.post(
            f"/admin/users/{target_id}/revoke-sessions",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        own = self.client.post(
            f"/admin/users/{actor_id}/revoke-sessions",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=True,
        )

        self.assertEqual(revoked.status_code, 302)
        self.assertIn(b"cannot revoke your current account", own.data)
        self.assertEqual(target_client.get("/people").status_code, 302)
        with self.app.app_context():
            target = db.session.get(User, target_id)
            actor = db.session.get(User, actor_id)
            self.assertEqual(target.session_version, target_version + 1)
            self.assertEqual(actor.session_version, 1)
