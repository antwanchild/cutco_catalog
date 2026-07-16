# pyright: reportOptionalMemberAccess=false, reportArgumentType=false
from helpers import AUTH_SESSION_KEY, IDENTITY_KIND_TOKEN_ADMIN, IDENTITY_KIND_USER
from models import (
    ActivityEvent,
    AuthSetupState,
    USER_ROLE_ADMIN,
    User,
)
from smoke_support import SmokeBaseTest, db


class LocalAuthTests(SmokeBaseTest):
    def _set_csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf-token"

    def _complete_setup(
        self,
        *,
        username="owner",
        password="correct horse battery staple",
        display_name="Vault Owner",
    ):
        self._set_csrf()
        return self.client.post(
            "/setup",
            data={
                "csrf_token": "test-csrf-token",
                "token": "test-admin-token",
                "username": username,
                "display_name": display_name,
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=False,
        )

    def _logout(self):
        self._set_csrf()
        return self.client.post(
            "/admin/logout",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

    def test_initial_setup_page_is_available_only_before_first_user(self):
        response = self.client.get("/setup")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Initial Account Setup", response.data)
        self.assertIn(b'name="token"', response.data)
        self.assertIn(b'name="username"', response.data)
        self.assertIn(b'name="password_confirm"', response.data)

        setup_response = self._complete_setup()
        self.assertEqual(setup_response.status_code, 302)
        second_response = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(second_response.status_code, 302)
        self.assertIn("/admin/login", second_response.headers["Location"])

    def test_initial_setup_requires_csrf_and_valid_token(self):
        missing_csrf = self.client.post(
            "/setup",
            data={
                "token": "test-admin-token",
                "username": "owner",
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
            },
        )
        self.assertEqual(missing_csrf.status_code, 403)

        self._set_csrf()
        wrong_token = self.client.post(
            "/setup",
            data={
                "csrf_token": "test-csrf-token",
                "token": "wrong-token",
                "username": "owner",
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
            },
        )
        self.assertEqual(wrong_token.status_code, 200)
        self.assertIn(b"setup token is invalid", wrong_token.data)
        with self.app.app_context():
            self.assertEqual(db.session.query(User).count(), 0)

    def test_setup_creates_admin_claim_session_and_audit_event(self):
        response = self._complete_setup(username="First.Admin")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/diagnostics", response.headers["Location"])
        with self.client.session_transaction() as session:
            payload = session[AUTH_SESSION_KEY]
            self.assertEqual(payload["kind"], IDENTITY_KIND_USER)
            self.assertNotIn("role", payload)
            self.assertNotIn("username", payload)

        with self.app.app_context():
            user = db.session.execute(db.select(User)).scalar_one()
            claim = db.session.get(AuthSetupState, 1)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Created initial administrator"
                )
            ).scalar_one()
            self.assertEqual(user.username, "first.admin")
            self.assertEqual(user.role, USER_ROLE_ADMIN)
            self.assertTrue(user.check_password("correct horse battery staple"))
            self.assertEqual(claim.user_id, user.id)
            self.assertEqual(event.actor_user_id, user.id)
            self.assertNotIn("password", event.payload or "")

    def test_local_login_uses_generic_failure_and_records_last_login(self):
        self._complete_setup(username="local-admin")
        self._logout()

        missing_csrf = self.client.post(
            "/admin/login",
            data={
                "login_type": "local",
                "username": "local-admin",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(missing_csrf.status_code, 403)

        unknown = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "unknown",
                "password": "wrong password value",
            },
        )
        wrong_password = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "local-admin",
                "password": "wrong password value",
            },
        )
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(wrong_password.status_code, 200)
        self.assertIn(b"Invalid username or password", unknown.data)
        self.assertIn(b"Invalid username or password", wrong_password.data)

        success = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "  LOCAL-ADMIN  ",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )
        self.assertEqual(success.status_code, 302)
        self.assertEqual(success.headers["Location"], "/")
        with self.app.app_context():
            user = db.session.execute(
                db.select(User).where(User.username == "local-admin")
            ).scalar_one()
            self.assertIsNotNone(user.last_login_at)

    def test_local_login_rejects_inactive_user_and_ignores_external_redirect(self):
        self._complete_setup(username="redirect-admin")
        self._logout()
        with self.app.app_context():
            user = db.session.execute(
                db.select(User).where(User.username == "redirect-admin")
            ).scalar_one()
            fallback_admin = User(username="fallback-admin", role=USER_ROLE_ADMIN)
            fallback_admin.set_password("another secure administrator password")
            db.session.add(fallback_admin)
            db.session.commit()
            user.is_active = False
            db.session.commit()

        inactive = self.client.post(
            "/admin/login?next=https://example.com/escape",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "redirect-admin",
                "password": "correct horse battery staple",
                "next": "https://example.com/escape",
            },
            follow_redirects=False,
        )
        self.assertEqual(inactive.status_code, 200)
        self.assertIn(b"Invalid username or password", inactive.data)

        with self.app.app_context():
            user = db.session.execute(
                db.select(User).where(User.username == "redirect-admin")
            ).scalar_one()
            user.is_active = True
            db.session.commit()
        active = self.client.post(
            "/admin/login?next=https://example.com/escape",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "redirect-admin",
                "password": "correct horse battery staple",
                "next": "https://example.com/escape",
            },
            follow_redirects=False,
        )
        self.assertEqual(active.status_code, 302)
        self.assertEqual(active.headers["Location"], "/")

    def test_token_login_and_existing_token_session_stop_after_setup(self):
        self._set_csrf()
        token_login = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "token": "test-admin-token",
            },
            follow_redirects=False,
        )
        self.assertEqual(token_login.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(
                session[AUTH_SESSION_KEY],
                {"kind": IDENTITY_KIND_TOKEN_ADMIN},
            )

        setup_response = self._complete_setup()
        self.assertEqual(setup_response.status_code, 302)
        self._logout()

        blocked_login = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "token": "test-admin-token",
            },
            follow_redirects=False,
        )
        self.assertEqual(blocked_login.status_code, 200)
        self.assertIn(b"Token login is unavailable", blocked_login.data)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_preexisting_token_session_is_invalidated_when_user_appears(self):
        self._set_csrf()
        self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "token": "test-admin-token",
            },
        )
        with self.app.app_context():
            user = User(username="new-admin", role=USER_ROLE_ADMIN)
            user.set_password("correct horse battery staple")
            db.session.add(user)
            db.session.commit()

        response = self.client.get("/admin/diagnostics", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)

    def test_password_change_rehashes_password_and_revokes_other_sessions(self):
        self._complete_setup(username="password-admin")
        with self.app.app_context():
            user = db.session.execute(db.select(User)).scalar_one()
            original_hash = user.password_hash
            original_version = user.session_version
            user_id = user.id

        self._set_csrf()
        response = self.client.post(
            "/account/password",
            data={
                "csrf_token": "test-csrf-token",
                "current_password": "correct horse battery staple",
                "new_password": "a substantially different password",
                "password_confirm": "a substantially different password",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        with self.app.app_context():
            user = db.session.get(User, user_id)
            self.assertNotEqual(user.password_hash, original_hash)
            self.assertTrue(user.check_password("a substantially different password"))
            self.assertEqual(user.session_version, original_version + 1)
            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Changed account password"
                )
            ).scalar_one()
            self.assertEqual(event.actor_user_id, user.id)
            self.assertNotIn("password", event.payload or "")
        with self.client.session_transaction() as session:
            self.assertEqual(
                session[AUTH_SESSION_KEY]["session_version"],
                original_version + 1,
            )

    def test_forced_password_change_blocks_other_pages(self):
        with self.app.app_context():
            user = User(
                username="temporary-user",
                role=USER_ROLE_ADMIN,
                must_change_password=True,
            )
            user.set_password(
                "temporary password value",
                require_change=True,
            )
            db.session.add(user)
            db.session.commit()
            user_id = user.id
            session_version = user.session_version
        with self.client.session_transaction() as session:
            session[AUTH_SESSION_KEY] = {
                "kind": IDENTITY_KIND_USER,
                "user_id": user_id,
                "session_version": session_version,
            }

        blocked = self.client.get("/catalog", follow_redirects=False)
        password_page = self.client.get("/account/password")

        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/account/password", blocked.headers["Location"])
        self.assertEqual(password_page.status_code, 200)

    def test_logout_is_post_only_and_csrf_protected(self):
        self._complete_setup()

        self.assertEqual(self.client.get("/admin/logout").status_code, 405)
        missing_csrf = self.client.post("/admin/logout")
        self.assertEqual(missing_csrf.status_code, 403)

        response = self._logout()
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)
