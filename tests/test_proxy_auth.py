# pyright: reportOptionalMemberAccess=false

import tempfile
from unittest import mock

import app as app_module
from app import _teardown_logging, create_app

from helpers import AUTH_SESSION_KEY, IDENTITY_KIND_PROXY_ADMIN, IDENTITY_KIND_USER
from models import (
    ActivityEvent,
    USER_AUTH_SOURCE_LOCAL,
    USER_AUTH_SOURCE_PROXY,
    USER_ROLE_ADMIN,
    USER_ROLE_USER,
    User,
)
from smoke_support import SmokeBaseTest, db


class ProxyAuthTests(SmokeBaseTest):
    def _configure(self, **values):
        self.app.config.update(values)

    def _add_user(
        self,
        username,
        *,
        role=USER_ROLE_USER,
        source=USER_AUTH_SOURCE_PROXY,
        subject=None,
        active=True,
    ):
        with self.app.app_context():
            user = User(
                username=username,
                role=role,
                auth_source=source,
                external_subject=subject,
                is_active=active,
            )
            if source == USER_AUTH_SOURCE_LOCAL:
                user.set_password("correct horse battery staple")
            db.session.add(user)
            db.session.commit()
            return user.id, user.session_version

    def _headers(self, username="proxy-user", subject=None, groups=None):
        headers = {"X-Forwarded-User": username}
        if subject is not None:
            headers["X-Subject"] = subject
        if groups is not None:
            headers["X-Forwarded-Groups"] = groups
        return headers

    def test_local_mode_ignores_forged_proxy_headers(self):
        self._configure(AUTH_MODE="local")

        response = self.client.get(
            "/people", headers=self._headers(), follow_redirects=False
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.scalar(db.select(db.func.count(User.id))), 0)

    def test_preprovisioned_proxy_identity_resolves_by_stable_subject(self):
        user_id, _ = self._add_user(
            "stored-name", role=USER_ROLE_ADMIN, subject="stable-123"
        )
        self._configure(TRUSTED_AUTH_SUBJECT_HEADER="X-Subject")

        response = self.client.get(
            "/admin/diagnostics",
            headers=self._headers(username="renamed-upstream", subject="stable-123"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Diagnostics", response.data)
        self.assertIn(b'Sign out of Cutco', response.data)
        self.assertIn(b'href="/outpost.goauthentik.io/sign_out"', response.data)
        self.assertNotIn(b"Session managed by proxy", response.data)
        with self.app.test_request_context(
            headers=self._headers(username="renamed-upstream", subject="stable-123")
        ):
            from helpers import current_identity

            identity = current_identity()
            self.assertEqual(identity.user_id, user_id)
            self.assertEqual(identity.username, "stored-name")
            self.assertEqual(identity.source, "proxy")

    def test_unprovisioned_identity_is_rejected_with_actionable_message(self):
        self._configure(TRUSTED_AUTH_SUBJECT_HEADER="X-Subject")

        response = self.client.get(
            "/people",
            headers=self._headers(subject="unknown-subject"),
            follow_redirects=True,
        )

        self.assertIn(b"not provisioned", response.data)
        self.assertIn(b"create or link", response.data)

    def test_missing_stable_subject_is_rejected(self):
        self._configure(TRUSTED_AUTH_SUBJECT_HEADER="X-Subject")

        response = self.client.get(
            "/people", headers=self._headers(), follow_redirects=True
        )

        self.assertIn(b"did not provide a stable subject", response.data)

    def test_auto_provision_starts_as_user_even_with_admin_group(self):
        self._configure(
            TRUSTED_AUTH_SUBJECT_HEADER="X-Subject",
            PROXY_AUTH_AUTO_PROVISION=True,
            TRUSTED_AUTH_ADMIN_GROUPS=("admins",),
            TRUSTED_AUTH_SYNC_ADMIN_ROLE=True,
        )

        response = self.client.get(
            "/people",
            headers=self._headers(subject="new-subject", groups="admins"),
        )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            user = db.session.execute(
                db.select(User).where(User.external_subject == "new-subject")
            ).scalar_one()
            self.assertEqual(user.role, USER_ROLE_USER)
            self.assertEqual(user.auth_source, USER_AUTH_SOURCE_PROXY)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Auto-provisioned proxy account"
                )
            ).scalar_one()
            self.assertNotIn("new-subject", event.payload or "")

    def test_username_collision_never_silently_merges_accounts(self):
        self._add_user("same-name", source=USER_AUTH_SOURCE_LOCAL)
        self._configure(
            TRUSTED_AUTH_SUBJECT_HEADER="X-Subject",
            PROXY_AUTH_AUTO_PROVISION=True,
        )

        response = self.client.get(
            "/people",
            headers=self._headers(username="same-name", subject="other-subject"),
            follow_redirects=True,
        )

        self.assertIn(b"must link", response.data)
        with self.app.app_context():
            self.assertEqual(db.session.scalar(db.select(db.func.count(User.id))), 1)

    def test_explicit_local_link_supports_proxy_and_local_fallback(self):
        user_id, version = self._add_user("hybrid-user", source=USER_AUTH_SOURCE_LOCAL)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            user.link_proxy_subject("linked-subject")
            db.session.commit()
        self._configure(TRUSTED_AUTH_SUBJECT_HEADER="X-Subject")

        proxy_response = self.client.get(
            "/people",
            headers=self._headers(username="upstream-name", subject="linked-subject"),
        )
        self.assertEqual(proxy_response.status_code, 200)

        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": version,
            }
        local_response = self.client.get(
            "/people",
            headers=self._headers(username="unrelated", subject="unrelated"),
        )
        self.assertEqual(local_response.status_code, 200)

    def test_role_sync_is_opt_in_and_audited(self):
        user_id, original_version = self._add_user(
            "proxy-admin", subject="role-subject"
        )
        self._configure(
            TRUSTED_AUTH_SUBJECT_HEADER="X-Subject",
            TRUSTED_AUTH_ADMIN_GROUPS=("admins",),
        )
        headers = self._headers(
            username="proxy-admin", subject="role-subject", groups="admins"
        )

        denied = self.client.get(
            "/admin/diagnostics", headers=headers, follow_redirects=False
        )
        self.assertEqual(denied.status_code, 302)

        self._configure(TRUSTED_AUTH_SYNC_ADMIN_ROLE=True)
        allowed = self.client.get("/admin/diagnostics", headers=headers)
        self.assertEqual(allowed.status_code, 200)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            self.assertEqual(user.role, USER_ROLE_ADMIN)
            self.assertEqual(user.session_version, original_version + 1)
            self.assertIsNotNone(
                db.session.execute(
                    db.select(ActivityEvent).where(
                        ActivityEvent.title == "Synchronized proxy account role"
                    )
                ).scalar_one_or_none()
            )

        self._add_user(
            "remaining-admin",
            role=USER_ROLE_ADMIN,
            source=USER_AUTH_SOURCE_LOCAL,
        )
        demoted = self.client.get(
            "/admin/diagnostics",
            headers=self._headers(
                username="proxy-admin", subject="role-subject", groups="users"
            ),
            follow_redirects=False,
        )
        self.assertEqual(demoted.status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.get(User, user_id).role, USER_ROLE_USER)

    def test_inactive_proxy_account_is_rejected(self):
        self._add_user("inactive-proxy", subject="inactive-subject", active=False)
        self._configure(TRUSTED_AUTH_SUBJECT_HEADER="X-Subject")

        response = self.client.get(
            "/people",
            headers=self._headers(subject="inactive-subject"),
            follow_redirects=True,
        )

        self.assertIn(b"proxy account is inactive", response.data)

    def test_proxy_mode_disables_local_sessions_and_setup(self):
        user_id, version = self._add_user("local-user", source=USER_AUTH_SOURCE_LOCAL)
        self._configure(AUTH_MODE="proxy")
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": version,
            }

        private = self.client.get("/people", follow_redirects=False)
        setup = self.client.get("/setup", follow_redirects=False)

        self.assertEqual(private.status_code, 302)
        self.assertEqual(setup.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_legacy_proxy_admin_cookie_is_rejected(self):
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_PROXY_ADMIN,
                "username": "old-proxy-admin",
            }

        response = self.client.get("/admin/diagnostics", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_startup_rejects_invalid_proxy_configuration(self):
        common = {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
        with self.assertRaisesRegex(RuntimeError, "Unsupported AUTH_MODE"):
            create_app({**common, "AUTH_MODE": "anything"})
        with self.assertRaisesRegex(RuntimeError, "SUBJECT_HEADER cannot be empty"):
            create_app(
                {
                    **common,
                    "AUTH_MODE": "proxy",
                    "TRUSTED_AUTH_SUBJECT_HEADER": "",
                }
            )

    def test_production_proxy_mode_logs_trusted_header_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(app_module, "ADMIN_TOKEN", "strong-token"):
                with mock.patch.object(app_module.logger, "warning") as warning:
                    app = create_app(
                        {
                            "TESTING": False,
                            "SECRET_KEY": "strong-production-secret",
                            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{temp_dir}/test.db",
                            "LOG_DIR": temp_dir,
                            "ATTACHMENTS_DIR": f"{temp_dir}/uploads/items",
                            "AUTH_MODE": "proxy",
                        }
                    )
            try:
                self.assertTrue(
                    any(
                        "strips client-supplied" in str(call)
                        for call in warning.call_args_list
                    )
                )
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()
                _teardown_logging(temp_dir)
