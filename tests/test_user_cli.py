# pyright: reportOptionalMemberAccess=false

import json

from helpers import AUTH_SESSION_KEY, IDENTITY_KIND_USER
from models import (
    ActivityEvent,
    AuthSetupState,
    USER_AUTH_SOURCE_PROXY,
    USER_ROLE_ADMIN,
    User,
)
from smoke_support import SmokeBaseTest, db


class UserCliTests(SmokeBaseTest):
    def _invoke_with_password(self, args, password="correct horse battery staple"):
        return self.app.test_cli_runner().invoke(
            args=args,
            input=f"{password}\n{password}\n",
        )

    def _add_local_user(
        self,
        username,
        *,
        role="user",
        active=True,
        password="existing secure password",
    ):
        with self.app.app_context():
            user = User(username=username, role=role, is_active=active)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return user.id, user.session_version

    def test_users_group_exposes_recovery_commands(self):
        result = self.app.test_cli_runner().invoke(args=["users", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        for command in (
            "list",
            "create-admin",
            "create-proxy",
            "reset-password",
            "activate",
            "revoke-sessions",
        ):
            self.assertIn(command, result.output)

    def test_create_admin_securely_completes_initial_setup_and_audits(self):
        password = "correct horse battery staple"
        result = self._invoke_with_password(
            [
                "users",
                "create-admin",
                "--username",
                " First.Admin ",
                "--display-name",
                "First Administrator",
            ],
            password,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Created administrator 'first.admin'", result.output)
        self.assertNotIn(password, result.output)
        with self.app.app_context():
            user = db.session.execute(db.select(User)).scalar_one()
            claim = db.session.get(AuthSetupState, 1)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Created initial administrator"
                )
            ).scalar_one()
            self.assertEqual(user.role, USER_ROLE_ADMIN)
            self.assertTrue(user.check_password(password))
            self.assertFalse(user.must_change_password)
            self.assertEqual(claim.user_id, user.id)
            self.assertEqual(event.actor, "cli")
            self.assertEqual(event.source, "flask users create-admin")
            self.assertNotIn("password", event.payload or "")

    def test_create_admin_rejects_short_password_without_partial_setup(self):
        result = self._invoke_with_password(
            ["users", "create-admin", "--username", "owner"],
            "too-short",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("too-short", result.output)
        with self.app.app_context():
            self.assertEqual(db.session.query(User).count(), 0)
            self.assertIsNone(db.session.get(AuthSetupState, 1))

    def test_second_create_admin_preserves_original_setup_claim(self):
        first = self._invoke_with_password(
            ["users", "create-admin", "--username", "first-admin"]
        )
        second = self._invoke_with_password(
            ["users", "create-admin", "--username", "recovery-admin"],
            "another sufficiently strong password",
        )

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 0, second.output)
        with self.app.app_context():
            first_user = db.session.execute(
                db.select(User).where(User.username == "first-admin")
            ).scalar_one()
            claim = db.session.get(AuthSetupState, 1)
            self.assertEqual(claim.user_id, first_user.id)
            self.assertEqual(db.session.query(User).count(), 2)

    def test_create_proxy_preprovisions_subject_without_password(self):
        result = self.app.test_cli_runner().invoke(
            args=[
                "users",
                "create-proxy",
                "--username",
                " Proxy.Admin ",
                "--subject",
                "stable-cli-subject",
                "--display-name",
                "Proxy Administrator",
                "--role",
                "admin",
            ]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Created proxy admin 'proxy.admin'", result.output)
        with self.app.app_context():
            user = db.session.execute(db.select(User)).scalar_one()
            self.assertEqual(user.auth_source, USER_AUTH_SOURCE_PROXY)
            self.assertEqual(user.external_subject, "stable-cli-subject")
            self.assertEqual(user.role, USER_ROLE_ADMIN)
            self.assertIsNone(user.password_hash)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.source == "flask users create-proxy"
                )
            ).scalar_one()
            self.assertNotIn("stable-cli-subject", event.payload or "")

    def test_create_proxy_rejects_duplicate_subject(self):
        runner = self.app.test_cli_runner()
        first = runner.invoke(
            args=[
                "users",
                "create-proxy",
                "--username",
                "first-proxy",
                "--subject",
                "same-subject",
            ]
        )
        second = runner.invoke(
            args=[
                "users",
                "create-proxy",
                "--username",
                "second-proxy",
                "--subject",
                "same-subject",
            ]
        )

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertNotEqual(second.exit_code, 0)
        with self.app.app_context():
            self.assertEqual(db.session.query(User).count(), 1)

    def test_reset_password_forces_change_and_revokes_existing_session(self):
        user_id, original_version = self._add_local_user(
            "locked-admin",
            role=USER_ROLE_ADMIN,
        )
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": original_version,
            }

        temporary_password = "temporary recovery password"
        result = self._invoke_with_password(
            ["users", "reset-password", "LOCKED-ADMIN"],
            temporary_password,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(temporary_password, result.output)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Reset account password through recovery CLI"
                )
            ).scalar_one()
            self.assertTrue(user.check_password(temporary_password))
            self.assertTrue(user.must_change_password)
            self.assertEqual(user.session_version, original_version + 1)
            payload = json.loads(event.payload)
            self.assertTrue(payload["must_change_password"])
            self.assertNotIn("password_hash", event.payload)

        stale_session = self.client.get("/admin/diagnostics", follow_redirects=False)
        self.assertEqual(stale_session.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

        self._set_csrf_token()
        login = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "locked-admin",
                "password": temporary_password,
            },
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302)
        self.assertIn("/account/password", login.headers["Location"])

    def test_reset_password_rejects_proxy_managed_account(self):
        with self.app.app_context():
            user = User(
                username="proxy-user",
                auth_source=USER_AUTH_SOURCE_PROXY,
                external_subject="proxy-subject",
            )
            db.session.add(user)
            db.session.commit()

        result = self.app.test_cli_runner().invoke(
            args=["users", "reset-password", "proxy-user"]
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Proxy-managed accounts", result.output)

    def test_activate_reactivates_user_and_revokes_stale_sessions(self):
        self._add_local_user("active-admin", role=USER_ROLE_ADMIN)
        user_id, original_version = self._add_local_user(
            "inactive-user",
            active=False,
        )

        result = self.app.test_cli_runner().invoke(
            args=["users", "activate", "inactive-user"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        with self.app.app_context():
            user = db.session.get(User, user_id)
            self.assertTrue(user.is_active)
            self.assertEqual(user.session_version, original_version + 1)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Activated account through recovery CLI"
                )
            ).scalar_one()
            self.assertEqual(event.actor, "cli")

    def test_revoke_sessions_invalidates_named_session(self):
        user_id, original_version = self._add_local_user(
            "session-admin",
            role=USER_ROLE_ADMIN,
        )
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": original_version,
            }

        result = self.app.test_cli_runner().invoke(
            args=["users", "revoke-sessions", "session-admin"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        response = self.client.get("/admin/diagnostics", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_list_reports_state_without_credential_material(self):
        password = "password that must stay secret"
        self._add_local_user(
            "listed-admin",
            role=USER_ROLE_ADMIN,
            password=password,
        )

        result = self.app.test_cli_runner().invoke(args=["users", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("listed-admin\tadmin\tlocal\tactive\tno", result.output)
        self.assertNotIn(password, result.output)
        with self.app.app_context():
            user = db.session.get(User, 1)
            self.assertNotIn(user.password_hash, result.output)
