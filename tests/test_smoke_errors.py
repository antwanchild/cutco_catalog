# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class ErrorSmokeTests(SmokeBaseTest):
    def test_forbidden_page_shows_access_denied(self):
        self._login_as_admin()

        response = self.client.post(
            "/people/add",
            data={"name": "No CSRF", "notes": "blocked"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Access denied.", response.data)

    def test_rate_limited_login_returns_429(self):
        self._set_csrf_token()
        for _ in range(10):
            response = self.client.post(
                "/admin/login",
                data={
                    "csrf_token": "test-csrf-token",
                    "login_type": "local",
                    "username": "unknown",
                    "password": "wrong-password",
                },
                follow_redirects=False,
            )
            self.assertIn(response.status_code, (200, 302))

        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "login_type": "local",
                "username": "unknown",
                "password": "wrong-password",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn(b"Too many requests", response.data)

    def test_large_upload_returns_413(self):
        self._login_as_admin()
        self._set_csrf_token()
        oversized_csv = BytesIO(b"a" * (10 * 1024 * 1024 + 1))

        response = self.client.post(
            "/import",
            data={
                "mode": "check",
                "csrf_token": "test-csrf-token",
                "csvfile": (oversized_csv, "too-big.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 413)
        self.assertIn(b"Request too large", response.data)
        self.assertIn(b"upload or submitted form data", response.data)
