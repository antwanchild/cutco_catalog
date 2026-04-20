import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import create_app
from constants import KNIFE_TASK_PRESETS
from extensions import db
from models import Item, ItemSetMember, KnifeTask, Set
from schema_migrations import SCHEMA_VERSION, SchemaState, apply_schema_migrations
from startup import BOOTSTRAP_VERSION, BootstrapState, initialize_database


class AdminJobSmokeTests(unittest.TestCase):
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
        self.client.post("/admin/login", data={"token": "test-admin-token"}, follow_redirects=False)

    def _set_csrf_token(self, value="test-csrf-token"):
        with self.client.session_transaction() as session:
            session["csrf_token"] = value
        return value

    def test_catalog_sync_confirm_creates_item_and_set(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "selected_skus": ["SYNC-1"],
                "name_SYNC-1": "Sync Knife",
                "category_SYNC-1": "Kitchen Knives",
                "url_SYNC-1": "https://example.com/sync-knife",
                "edge_type_SYNC-1": "Straight",
                "msrp_SYNC-1": "49.99",
                "blade_length_SYNC-1": "4 in",
                "overall_length_SYNC-1": "8 in",
                "weight_SYNC-1": "1 lb",
                "set_count": "1",
                "selected_sets": ["Sync Set"],
                "set_name_0": "Sync Set",
                "set_sku_0": "SS-1",
                "set_members_0": "SYNC-1",
                "set_member_qtys_0": "SYNC-1:2",
                "existing_set_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = db.session.execute(db.select(Item).filter_by(sku="SYNC-1")).scalar_one()
            item_set = db.session.execute(db.select(Set).filter_by(name="Sync Set")).scalar_one()
            membership = db.session.execute(
                db.select(ItemSetMember).filter_by(item_id=item.id, set_id=item_set.id)
            ).scalar_one()
            self.assertEqual(item.name, "Sync Knife")
            self.assertEqual(item.category, "Kitchen Knives")
            self.assertEqual(item.cutco_url, "https://example.com/sync-knife")
            self.assertEqual(item.msrp, 49.99)
            self.assertEqual(membership.quantity, 2)

    def test_specs_backfill_run_starts_background_job(self):
        self._login_as_admin()
        self._set_csrf_token()

        with mock.patch("blueprints.admin._read_specs_job", return_value={"status": "idle"}), \
             mock.patch("blueprints.admin._write_specs_job") as write_mock, \
             mock.patch("blueprints.admin.threading.Thread") as thread_mock:
            thread_instance = mock.Mock()
            thread_mock.return_value = thread_instance

            response = self.client.post(
                "/admin/specs-backfill/run",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        write_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.args[0]["status"], "running")
        thread_instance.start.assert_called_once()

    def test_msrp_diff_run_starts_background_job(self):
        self._login_as_admin()
        self._set_csrf_token()

        with mock.patch("blueprints.admin._read_msrp_job", return_value={"status": "idle"}), \
             mock.patch("blueprints.admin._write_msrp_job") as write_mock, \
             mock.patch("blueprints.admin.threading.Thread") as thread_mock:
            thread_instance = mock.Mock()
            thread_mock.return_value = thread_instance

            response = self.client.post(
                "/admin/msrp-diff/run",
                data={"csrf_token": "test-csrf-token", "update_db": "on"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        write_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.args[0]["status"], "running")
        self.assertTrue(write_mock.call_args.args[0]["update_db"])
        thread_instance.start.assert_called_once()

    def test_admin_diagnostics_shows_job_summaries(self):
        self._login_as_admin()

        response = self.client.get("/admin/diagnostics")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MSRP Diff", response.data)
        self.assertIn(b"Specs Backfill", response.data)
        self.assertIn(b"Schema Migrations", response.data)
        self.assertIn(b"Bootstrap History", response.data)
        self.assertIn(b"Bootstrap Version", response.data)
        self.assertIn(str(SCHEMA_VERSION).encode(), response.data)
        self.assertIn(str(BOOTSTRAP_VERSION).encode(), response.data)

    def test_startup_bootstrap_is_idempotent(self):
        with self.app.app_context():
            schema_state = db.session.get(SchemaState, "schema")
            self.assertIsNotNone(schema_state)
            self.assertEqual(schema_state.version, SCHEMA_VERSION)
            initial_task_names = {
                task.name for task in db.session.execute(db.select(KnifeTask)).scalars().all()
            }
            bootstrap_state = db.session.get(BootstrapState, "bootstrap")
            self.assertIsNotNone(bootstrap_state)
            self.assertEqual(bootstrap_state.version, BOOTSTRAP_VERSION)
            initial_updated_at = bootstrap_state.updated_at
            initial_version = bootstrap_state.version
            apply_schema_migrations()
            initialize_database()
            second_state = db.session.get(BootstrapState, "bootstrap")
            second_task_names = {
                task.name for task in db.session.execute(db.select(KnifeTask)).scalars().all()
            }
            self.assertEqual(initial_task_names, set(KNIFE_TASK_PRESETS))
            self.assertIsNotNone(second_state)
            self.assertEqual(second_task_names, initial_task_names)
            self.assertEqual(second_state.version, initial_version)
            self.assertEqual(second_state.updated_at, initial_updated_at)
            self.assertEqual(len(second_task_names), len(KNIFE_TASK_PRESETS))
