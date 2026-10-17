import os
import tempfile
import unittest

os.environ.setdefault("INITIAL_SETUP_TOKEN", "test-initial-setup-token")

from app import _teardown_logging, create_app
from extensions import db


class AdminJobBaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = f"{self.temp_dir.name}/test.db"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
                "LOG_DIR": self.temp_dir.name,
                "INITIAL_SETUP_TOKEN": "test-initial-setup-token",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        _teardown_logging(self.temp_dir.name)
        self.temp_dir.cleanup()

    def _login_as_admin(self):
        self._set_csrf_token()
        self.client.post(
            "/setup",
            data={
                "csrf_token": "test-csrf-token",
                "setup_token": "test-initial-setup-token",
                "username": "admin-job-user",
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
            },
            follow_redirects=False,
        )

    def _set_csrf_token(self, value="test-csrf-token"):
        with self.client.session_transaction() as session:
            session["csrf_token"] = value
        return value
