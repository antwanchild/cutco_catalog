import os
import tempfile
import unittest
from datetime import timedelta, timezone
from unittest import mock

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import create_app
import constants
from constants import KNIFE_TASK_PRESETS
from extensions import db
from models import Item, ItemSetMember, ItemVariant, KnifeTask, Set
from schema_migrations import SCHEMA_VERSION, SchemaState, apply_schema_migrations
from startup import BOOTSTRAP_VERSION, BootstrapState, initialize_database
from scraping import scrape_sets


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

    def test_scrape_sets_prefers_visible_set_pieces_for_set_only_members(self):
        listing_html = "<html><body></body></html>"
        detail_html = """
            <html><body>
              <script>
                var webItemsMap={"BBQ-SET-1":{"itemSetList":[
                  {"childItemNumber":"GB-1","qty":1,"name":"Gift Box 1"},
                  {"childItemNumber":"GB-2","qty":2,"name":"Gift Box 2"},
                  {"childItemNumber":"GB-3","qty":1,"name":"Gift Box 3"}
                ]}};
              </script>
              <h3>Set Pieces</h3>
              <ul>
                <li>Barbecue Tongs</li>
                <li>Barbecue Turner</li>
                <li>Barbecue Fork</li>
              </ul>
            </body></html>
        """

        def fake_get(url, headers=None, timeout=None):
            response = mock.Mock()
            response.status_code = 200
            response.raise_for_status.return_value = None
            response.text = listing_html if url == "https://www.cutco.com/shop/knife-sets" else detail_html
            return response

        with mock.patch("scraping.requests.get", side_effect=fake_get), \
             mock.patch("scraping._fetch_sku_from_page", return_value=("BBQ-SET-1", "Barbecue Set")):
            scraped_sets = scrape_sets(extra_candidates=[("Barbecue Set", "https://example.com/barbecue-set")])

        self.assertEqual(len(scraped_sets), 1)
        set_data = scraped_sets[0]
        self.assertEqual([member["name"] for member in set_data["member_entries"]], [
            "Barbecue Tongs",
            "Barbecue Turner",
            "Barbecue Fork",
        ])
        self.assertTrue(all(member["is_set_only"] for member in set_data["member_entries"]))

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
        self.assertIn(b"Service Health", response.data)
        self.assertIn(b"Runtime Snapshot", response.data)
        self.assertIn(b"MSRP Diff", response.data)
        self.assertIn(b"Specs Backfill", response.data)
        self.assertIn(b"Schema Migrations", response.data)
        self.assertIn(b"Bootstrap History", response.data)
        self.assertIn(b"Storage &amp; Paths", response.data)
        self.assertIn(b"Bootstrap Version", response.data)
        self.assertIn(b"Recorded At", response.data)
        self.assertIn(b"Older entries may reflect the time this database first recorded the migration.", response.data)
        self.assertIn(b"from local repo", response.data)
        self.assertIn(b"UTC", response.data)
        self.assertIn(str(SCHEMA_VERSION).encode(), response.data)
        self.assertIn(str(BOOTSTRAP_VERSION).encode(), response.data)

    def test_admin_diagnostics_formats_history_in_container_timezone(self):
        self._login_as_admin()

        with mock.patch.dict(os.environ, {"TZ": "America/Denver"}, clear=False):
            response = self.client.get("/admin/diagnostics")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MDT", response.data)

    def test_admin_diagnostics_falls_back_to_git_sha(self):
        self._login_as_admin()
        constants.get_git_sha_info.cache_clear()
        self.addCleanup(constants.get_git_sha_info.cache_clear)

        with mock.patch.dict(os.environ, {"GIT_SHA": ""}, clear=False), \
             mock.patch("constants._read_git_sha_from_repo", return_value="0123456789abcdef0123456789abcdef01234567"):
            response = self.client.get("/admin/diagnostics")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"0123456", response.data)
        self.assertIn(b"from local repo", response.data)

    def test_admin_routes_require_login(self):
        msrp_page = self.client.get("/admin/msrp-diff", follow_redirects=False)
        specs_page = self.client.get("/admin/specs-backfill", follow_redirects=False)
        diagnostics_page = self.client.get("/admin/diagnostics", follow_redirects=False)
        logout_response = self.client.get("/admin/logout", follow_redirects=False)
        msrp_status = self.client.get("/admin/msrp-diff/status")
        specs_status = self.client.get("/admin/specs-backfill/status")

        self.assertEqual(msrp_page.status_code, 302)
        self.assertEqual(specs_page.status_code, 302)
        self.assertEqual(diagnostics_page.status_code, 302)
        self.assertEqual(logout_response.status_code, 302)
        self.assertEqual(msrp_status.status_code, 403)
        self.assertEqual(specs_status.status_code, 403)
        self.assertEqual(msrp_status.get_json()["error"], "Unauthorized")
        self.assertEqual(specs_status.get_json()["error"], "Unauthorized")

    def test_admin_routes_render_and_api_surfaces(self):
        self._login_as_admin()

        with self.app.app_context():
            item = Item(
                name="Route Knife",
                sku="RT-1",
                category="Kitchen Knives",
                edge_type="Straight",
            )
            db.session.add(item)
            db.session.flush()
            db.session.add(ItemVariant(item_id=item.id, color="Classic Brown"))
            db.session.commit()
            item_id = item.id

        with mock.patch(
                "blueprints.admin._read_msrp_job",
                return_value={
                    "status": "idle",
                    "progress": [],
                    "started_at": "2026-04-20T18:02:00+00:00",
                    "finished_at": "2026-04-20T18:07:00+00:00",
                    "results": None,
                    "error": None,
                    "update_db": False,
                },
            ), \
             mock.patch(
                "blueprints.admin._read_specs_job",
                return_value={
                    "status": "idle",
                    "progress": [],
                    "started_at": "2026-04-20T18:02:00+00:00",
                    "finished_at": "2026-04-20T18:07:00+00:00",
                    "results": None,
                    "error": None,
                },
            ), \
             mock.patch(
                "time_utils.container_timezone",
                return_value=(timezone(timedelta(hours=-6), "MDT"), "MDT"),
            ), \
             mock.patch.dict(
                 os.environ,
                 {"DATA_DIR": "", "LOG_DIR": "", "GIT_SHA": "abcdef1234567890"},
                 clear=False,
             ):
            constants.get_git_sha_info.cache_clear()
            self.app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://user:secret@db.example.com:5432/cutco"

            msrp_page = self.client.get("/admin/msrp-diff")
            specs_page = self.client.get("/admin/specs-backfill")
            diagnostics_page = self.client.get("/admin/diagnostics")
            msrp_status = self.client.get("/admin/msrp-diff/status")
            specs_status = self.client.get("/admin/specs-backfill/status")
            variants_response = self.client.get(f"/api/variants/{item_id}")
            logout_response = self.client.get("/admin/logout", follow_redirects=False)

        self.assertEqual(msrp_page.status_code, 200)
        self.assertIn(b"MSRP Diff", msrp_page.data)
        self.assertIn(b"Admin Snapshot", msrp_page.data)
        self.assertIn(b"12:02 PM MDT", msrp_page.data)
        self.assertEqual(specs_page.status_code, 200)
        self.assertIn(b"Specs Backfill", specs_page.data)
        self.assertIn(b"Admin Snapshot", specs_page.data)
        self.assertIn(b"12:02 PM MDT", specs_page.data)
        self.assertEqual(diagnostics_page.status_code, 200)
        self.assertIn(b"Schema Migrations", diagnostics_page.data)
        self.assertIn(b"Bootstrap History", diagnostics_page.data)
        self.assertIn(b"postgresql://user:***@db.example.com:5432/cutco", diagnostics_page.data)
        self.assertIn(b"from image build", diagnostics_page.data)
        self.assertEqual(msrp_status.status_code, 200)
        self.assertEqual(msrp_status.get_json()["status"], "idle")
        self.assertEqual(specs_status.status_code, 200)
        self.assertEqual(specs_status.get_json()["status"], "idle")
        self.assertEqual(variants_response.status_code, 200)
        self.assertEqual(variants_response.get_json()[0]["color"], "Classic Brown")
        self.assertEqual(logout_response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("is_admin"))

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
