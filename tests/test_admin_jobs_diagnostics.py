# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
import os
from datetime import timedelta, timezone

from admin_jobs_support import (
    AUTH_SESSION_KEY,
    AdminJobBaseTest,
    BOOTSTRAP_VERSION,
    Item,
    ItemVariant,
    SCHEMA_VERSION,
    constants,
    db,
    mock,
)


class AdminDiagnosticsSmokeTests(AdminJobBaseTest):
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
        self.assertIn(
            b"Older entries may reflect the time this database first recorded the migration.",
            response.data,
        )
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

        with (
            mock.patch.dict(os.environ, {"GIT_SHA": ""}, clear=False),
            mock.patch(
                "constants._read_git_sha_from_repo",
                return_value="0123456789abcdef0123456789abcdef01234567",
            ),
        ):
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
        self.assertEqual(logout_response.status_code, 405)
        self.assertEqual(msrp_status.status_code, 403)
        self.assertEqual(specs_status.status_code, 403)
        self.assertEqual(msrp_status.get_json()["error"], "Unauthorized")
        self.assertEqual(specs_status.get_json()["error"], "Unauthorized")

    def test_admin_routes_render_and_api_surfaces(self):
        self._login_as_admin()
        self._set_csrf_token()

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

        with (
            mock.patch(
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
            ),
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
            ),
            mock.patch(
                "time_utils.container_timezone",
                return_value=(timezone(timedelta(hours=-6), "MDT"), "MDT"),
            ),
            mock.patch.dict(
                os.environ,
                {"DATA_DIR": "", "LOG_DIR": "", "GIT_SHA": "abcdef1234567890"},
                clear=False,
            ),
        ):
            constants.get_git_sha_info.cache_clear()
            self.app.config["SQLALCHEMY_DATABASE_URI"] = (
                "postgresql://user:secret@db.example.com:5432/cutco"
            )

            msrp_page = self.client.get("/admin/msrp-diff")
            specs_page = self.client.get("/admin/specs-backfill")
            diagnostics_page = self.client.get("/admin/diagnostics")
            msrp_status = self.client.get("/admin/msrp-diff/status")
            specs_status = self.client.get("/admin/specs-backfill/status")
            variants_response = self.client.get(f"/api/variants/{item_id}")
            logout_response = self.client.post(
                "/admin/logout",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

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
        self.assertIn(
            b"postgresql://user:***@db.example.com:5432/cutco", diagnostics_page.data
        )
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
            self.assertIsNone(session.get(AUTH_SESSION_KEY))
