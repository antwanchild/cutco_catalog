import os  # noqa: F401
import tempfile  # noqa: F401
import unittest  # noqa: F401

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import create_app  # noqa: F401
from extensions import db  # noqa: F401


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
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _login_as_admin(self):
        self.client.post(
            "/admin/login", data={"token": "test-admin-token"}, follow_redirects=False
        )

    def _set_csrf_token(self, value="test-csrf-token"):
        with self.client.session_transaction() as session:
            session["csrf_token"] = value
        return value
