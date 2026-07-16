import json  # noqa: F401
import os  # noqa: F401
import tempfile  # noqa: F401
import unittest  # noqa: F401
from datetime import UTC, datetime, timedelta, timezone  # noqa: F401
from unittest import mock  # noqa: F401

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from flask import Flask  # noqa: F401

from app import _teardown_logging, create_app  # noqa: F401
import constants  # noqa: F401
from constants import KNIFE_TASK_PRESETS  # noqa: F401
from extensions import db  # noqa: F401
from helpers import AUTH_SESSION_KEY  # noqa: F401
import msrp_jobs  # noqa: F401
from models import Item, ItemSetMember, ItemVariant, KnifeTask, Set  # noqa: F401
from schema_migrations import (  # noqa: F401
    SCHEMA_VERSION,
    SchemaState,
    apply_schema_migrations,
)
from startup import BOOTSTRAP_VERSION, BootstrapState, initialize_database  # noqa: F401


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
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        _teardown_logging(self.temp_dir.name)
        self.temp_dir.cleanup()

    def _login_as_admin(self):
        self._set_csrf_token()
        self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "token": "test-admin-token",
            },
            follow_redirects=False,
        )

    def _set_csrf_token(self, value="test-csrf-token"):
        with self.client.session_transaction() as session:
            session["csrf_token"] = value
        return value
