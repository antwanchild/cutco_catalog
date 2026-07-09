# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
from admin_jobs_support import (
    AdminJobBaseTest,
    Item,
    ItemSetMember,
    Set,
    UTC,
    db,
    datetime,
    json,
    mock,
    timedelta,
)


class AdminCatalogJobSmokeTests(AdminJobBaseTest):
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
            item = db.session.execute(
                db.select(Item).filter_by(sku="SYNC-1")
            ).scalar_one()
            item_set = db.session.execute(
                db.select(Set).filter_by(name="Sync Set")
            ).scalar_one()
            membership = db.session.execute(
                db.select(ItemSetMember).filter_by(item_id=item.id, set_id=item_set.id)
            ).scalar_one()
            self.assertEqual(item.name, "Sync Knife")
            self.assertEqual(item.category, "Kitchen Knives")
            self.assertEqual(item.cutco_url, "https://example.com/sync-knife")
            self.assertEqual(item.msrp, 49.99)
            self.assertEqual(membership.quantity, 2)

    def test_stale_catalog_sync_job_is_recovered_on_read(self):
        stale_started_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        job_data = {
            "status": "running",
            "progress": ["Scraping live catalog…"],
            "results": None,
            "error": None,
            "started_at": stale_started_at,
            "finished_at": None,
            "preview": None,
            "heartbeat_at": stale_started_at,
        }
        job_file = f"{self.temp_dir.name}/catalog_sync_job.json"
        with open(job_file, "w", encoding="utf-8") as fh:
            json.dump(job_data, fh)

        self._login_as_admin()
        with mock.patch("blueprints.catalog._CATALOG_SYNC_JOB_FILE", job_file):
            response = self.client.get("/catalog/sync")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Catalog sync failed", response.data)
        self.assertIn(b"stale", response.data.lower())
