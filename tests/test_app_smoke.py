import json
import os
from io import BytesIO
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import create_app
from extensions import db
from helpers import _collection_token, _gift_token, _notify_discord, _verify_collection_token, _verify_gift_token, check_wishlist_targets
from models import (
    CookwareSession,
    Item,
    ItemSetMember,
    ItemVariant,
    KnifeTask,
    KnifeTaskLog,
    Ownership,
    Person,
    SharpeningLog,
    Set,
)
from scraping import (
    _build_set_member_entries,
    _member_hover_title,
    _infer_visible_member_sku,
    _normalize_set_member_sku,
)
from blueprints.catalog import _load_member_snapshot
from time_utils import container_timezone, format_container_time


class SmokeBaseTest(unittest.TestCase):
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

    def _add_catalog_item(self, *, name="Test Knife", sku="TK-1", category="Kitchen Knives"):
        response = self.client.post(
            "/catalog/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": name,
                "sku": sku,
                "category": category,
                "edge_type": "Straight",
                "cutco_url": "https://example.com/test-knife",
                "notes": "Initial note",
                "colors": "",
                "in_catalog": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = db.session.execute(db.select(Item).filter_by(sku=sku)).scalar_one()
            variant = db.session.execute(db.select(ItemVariant).filter_by(item_id=item.id)).scalar_one()
        return item.id, variant.id

    def _add_person(self, name="Anthony", notes="Primary collector"):
        response = self.client.post(
            "/people/add",
            data={"csrf_token": "test-csrf-token", "name": name, "notes": notes},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            person = db.session.execute(db.select(Person).filter_by(name=name)).scalar_one()
        return person.id

    def _add_task(self, name="Slice apples"):
        response = self.client.post(
            "/tasks/manage/add",
            data={"csrf_token": "test-csrf-token", "name": name},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = db.session.execute(db.select(KnifeTask).filter_by(name=name)).scalar_one()
        return task.id

    def _add_set(self, name="Sample Set", sku="SS-1", item_ids=()):
        with self.app.app_context():
            item_set = Set(name=name, sku=sku)
            db.session.add(item_set)
            db.session.flush()
            for item_id in item_ids:
                db.session.add(ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=1))
            db.session.commit()
            return item_set.id


class PublicSmokeTests(SmokeBaseTest):
    def test_public_pages_load(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recent Activity", response.data)
        self.assertIn(b"Quick Actions", response.data)
        self.assertIn(b"Recently Changed", response.data)
        self.assertIn(b"Release &amp; Diagnostics", response.data)
        self.assertIn(b"\xc2\xa9", response.data)
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertNotIn("Strict-Transport-Security", response.headers)
        self.assertEqual(self.client.get("/robots.txt").status_code, 200)

    def test_public_pages_include_hsts_when_cookie_secure_enabled(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Strict-Transport-Security"], "max-age=31536000; includeSubDomains")

    def test_health_endpoint_reports_ok(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_version_endpoint_returns_metadata(self):
        response = self.client.get("/version")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("version", payload)
        self.assertIn("git_sha", payload)

    def test_search_page_renders_results_and_shortcuts(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(name="Search Knife", sku="SRCH-1")
        _person_id = self._add_person(name="Search Collector", notes="Search note")
        _set_id = self._add_set(name="Search Set", sku="SET-S", item_ids=(item_id,))
        self._add_task(name="Slice tomatoes")

        empty_response = self.client.get("/search")
        self.assertEqual(empty_response.status_code, 200)
        self.assertIn(b"Shortcuts", empty_response.data)

        response = self.client.get("/search?q=Search")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Search Knife", response.data)
        self.assertIn(b"Search Collector", response.data)
        self.assertIn(b"Search Set", response.data)
        self.assertIn(b"Catalog Items", response.data)

    def test_admin_login_sets_session_flag(self):
        response = self.client.post(
            "/admin/login",
            data={"token": "test-admin-token"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Secure", response.headers.get("Set-Cookie", ""))
        self.assertIn("Expires=", response.headers.get("Set-Cookie", ""))
        self.assertFalse(self.app.config["SESSION_REFRESH_EACH_REQUEST"])
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("is_admin"))

    def test_gift_share_and_collection_card_pages_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        owned_item_id, owned_variant_id = self._add_catalog_item(name="Gift Knife Owned", sku="GL-1")
        missing_item_id, _missing_variant_id = self._add_catalog_item(name="Gift Knife Missing", sku="GL-2")
        person_id = self._add_person(name="Gift Recipient", notes="")
        set_id = self._add_set(name="Gift Set", sku="GS-1", item_ids=(owned_item_id, missing_item_id))

        ownership_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(owned_variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "Already owned",
            },
            follow_redirects=False,
        )
        self.assertEqual(ownership_response.status_code, 302)

        gift_share_response = self.client.get(f"/sets/{set_id}/gift-token?person={person_id}")
        self.assertEqual(gift_share_response.status_code, 200)
        self.assertIn(b"Share Gift List", gift_share_response.data)

        with self.app.app_context():
            gift_token = _gift_token(set_id, person_id)
            card_token = _collection_token(person_id)

        gift_list_response = self.client.get(f"/gifts/{gift_token}")
        self.assertEqual(gift_list_response.status_code, 200)
        self.assertIn(b"Gift Recipient", gift_list_response.data)
        self.assertIn(b"Gift Knife Missing", gift_list_response.data)
        self.assertIn(b"still needed", gift_list_response.data)

        card_share_response = self.client.get(f"/people/{person_id}/collection-token")
        self.assertEqual(card_share_response.status_code, 200)
        self.assertIn(b"Share Collection Card", card_share_response.data)
        self.assertIn(b"Gift Recipient", card_share_response.data)

        collection_card_response = self.client.get(f"/collection-card/{card_token}")
        self.assertEqual(collection_card_response.status_code, 200)
        self.assertIn(b"Gift Recipient", collection_card_response.data)
        self.assertIn(b"Gift Knife Owned", collection_card_response.data)

    def test_wishlist_page_and_check_route(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Wishlist Knife", sku="WL-1")
        person_id = self._add_person(name="Wishlist Person", notes="")

        ownership_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Wishlist",
                "target_price": "59.99",
                "notes": "Waiting for a drop",
            },
            follow_redirects=False,
        )
        self.assertEqual(ownership_response.status_code, 302)

        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertIsNotNone(item)
            item.msrp = 49.99
            db.session.commit()

        wishlist_response = self.client.get(f"/wishlist?person={person_id}")
        self.assertEqual(wishlist_response.status_code, 200)
        self.assertIn(b"Wishlist Knife", wishlist_response.data)
        self.assertIn(b"target met", wishlist_response.data)

        with mock.patch("blueprints.people.DISCORD_WEBHOOK_URL", "https://discord.invalid"), \
             mock.patch("blueprints.people._notify_discord", return_value=True) as notify_mock:
            check_response = self.client.post(
                "/wishlist/check",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(check_response.status_code, 302)
        self.assertIn("/wishlist", check_response.headers["Location"])
        notify_mock.assert_called_once()

    def test_views_pages_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="View Knife", sku="VW-1")
        person_id = self._add_person(name="Viewer", notes="")
        set_id = self._add_set(name="View Set", sku="VS-1", item_ids=(item_id,))

        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "",
            },
            follow_redirects=False,
        )

        item_response = self.client.get(f"/views/item/{item_id}")
        matrix_response = self.client.get("/views/matrix")
        stats_response = self.client.get("/stats")
        gift_share_response = self.client.get(f"/sets/{set_id}/gift-token?person={person_id}")
        collection_share_response = self.client.get(f"/people/{person_id}/collection-token")

        self.assertEqual(item_response.status_code, 200)
        self.assertIn(b"View Knife", item_response.data)
        self.assertEqual(matrix_response.status_code, 200)
        self.assertIn(b"Matrix", matrix_response.data)
        self.assertEqual(stats_response.status_code, 200)
        self.assertIn(b"Coverage", stats_response.data)
        self.assertEqual(gift_share_response.status_code, 200)
        self.assertIn(b"Share Gift List", gift_share_response.data)
        self.assertEqual(collection_share_response.status_code, 200)
        self.assertIn(b"Share Collection Card", collection_share_response.data)

    def test_catalog_pages_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Catalog Knife", sku="CA-1")
        set_id = self._add_set(name="Catalog Set", sku="CS-1", item_ids=(item_id,))

        catalog_response = self.client.get("/catalog")
        variants_response = self.client.get(f"/catalog/{item_id}/variants")
        sets_response = self.client.get("/sets")
        set_detail_response = self.client.get(f"/sets/{set_id}")

        self.assertEqual(catalog_response.status_code, 200)
        self.assertIn(b"Catalog Knife", catalog_response.data)
        self.assertIn(b"data-confirm-title=\"Delete item\"", catalog_response.data)
        self.assertEqual(variants_response.status_code, 200)
        self.assertIn(b"Variants", variants_response.data)
        self.assertEqual(sets_response.status_code, 200)
        self.assertIn(b"Catalog Set", sets_response.data)
        self.assertEqual(set_detail_response.status_code, 200)
        self.assertIn(b"Catalog Set", set_detail_response.data)

        add_variant_response = self.client.post(
            f"/catalog/{item_id}/variants/add",
            data={"csrf_token": "test-csrf-token", "color": "Pearl White", "notes": "Alt color"},
            follow_redirects=False,
        )
        self.assertEqual(add_variant_response.status_code, 302)
        with self.app.app_context():
            added_variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item_id, color="Pearl White")
            ).scalar_one()

        edit_variant_response = self.client.post(
            f"/variants/{added_variant.id}/edit",
            data={"csrf_token": "test-csrf-token", "color": "Pearl Ivory", "notes": "Updated color"},
            follow_redirects=False,
        )
        self.assertEqual(edit_variant_response.status_code, 302)

    def test_people_pages_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="People Knife", sku="PL-1")
        person_id = self._add_person(name="People Viewer", notes="")

        add_ownership_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "Owned item",
            },
            follow_redirects=False,
        )
        self.assertEqual(add_ownership_response.status_code, 302)

        people_response = self.client.get("/people")
        collection_response = self.client.get(f"/people/{person_id}/collection")
        edit_page_response = self.client.get(f"/people/{person_id}/edit")
        wishlist_response = self.client.get("/wishlist")

        self.assertEqual(people_response.status_code, 200)
        self.assertIn(b"People Viewer", people_response.data)
        self.assertEqual(collection_response.status_code, 200)
        self.assertIn(b"Owned", collection_response.data)
        self.assertEqual(edit_page_response.status_code, 200)
        self.assertIn(b"People Viewer", edit_page_response.data)
        self.assertEqual(wishlist_response.status_code, 200)
        self.assertIn(b"Wishlist", wishlist_response.data)

    def test_data_routes_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Export Knife", sku="EX-1")
        person_id = self._add_person(name="Exporter", notes="")
        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "Exported",
            },
            follow_redirects=False,
        )

        export_page_response = self.client.get("/export")
        export_csv_response = self.client.get("/export/csv?filename=my export.csv")
        import_page_response = self.client.get("/import")
        import_template_response = self.client.get("/import/template")

        self.assertEqual(export_page_response.status_code, 200)
        self.assertIn(b"Export", export_page_response.data)
        self.assertEqual(export_csv_response.status_code, 200)
        self.assertEqual(export_csv_response.mimetype, "text/csv")
        self.assertIn("my_export.csv", export_csv_response.headers["Content-Disposition"])
        self.assertIn(b"Exporter", export_csv_response.data)
        self.assertEqual(import_page_response.status_code, 200)
        self.assertIn(b"Import", import_page_response.data)
        self.assertEqual(import_template_response.status_code, 200)
        self.assertEqual(import_template_response.mimetype, "text/csv")

    def test_log_dashboards_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        sharpening_item_id, _ = self._add_catalog_item(name="Sharpen View Knife", sku="SV-1")
        cookware_item_id, _ = self._add_catalog_item(name="Cook View Piece", sku="CV-1", category="Cookware")
        task_item_id, _ = self._add_catalog_item(name="Task View Knife", sku="TV-1")
        task_id = self._add_task(name="Slice onions")

        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(sharpening_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(cookware_item_id),
                "used_on": "2026-04-15",
                "made_item": "Soup",
                "rating": "5",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(task_item_id),
                "task_id": str(task_id),
                "logged_on": "2026-04-15",
                "notes": "",
            },
            follow_redirects=False,
        )

        sharpening_response = self.client.get("/sharpening")
        cookware_response = self.client.get("/cookware")
        tasks_response = self.client.get("/tasks")
        tasks_manage_response = self.client.get("/tasks/manage")
        task_detail_response = self.client.get(f"/tasks/manage/{task_id}")

        self.assertEqual(sharpening_response.status_code, 200)
        self.assertIn(b"Sharpening", sharpening_response.data)
        self.assertEqual(cookware_response.status_code, 200)
        self.assertIn(b"Cookware", cookware_response.data)
        self.assertEqual(tasks_response.status_code, 200)
        self.assertIn(b"Tasks", tasks_response.data)
        self.assertEqual(tasks_manage_response.status_code, 200)
        self.assertIn(b"Slice onions", tasks_manage_response.data)
        self.assertEqual(task_detail_response.status_code, 200)
        self.assertIn(b"Slice onions", task_detail_response.data)

        with mock.patch("blueprints.logs._notify_discord", return_value=True) as notify_mock, \
             mock.patch("blueprints.logs.DISCORD_WEBHOOK_URL", "https://discord.invalid"), \
             mock.patch("blueprints.logs.SHARPEN_THRESHOLD_DAYS", 1), \
             mock.patch("blueprints.logs.COOKWARE_THRESHOLD_DAYS", 1):
            sharpening_notify_response = self.client.post(
                "/sharpening/notify",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )
            cookware_notify_response = self.client.post(
                "/cookware/notify",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(sharpening_notify_response.status_code, 302)
        self.assertEqual(cookware_notify_response.status_code, 302)
        self.assertTrue(notify_mock.called)


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
        for _ in range(10):
            response = self.client.post(
                "/admin/login",
                data={"token": "wrong-token"},
                follow_redirects=False,
            )
            self.assertIn(response.status_code, (200, 302))

        response = self.client.post(
            "/admin/login",
            data={"token": "wrong-token"},
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
        self.assertIn(b"File too large", response.data)


class UtilitySmokeTests(SmokeBaseTest):
    def test_token_helpers_validate_and_reject_tampering(self):
        self._login_as_admin()
        self._set_csrf_token()

        with self.app.app_context():
            gift_token = _gift_token(12, 34)
            collection_token = _collection_token(56)
            self.assertEqual(_verify_gift_token(gift_token), (12, 34))
            self.assertEqual(_verify_collection_token(collection_token), 56)
            self.assertIsNone(_verify_gift_token("not-a-token"))
            self.assertIsNone(_verify_collection_token("not-a-token"))
            self.assertIsNone(_verify_gift_token(gift_token + "x"))
            self.assertIsNone(_verify_collection_token(collection_token + "x"))

    def test_notify_discord_handles_success_and_failure(self):
        with mock.patch("helpers.DISCORD_WEBHOOK_URL", None):
            self.assertFalse(_notify_discord("No webhook"))

        response = mock.Mock()
        response.raise_for_status.return_value = None
        with mock.patch("helpers.DISCORD_WEBHOOK_URL", "https://discord.invalid"), \
             mock.patch("helpers.requests.post", return_value=response) as post_mock:
            self.assertTrue(_notify_discord("Webhook works"))
            post_mock.assert_called_once()

        with mock.patch("helpers.DISCORD_WEBHOOK_URL", "https://discord.invalid"), \
             mock.patch("helpers.requests.post", side_effect=RuntimeError("boom")):
            self.assertFalse(_notify_discord("Webhook fails"))

    def test_set_member_entries_preserve_structured_skus(self):
        structured_members = [
            {"sku": "BBQ-1", "name": "Barbecue Tongs", "quantity": 1},
            {"sku": "BBQ-2", "name": "Barbecue Turner", "quantity": 2},
        ]
        visible_rows = [
            {"name": "Barbecue Tongs", "is_set_only": False},
            {"name": "Barbecue Turner", "is_set_only": False},
            {"name": "Extra Piece", "is_set_only": True},
        ]

        member_entries = _build_set_member_entries(
            structured_members,
            visible_rows,
            ["BBQ-1", "BBQ-2", "BBQ-3"],
            {"BBQ-1": 1, "BBQ-2": 2, "BBQ-3": 1},
        )

        self.assertEqual(member_entries[0]["sku"], "BBQ-1")
        self.assertEqual(member_entries[0]["name"], "Barbecue Tongs")
        self.assertEqual(member_entries[1]["sku"], "BBQ-2")
        self.assertEqual(member_entries[1]["quantity"], 2)
        self.assertEqual(member_entries[2]["sku"], "BBQ-3")
        self.assertEqual(member_entries[2]["name"], "Extra Piece")
        self.assertTrue(member_entries[2]["is_set_only"])

    def test_load_member_snapshot_dedupes_duplicate_skus(self):
        rows = _load_member_snapshot(
            json.dumps(
                [
                    {"sku": "10", "name": "Knife One", "quantity": 1},
                    {"sku": "10", "name": "Knife One", "quantity": 11},
                    {"sku": "1741", "name": "Knife Two", "quantity": 1},
                ]
            )
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sku"], "10")
        self.assertEqual(rows[0]["quantity"], 12)
        self.assertEqual(rows[1]["sku"], "1741")

    def test_normalize_set_member_skus_strips_variant_suffixes(self):
        self.assertEqual(_normalize_set_member_sku("1737W-1"), "1737")
        self.assertEqual(_normalize_set_member_sku("1737C-1"), "1737")
        self.assertEqual(_normalize_set_member_sku("1737/1"), "1737")
        self.assertEqual(_normalize_set_member_sku("77-"), "77")
        self.assertEqual(_normalize_set_member_sku("1716C"), "1716")
        self.assertIsNone(_normalize_set_member_sku("2026D"))
        self.assertEqual(_normalize_set_member_sku("1737"), "1737")
        self.assertIsNone(_normalize_set_member_sku(""))

    def test_member_hover_titles_trim_set_lists(self):
        self.assertEqual(
            _member_hover_title("Barbecue Tongs, Barbecue Turner, Barbecue Fork"),
            "Barbecue Tongs",
        )
        self.assertEqual(
            _member_hover_title("Super Shears - 77, 78"),
            "Super Shears",
        )
        self.assertEqual(
            _member_hover_title("Basting Spoon Slotted Spoon Ladle Mix-Stir Kitchen Tool Holder"),
            "Basting Spoon",
        )
        self.assertEqual(_member_hover_title("Super Shears"), "Super Shears")
        self.assertIsNone(_member_hover_title(""))

    def test_infer_visible_member_sku_supports_gift_box_pages(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><h1>#2026D</h1><h1>Gift Box for Super Shears</h1></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(_infer_visible_member_sku("Gift Box for Super Shears"), "2026D")
            self.assertIsNone(_infer_visible_member_sku("Super Shears"))

    def test_infer_visible_member_sku_supports_sheath_pages(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><h1>#2120-2</h1><h1>4\" Paring Knife Sheath</h1></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(_infer_visible_member_sku('4" Paring Knife Sheath'), "2120-2")

    def test_infer_visible_member_sku_supports_generic_box_rows(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><h1>#2130CD</h1><h1>Wine & Cheese Set</h1></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(
                _infer_visible_member_sku("Gift Box", context_url="https://www.cutco.com/p/wine-cheese-gift-set"),
                "2130CD",
            )

    def test_build_set_member_entries_uses_visible_row_skus(self):
        structured_members = [{"sku": "777", "name": "Super Shears", "quantity": 1}]
        visible_rows = [
            {"name": "Super Shears", "sku": "777", "is_set_only": False},
            {"name": "Gift Box", "sku": "123", "is_set_only": True},
        ]

        member_entries = _build_set_member_entries(
            structured_members,
            visible_rows,
            ["777", "123"],
            {"777": 1, "123": 1},
        )

        self.assertEqual(member_entries[0]["sku"], "777")
        self.assertEqual(member_entries[0]["name"], "Super Shears")
        self.assertEqual(member_entries[1]["sku"], "123")
        self.assertEqual(member_entries[1]["name"], "Gift Box")

    def test_admin_diagnostics_shows_schema_target(self):
        self._login_as_admin()

        response = self.client.get("/admin/diagnostics")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Schema Current", response.data)
        self.assertIn(b"Schema Target", response.data)

    def test_check_wishlist_targets_returns_hits(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Target Knife", sku="WT-1")
        person_id = self._add_person(name="Target Person", notes="")

        add_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Wishlist",
                "target_price": "59.99",
                "notes": "Waiting for a sale",
            },
            follow_redirects=False,
        )
        self.assertEqual(add_response.status_code, 302)

        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertIsNotNone(item)
            item.msrp = 49.99
            db.session.commit()

            hits = check_wishlist_targets()

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["person"], "Target Person")
        self.assertEqual(hits[0]["item"], "Target Knife")
        self.assertEqual(hits[0]["sku"], "WT-1")
        self.assertEqual(hits[0]["target"], 59.99)
        self.assertEqual(hits[0]["msrp"], 49.99)
        self.assertEqual(hits[0]["savings"], 10.0)

    def test_time_utils_format_in_container_timezone(self):
        with mock.patch.dict(os.environ, {"TZ": "America/Boise"}, clear=False):
            tz, tz_name = container_timezone()
            self.assertEqual(tz_name, "America/Boise")
            self.assertEqual(format_container_time(None), "—")
            self.assertEqual(format_container_time("not-a-time"), "not-a-time")
            self.assertEqual(
                format_container_time("2026-04-20T19:18:00+00:00"),
                "Apr 20, 2026, 1:18 PM MDT",
            )
            self.assertEqual(
                format_container_time("2026-04-20T19:18:00"),
                "Apr 20, 2026, 1:18 PM MDT",
            )

        self.assertEqual(tz.key, "America/Boise")

    def test_time_utils_invalid_timezone_falls_back_to_utc(self):
        with mock.patch.dict(os.environ, {"TZ": "Not/AZone"}, clear=False):
            tz, tz_name = container_timezone()
            self.assertEqual(tz_name, "UTC")
            self.assertEqual(format_container_time("2026-04-20T19:18:00+00:00"), "Apr 20, 2026, 7:18 PM UTC")


class ImportSmokeTests(SmokeBaseTest):
    def test_import_template_downloads_csv(self):
        response = self.client.get("/import/template")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn("cutco_import_template.csv", response.headers["Content-Disposition"])

    def test_import_check_accepts_basic_csv(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "check",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(b"name,sku,color,status\nParing Knife,1720,Classic Brown,Owned\n"),
                    "import.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Header check passed.", response.data)

    def test_import_confirm_creates_ownership(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Import Knife", sku="IM-1")

        response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "0",
                "own_count": "1",
                "total_rows": "1",
                "own_accept_0": "on",
                "own_row_0": "2",
                "own_item_id_0": str(item_id),
                "own_item_name_0": "Import Knife",
                "own_item_sku_0": "IM-1",
                "own_person_0": "Importer",
                "own_color_0": "Classic Brown",
                "own_status_0": "Owned",
                "own_notes_0": "Imported ownership",
                "own_sku_unicorn_0": "",
                "own_variant_unicorn_0": "",
                "own_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Import complete", response.data)
        with self.app.app_context():
            person = db.session.execute(db.select(Person).filter_by(name="Importer")).scalar_one()
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item_id, color="Classic Brown")
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person.id, variant_id=variant.id)
            ).scalar_one()
            self.assertEqual(ownership.status, "Owned")
            self.assertEqual(ownership.notes, "Imported ownership")

    def test_import_confirm_creates_catalog_item_and_set_from_item_rows(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "1",
                "own_count": "0",
                "total_rows": "1",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Imported Knife",
                "item_sku_0": "IM-2",
                "item_color_0": "Pearl White",
                "item_edge_0": "Straight",
                "item_category_0": "Kitchen Knives",
                "item_notes_0": "Imported note",
                "item_person_0": "Importer",
                "item_status_0": "Owned",
                "item_sets_0": "Imported Set",
                "item_sku_unicorn_0": "on",
                "item_variant_unicorn_0": "on",
                "item_edge_unicorn_0": "on",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Import complete", response.data)
        with self.app.app_context():
            item = db.session.execute(db.select(Item).filter_by(sku="IM-2")).scalar_one()
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item.id, color="Pearl White")
            ).scalar_one()
            person = db.session.execute(db.select(Person).filter_by(name="Importer")).scalar_one()
            item_set = db.session.execute(db.select(Set).filter_by(name="Imported Set")).scalar_one()
            membership = db.session.execute(
                db.select(ItemSetMember).filter_by(item_id=item.id, set_id=item_set.id)
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person.id, variant_id=variant.id)
            ).scalar_one()

            self.assertEqual(item.notes, "Imported note")
            self.assertEqual(membership.quantity, 1)
            self.assertIn(item, item_set.items)
            self.assertEqual(ownership.status, "Owned")

    def test_import_preview_renders_xlsx_rows(self):
        self._login_as_admin()
        self._set_csrf_token()
        self._add_catalog_item(name="Preview Knife", sku="PR-1")
        self._add_person(name="Anthony", notes="")

        workbook = Workbook()
        sheet = workbook.active
        sheet.append([
            "Name",
            "Model #",
            "COLOR",
            "Owned?",
            "person",
            "Price",
            "Gift Box",
            "Sheath",
            "Quantity Purchased",
            "Given Away",
            "Beast",
        ])
        sheet.append([
            "Preview Knife",
            "PR-1",
            "Classic Brown",
            "Anthony",
            "",
            "12.50",
            "yes",
            "Leather",
            "2",
            "n/a",
            "",
        ])
        sheet.append([
            "Preview New Knife",
            "PN-1",
            "Pearl White",
            "Wishlist",
            "Collector Two",
            "34.00",
            "",
            "",
            "",
            "",
            "yes",
        ])
        upload = BytesIO()
        workbook.save(upload)
        upload.seek(0)

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (upload, "preview.xlsx"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Import Preview", response.data)
        self.assertIn(b"Ownership Entries (1)", response.data)
        self.assertIn(b"New Catalog Items (1)", response.data)
        self.assertIn(b"Preview Knife", response.data)
        self.assertIn(b"Preview New Knife", response.data)
        self.assertIn(b"Price: 12.50", response.data)

    def test_import_check_reports_header_warnings(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "check",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(b"sku,color\nIM-2,Classic Brown\n"),
                    "headers.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Header Check Result (CSV)", response.data)
        self.assertIn(b"Missing required headers: name", response.data)
        self.assertIn(b"No ownership/status column found", response.data)

    def test_import_preview_csv_and_error_paths(self):
        self._login_as_admin()
        self._set_csrf_token()

        existing_item_id, _existing_variant_id = self._add_catalog_item(name="Import Existing Knife", sku="IM-EX-1")
        self._add_person(name="Import Existing Collector", notes="")

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,color,Owned?,person,Price,Gift Box,Sheath,Quantity Purchased,Given Away\n"
                        b"Import Existing Knife,IM-EX-1,Classic Brown,yes,Import Existing Collector,12.50,yes,Leather,2,n/a\n"
                        b"Import New Knife,IM-NEW-1,Pearl White,no,New Collector,34.00,,,,\n"
                    ),
                    "preview.csv",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Import Preview", preview_response.data)
        self.assertIn(b"Import Existing Knife", preview_response.data)
        self.assertIn(b"Import New Knife", preview_response.data)
        self.assertIn(b"Classic Brown", preview_response.data)

        invalid_check_response = self.client.post(
            "/import",
            data={
                "mode": "check",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(b"this is not a workbook"),
                    "broken.xlsx",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid_check_response.status_code, 200)
        self.assertIn(b"Could not read headers from this file", invalid_check_response.data)

        empty_upload_response = self.client.post(
            "/import",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(empty_upload_response.status_code, 200)
        self.assertIn(b"Please choose a file.", empty_upload_response.data)

        confirm_response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "1",
                "own_count": "1",
                "total_rows": "3",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Imported Confirm Knife",
                "item_sku_0": "IM-CF-1",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Straight",
                "item_category_0": "Kitchen Knives",
                "item_notes_0": "Imported from confirm",
                "item_person_0": "Confirm Collector",
                "item_status_0": "Owned",
                "item_sets_0": "Confirm Set",
                "item_sku_unicorn_0": "on",
                "item_variant_unicorn_0": "on",
                "item_edge_unicorn_0": "on",
                "own_accept_0": "on",
                "own_row_0": "3",
                "own_item_id_0": str(existing_item_id),
                "own_item_name_0": "Import Existing Knife",
                "own_item_sku_0": "IM-EX-1",
                "own_person_0": "Import Existing Collector",
                "own_color_0": "Classic Brown",
                "own_status_0": "Wishlist",
                "own_notes_0": "Existing ownership",
                "own_sku_unicorn_0": "",
                "own_variant_unicorn_0": "",
                "own_edge_unicorn_0": "",
                "error_count": "1",
                "error_row_0": "4",
                "error_name_0": "Broken Row",
                "error_sku_0": "BR-1",
                "error_reason_0": "Could not parse row.",
                "conflict_count": "1",
                "conflict_row_0": "5",
                "conflict_item_0": "Import Existing Knife",
                "conflict_sku_0": "IM-EX-1",
                "conflict_person_0": "Import Existing Collector",
                "conflict_existing_status_0": "Owned",
                "conflict_import_status_0": "Wishlist",
            },
            follow_redirects=False,
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertIn(b"Import complete", confirm_response.data)
        self.assertIn(b"Could not parse row.", confirm_response.data)
        self.assertIn(b"kept unchanged", confirm_response.data)
        with self.app.app_context():
            confirm_item = db.session.execute(
                db.select(Item).filter_by(sku="IM-CF-1")
            ).scalar_one()
            self.assertEqual(confirm_item.msrp, None)
            confirm_set = db.session.execute(db.select(Set).filter_by(name="Confirm Set")).scalar_one()
            self.assertEqual(confirm_set.members[0].quantity, 1)


class PeopleSmokeTests(SmokeBaseTest):
    def test_people_add_creates_a_record(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/people/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Anthony",
                "notes": "Primary collector",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            person = db.session.execute(db.select(Person).filter_by(name="Anthony")).scalar_one_or_none()
            self.assertIsNotNone(person)
            self.assertEqual(person.notes, "Primary collector")

    def test_people_bulk_status_and_purge_collection(self):
        self._login_as_admin()
        self._set_csrf_token()
        _item_id, variant_id = self._add_catalog_item(name="Bulk Knife", sku="BL-1")
        _second_item_id, second_variant_id = self._add_catalog_item(name="Bulk Knife Two", sku="BL-2")
        person_id = self._add_person(name="Bulk Collector", notes="")

        first_add_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Wishlist",
                "target_price": "49.99",
                "notes": "Queued",
            },
            follow_redirects=False,
        )

        self.assertEqual(first_add_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person_id, variant_id=variant_id)
            ).scalar_one()

        second_add_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(second_variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "To remove",
            },
            follow_redirects=False,
        )

        self.assertEqual(second_add_response.status_code, 302)
        with self.app.app_context():
            second_ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person_id, variant_id=second_variant_id)
            ).scalar_one()

        bulk_response = self.client.post(
            f"/people/{person_id}/bulk-status",
            data={
                "csrf_token": "test-csrf-token",
                "bulk_action": "status",
                "ownership_ids": [str(ownership.id)],
                "bulk_status": "Owned",
            },
            follow_redirects=False,
        )

        self.assertEqual(bulk_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.get(Ownership, ownership.id)
            self.assertIsNotNone(ownership)
            self.assertEqual(ownership.status, "Owned")

        target_response = self.client.post(
            f"/people/{person_id}/bulk-status",
            data={
                "csrf_token": "test-csrf-token",
                "bulk_action": "target",
                "ownership_ids": [str(ownership.id)],
                "bulk_target_price": "39.99",
            },
            follow_redirects=False,
        )

        self.assertEqual(target_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.get(Ownership, ownership.id)
            self.assertIsNotNone(ownership)
            self.assertEqual(ownership.status, "Wishlist")
            self.assertEqual(ownership.target_price, 39.99)

        delete_response = self.client.post(
            f"/people/{person_id}/bulk-status",
            data={
                "csrf_token": "test-csrf-token",
                "bulk_action": "delete",
                "ownership_ids": [str(second_ownership.id)],
            },
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ownership, second_ownership.id))

        purge_response = self.client.post(
            f"/people/{person_id}/purge-collection",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(purge_response.status_code, 302)
        with self.app.app_context():
            remaining = db.session.execute(
                db.select(Ownership).filter_by(person_id=person_id)
            ).scalars().all()
            self.assertEqual(remaining, [])

    def test_people_delete_removes_a_record(self):
        self._login_as_admin()
        self._set_csrf_token()
        person_id = self._add_person(name="To Delete", notes="")

        response = self.client.post(
            f"/people/{person_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Person, person_id))


class CatalogSmokeTests(SmokeBaseTest):
    def test_catalog_page_filters_and_forms_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(name="Filter Knife", sku="FL-1", category="Kitchen Knives")
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            item.is_unicorn = True
            db.session.add(Item(name="Set Only Knife", sku="SO-1", category="Kitchen Knives", set_only=True, in_catalog=False))
            db.session.add(Item(name="Off Catalog Knife", sku="OC-1", category="Kitchen Knives", set_only=False, in_catalog=False))
            db.session.commit()

        catalog_response = self.client.get("/catalog?q=Filter&category=Kitchen+Knives&unicorn=1&sort=sku&dir=desc")
        set_only_response = self.client.get("/catalog?status=set_only")
        off_catalog_response = self.client.get("/catalog?status=off_catalog")
        non_catalog_response = self.client.get("/catalog?status=non_catalog")
        add_page_response = self.client.get("/catalog/add")
        edit_page_response = self.client.get(f"/catalog/{item_id}/edit")

        self.assertEqual(catalog_response.status_code, 200)
        self.assertIn(b"Filter Knife", catalog_response.data)
        self.assertIn(b"Set-only", set_only_response.data)
        self.assertIn(b"Set Only Knife", set_only_response.data)
        self.assertIn(b"Off Catalog Knife", off_catalog_response.data)
        self.assertIn(b"Set Only Knife", non_catalog_response.data)
        self.assertIn(b"Off Catalog Knife", non_catalog_response.data)
        self.assertEqual(add_page_response.status_code, 200)
        self.assertIn(b"Add Item", add_page_response.data)
        self.assertEqual(edit_page_response.status_code, 200)
        self.assertIn(b"Filter Knife", edit_page_response.data)

    def test_catalog_validation_and_sort_fallbacks(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Variant Knife", sku="VR-1")
        cookware_item_id, cookware_variant_id = self._add_catalog_item(name="Cookware Variant", sku="CVR-1", category="Cookware")
        set_id = self._add_set(name="Variant Set", sku="VS-1", item_ids=(item_id,))

        duplicate_set_response = self.client.post(
            "/sets/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Variant Set",
                "sku": "VS-2",
                "notes": "Duplicate set",
            },
            follow_redirects=False,
        )
        self.assertEqual(duplicate_set_response.status_code, 302)

        empty_variant_response = self.client.post(
            f"/catalog/{item_id}/variants/add",
            data={"csrf_token": "test-csrf-token", "color": "", "notes": ""},
            follow_redirects=False,
        )
        duplicate_variant_response = self.client.post(
            f"/catalog/{item_id}/variants/add",
            data={"csrf_token": "test-csrf-token", "color": "Classic Brown", "notes": ""},
            follow_redirects=False,
        )
        cookware_color_response = self.client.post(
            f"/catalog/{cookware_item_id}/variants/add",
            data={"csrf_token": "test-csrf-token", "color": "Pearl White", "notes": ""},
            follow_redirects=False,
        )
        self.assertEqual(empty_variant_response.status_code, 302)
        self.assertEqual(duplicate_variant_response.status_code, 302)
        self.assertEqual(cookware_color_response.status_code, 302)

        with self.app.app_context():
            cookware_variant = db.session.get(ItemVariant, cookware_variant_id)

        empty_edit_response = self.client.post(
            f"/variants/{variant_id}/edit",
            data={"csrf_token": "test-csrf-token", "color": "", "notes": ""},
            follow_redirects=False,
        )
        cookware_edit_response = self.client.post(
            f"/variants/{cookware_variant.id}/edit",
            data={"csrf_token": "test-csrf-token", "color": "Classic Brown", "notes": ""},
            follow_redirects=False,
        )
        self.assertEqual(empty_edit_response.status_code, 302)
        self.assertEqual(cookware_edit_response.status_code, 302)

        set_edit_response = self.client.post(
            f"/sets/{set_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Variant Set Updated",
                "sku": "VS-UPDATED",
                "notes": "Updated set",
                "member_item_ids": [str(item_id), "not-an-id"],
                f"member_qty_{item_id}": "bogus",
            },
            follow_redirects=False,
        )
        self.assertEqual(set_edit_response.status_code, 302)

        set_detail_fallback = self.client.get(f"/sets/{set_id}?person=1&sort=bogus&dir=sideways")
        self.assertEqual(set_detail_fallback.status_code, 200)
        self.assertIn(b"Variant Set Updated", set_detail_fallback.data)

        with self.app.app_context():
            updated_set = db.session.get(Set, set_id)
            self.assertIsNotNone(updated_set)
            self.assertEqual(updated_set.members[0].quantity, 1)
            self.assertEqual(updated_set.sku, "VS-UPDATED")

    def test_catalog_add_and_edit_item(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item()

        edit_response = self.client.post(
            f"/catalog/{item_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Test Knife Updated",
                "sku": "TK-1",
                "category": "Kitchen Knives",
                "edge_type": "Straight",
                "cutco_url": "https://example.com/test-knife-updated",
                "notes": "Updated note",
                "is_unicorn": "on",
                "edge_is_unicorn": "on",
                "in_catalog": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(edit_response.status_code, 302)
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertIsNotNone(item)
            self.assertEqual(item.name, "Test Knife Updated")
            self.assertEqual(item.cutco_url, "https://example.com/test-knife-updated")
            self.assertEqual(item.notes, "Updated note")
            self.assertTrue(item.is_unicorn)
            self.assertTrue(item.edge_is_unicorn)

    def test_catalog_set_and_variant_management_routes(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Set Knife", sku="SK-1")

        set_add_response = self.client.post(
            "/sets/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Set Group",
                "sku": "SG-1",
                "notes": "Initial set",
            },
            follow_redirects=False,
        )
        self.assertEqual(set_add_response.status_code, 302)
        with self.app.app_context():
            item_set = db.session.execute(db.select(Set).filter_by(name="Set Group")).scalar_one()

        sets_page = self.client.get("/sets")
        set_detail_page = self.client.get(f"/sets/{item_set.id}")
        set_edit_page = self.client.get(f"/sets/{item_set.id}/edit")

        self.assertEqual(sets_page.status_code, 200)
        self.assertIn(b"Set Group", sets_page.data)
        self.assertIn(b"SG-1", sets_page.data)
        self.assertEqual(set_detail_page.status_code, 200)
        self.assertIn(b"Set Group", set_detail_page.data)
        self.assertEqual(set_edit_page.status_code, 200)
        self.assertIn(b"Set Members", set_edit_page.data)

        edit_response = self.client.post(
            f"/sets/{item_set.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Set Group Updated",
                "sku": "SG-2",
                "notes": "Updated set",
                "member_item_ids": [str(item_id)],
                f"member_qty_{item_id}": "2",
            },
            follow_redirects=False,
        )
        self.assertEqual(edit_response.status_code, 302)
        with self.app.app_context():
            item_set = db.session.get(Set, item_set.id)
            self.assertIsNotNone(item_set)
            self.assertEqual(item_set.name, "Set Group Updated")
            self.assertEqual(item_set.sku, "SG-2")
            self.assertEqual(item_set.notes, "Updated set")
            self.assertEqual(item_set.members[0].quantity, 2)

        add_variant_response = self.client.post(
            f"/catalog/{item_id}/variants/add",
            data={"csrf_token": "test-csrf-token", "color": "Pearl White", "notes": "Alt color"},
            follow_redirects=False,
        )
        self.assertEqual(add_variant_response.status_code, 302)
        with self.app.app_context():
            new_variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item_id, color="Pearl White")
            ).scalar_one()

        delete_variant_response = self.client.post(
            f"/variants/{new_variant.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_variant_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(ItemVariant, new_variant.id))

        delete_set_response = self.client.post(
            f"/sets/{item_set.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_set_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Set, item_set.id))

    def test_catalog_sync_preview_renders_with_mocked_scrapes(self):
        self._login_as_admin()
        self._set_csrf_token()

        self._add_catalog_item(name="Existing Sync Knife", sku="EX-1")

        scraped_items = [
            {
                "name": "Existing Sync Knife",
                "sku": "EX-1",
                "category": "Kitchen Knives",
                "url": "https://example.com/existing",
            },
            {
                "name": "New Sync Knife",
                "sku": "NS-1",
                "category": "Kitchen Knives",
                "url": "https://example.com/new-sync",
            },
        ]
        scraped_sets = [
            {
                "name": "New Sync Set",
                "sku": "NSS-1",
                "url": "https://example.com/new-set",
                "member_skus": ["EX-1", "NS-1", "NS-2"],
                "member_quantities": {"EX-1": 2, "NS-1": 1, "NS-2": 1},
                "member_entries": [
                    {"sku": "EX-1", "name": "Existing Sync Knife", "quantity": 2},
                    {"sku": "NS-1", "name": "Found Sync Knife", "quantity": 1},
                    {"sku": "NS-2", "name": "Missing Sync Knife", "quantity": 1},
                ],
            }
        ]

        with mock.patch("blueprints.catalog.scrape_catalog", return_value=(scraped_items, [])), \
             mock.patch("blueprints.catalog.scrape_sets", return_value=scraped_sets), \
             mock.patch(
                 "blueprints.catalog.scrape_item_specs",
                 return_value={
                     "edge_type": "Straight",
                     "msrp": 49.99,
                     "blade_length": "4 in",
                     "overall_length": "8 in",
                     "weight": "1 lb",
                 },
             ):
            response = self.client.get("/catalog/sync")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Catalog Sync Preview", response.data)
        self.assertIn(b"New Items", response.data)
        self.assertIn(b"New Sync Knife", response.data)
        self.assertIn(b"New Sets", response.data)
        self.assertIn(b"New Sync Set", response.data)
        self.assertNotIn(b"EX-1 ,", response.data)
        self.assertIn(b"Missing item numbers", response.data)

    def test_catalog_sync_uses_populates_tasks(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Use Sync Knife", sku="US-1")
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            item.cutco_url = "https://example.com/use-sync"
            db.session.commit()

        with mock.patch("blueprints.catalog.scrape_item_uses", return_value=["Slice onions", "Peel potatoes"]):
            response = self.client.post(
                "/catalog/sync-uses",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/tasks/manage", response.headers["Location"])
        with self.app.app_context():
            refreshed_item = db.session.get(Item, item_id)
            task_names = [task.name for task in refreshed_item.suggested_tasks]
            self.assertIn("Slice onions", task_names)
            self.assertIn("Peel potatoes", task_names)

    def test_catalog_sync_confirm_creates_items_and_sets(self):
        self._login_as_admin()
        self._set_csrf_token()

        existing_item_id, _existing_variant_id = self._add_catalog_item(name="Sync Existing Knife", sku="SX-EX-1")
        stale_item_id, _stale_variant_id = self._add_catalog_item(name="Sync Stale Knife", sku="SX-STALE-1")
        existing_set_id = self._add_set(name="Sync Existing Set", sku="SX-SET-1", item_ids=(existing_item_id, stale_item_id))

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "selected_skus": ["SX-NEW-1"],
                "name_SX-NEW-1": "Sync New Knife",
                "category_SX-NEW-1": "Kitchen Knives",
                "url_SX-NEW-1": "https://example.com/sync-new",
                "edge_type_SX-NEW-1": "Straight",
                "msrp_SX-NEW-1": "not-a-number",
                "blade_length_SX-NEW-1": "4 in",
                "overall_length_SX-NEW-1": "8 in",
                "weight_SX-NEW-1": "1 lb",
                "selected_sets": ["Sync New Set"],
                "set_count": "1",
                "set_name_0": "Sync New Set",
                "set_sku_0": "SX-SET-NEW",
                "set_member_entries_0": json.dumps(
                    [
                        {"sku": "SX-NEW-1", "name": "Sync New Knife", "quantity": 2},
                        {"sku": "SX-MISS-1", "name": "Sync Missing Knife", "quantity": 1},
                    ]
                ),
                "create_missing_set_members": "on",
                "existing_set_count": "1",
                "existing_set_name_0": "Sync Existing Set",
                "existing_set_member_entries_0": json.dumps(
                    [
                        {"sku": "SX-EX-1", "name": "Sync Existing Knife", "quantity": 3},
                        {"sku": "SX-EX-MISS-1", "name": "Sync Existing Missing Knife", "quantity": 1},
                    ]
                ),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            new_item = db.session.execute(db.select(Item).filter_by(sku="SX-NEW-1")).scalar_one()
            self.assertIsNone(new_item.msrp)
            new_set = db.session.execute(db.select(Set).filter_by(name="Sync New Set")).scalar_one()
            self.assertEqual(len(new_set.members), 2)
            self.assertEqual(new_set.members[0].quantity, 2)
            created_member = db.session.execute(db.select(Item).filter_by(sku="SX-MISS-1")).scalar_one()
            self.assertFalse(created_member.in_catalog)
            self.assertTrue(created_member.set_only)
            self.assertIsNotNone(new_set.member_data)
            self.assertIn("SX-MISS-1", new_set.member_data)
            existing_set = db.session.get(Set, existing_set_id)
            self.assertEqual(len(existing_set.members), 2)
            self.assertEqual(existing_set.members[0].quantity, 3)
            created_existing_member = db.session.execute(db.select(Item).filter_by(sku="SX-EX-MISS-1")).scalar_one()
            self.assertFalse(created_existing_member.in_catalog)
            self.assertTrue(created_existing_member.set_only)
            self.assertIsNotNone(existing_set.member_data)
            existing_member_skus = {db.session.get(Item, membership.item_id).sku for membership in existing_set.members}
            self.assertNotIn("SX-STALE-1", existing_member_skus)
            self.assertIn("SX-EX-1", existing_member_skus)
            self.assertIn("SX-EX-MISS-1", existing_member_skus)

        set_detail_response = self.client.get(f"/sets/{new_set.id}")
        existing_set_detail_response = self.client.get(f"/sets/{existing_set.id}")
        self.assertEqual(set_detail_response.status_code, 200)
        self.assertIn(b"Imported Members", set_detail_response.data)
        self.assertIn(b"SX-MISS-1", set_detail_response.data)
        self.assertEqual(existing_set_detail_response.status_code, 200)
        self.assertIn(b"Sync Existing Missing Knife", existing_set_detail_response.data)

    def test_catalog_purge_and_delete_routes(self):
        self._login_as_admin()
        self._set_csrf_token()

        keep_item_id, keep_variant_id = self._add_catalog_item(name="Keep Knife", sku="KP-1")
        drop_item_id, _drop_variant_id = self._add_catalog_item(name="Drop Knife", sku="DR-1")
        person_id = self._add_person(name="Catalog Keeper", notes="")
        self._add_set(name="Catalog Set", sku="CS-1", item_ids=(keep_item_id,))

        add_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(keep_variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(add_response.status_code, 302)

        purge_unreferenced = self.client.post(
            "/catalog/purge-unreferenced",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_unreferenced.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Item, keep_item_id))
            self.assertIsNone(db.session.get(Item, drop_item_id))

        delete_response = self.client.post(
            f"/catalog/{keep_item_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Item, keep_item_id))

        temp_item_id, _ = self._add_catalog_item(name="Purge All Knife", sku="PA-1")
        self._add_set(name="Purge All Set", sku="PS-1", item_ids=(temp_item_id,))

        purge_all = self.client.post(
            "/catalog/purge-all",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_all.status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.query(Item).count(), 0)
            self.assertEqual(db.session.query(Set).count(), 0)


class OwnershipSmokeTests(SmokeBaseTest):
    def test_ownership_add_edit_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()

        _item_id, variant_id = self._add_catalog_item(name="Ownership Knife", sku="OK-1")
        person_id = self._add_person(name="Collector One", notes="")

        add_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "First ownership",
            },
            follow_redirects=False,
        )

        self.assertEqual(add_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person_id, variant_id=variant_id)
            ).scalar_one()
            self.assertEqual(ownership.status, "Owned")
            self.assertEqual(ownership.notes, "First ownership")

        edit_response = self.client.post(
            f"/ownership/{ownership.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "status": "Wishlist",
                "target_price": "89.00",
                "notes": "Updated ownership",
            },
            follow_redirects=False,
        )

        self.assertEqual(edit_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.get(Ownership, ownership.id)
            self.assertIsNotNone(ownership)
            self.assertEqual(ownership.status, "Wishlist")
            self.assertEqual(ownership.target_price, 89.00)
            self.assertEqual(ownership.notes, "Updated ownership")

        delete_response = self.client.post(
            f"/ownership/{ownership.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ownership, ownership.id))


class LogSmokeTests(SmokeBaseTest):
    def test_log_pages_render_and_notifications(self):
        self._login_as_admin()
        self._set_csrf_token()

        sharpening_item_id, _ = self._add_catalog_item(name="Sharpen Page Knife", sku="SR-1")
        cookware_item_id, _ = self._add_catalog_item(name="Cookware Page Knife", sku="CW-1", category="Cookware")
        task_item_id, _ = self._add_catalog_item(name="Task Page Knife", sku="TP-1")
        task_id = self._add_task(name="Slice onions")

        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(sharpening_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Page check",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(cookware_item_id),
                "used_on": "2026-04-15",
                "made_item": "Soup",
                "rating": "4",
                "notes": "Page check",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(task_item_id),
                "task_id": str(task_id),
                "logged_on": "2026-04-15",
                "notes": "Page check",
            },
            follow_redirects=False,
        )

        sharpening_page = self.client.get("/sharpening")
        cookware_page = self.client.get("/cookware")
        tasks_page = self.client.get("/tasks")
        tasks_manage_page = self.client.get("/tasks/manage")
        task_detail_page = self.client.get(f"/tasks/manage/{task_id}")

        self.assertEqual(sharpening_page.status_code, 200)
        self.assertIn(b"Sharpening Log", sharpening_page.data)
        self.assertEqual(cookware_page.status_code, 200)
        self.assertIn(b"Cookware", cookware_page.data)
        self.assertEqual(tasks_page.status_code, 200)
        self.assertIn(b"Knife Task Log", tasks_page.data)
        self.assertEqual(tasks_manage_page.status_code, 200)
        self.assertIn(b"Manage Knife Tasks", tasks_manage_page.data)
        self.assertEqual(task_detail_page.status_code, 200)
        self.assertIn(b"Slice onions", task_detail_page.data)

        with mock.patch("blueprints.logs.DISCORD_WEBHOOK_URL", "https://example.com/webhook"), \
             mock.patch("blueprints.logs.SHARPEN_THRESHOLD_DAYS", 1), \
             mock.patch("blueprints.logs.COOKWARE_THRESHOLD_DAYS", 1), \
             mock.patch("blueprints.logs._notify_discord") as notify_mock:
            sharpen_notify_response = self.client.post(
                "/sharpening/notify",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )
            cook_notify_response = self.client.post(
                "/cookware/notify",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(sharpen_notify_response.status_code, 302)
        self.assertEqual(cook_notify_response.status_code, 302)
        self.assertGreaterEqual(notify_mock.call_count, 2)

    def test_log_validation_and_no_notification_paths(self):
        self._login_as_admin()
        self._set_csrf_token()

        sharpening_item_id, _ = self._add_catalog_item(name="Validation Sharpen Knife", sku="VL-1")
        cookware_item_id, _ = self._add_catalog_item(name="Validation Cookware Knife", sku="VC-1", category="Cookware")
        task_item_id, _ = self._add_catalog_item(name="Validation Task Knife", sku="VT-1")
        task_id = self._add_task(name="Slice carrots")

        sharpen_edit = self.client.get("/sharpening")
        cookware_edit = self.client.get("/cookware")
        self.assertEqual(sharpen_edit.status_code, 200)
        self.assertEqual(cookware_edit.status_code, 200)

        missing_sharpen_date = self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(sharpening_item_id),
                "sharpened_on": "",
                "method": "Whetstone",
                "notes": "",
            },
            follow_redirects=False,
        )
        invalid_sharpen_date = self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(sharpening_item_id),
                "sharpened_on": "2026-99-99",
                "method": "Whetstone",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(missing_sharpen_date.status_code, 302)
        self.assertEqual(invalid_sharpen_date.status_code, 302)

        missing_cookware_fields = self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": "",
                "used_on": "2026-04-15",
                "made_item": "",
                "rating": "7",
                "notes": "",
            },
            follow_redirects=False,
        )
        invalid_cookware_date = self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(cookware_item_id),
                "used_on": "2026-99-99",
                "made_item": "Soup",
                "rating": "bogus",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(missing_cookware_fields.status_code, 302)
        self.assertEqual(invalid_cookware_date.status_code, 302)

        missing_task_fields = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": "",
                "task_id": "",
                "logged_on": "",
                "notes": "",
            },
            follow_redirects=False,
        )
        invalid_task_date = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(task_item_id),
                "task_id": str(task_id),
                "logged_on": "2026-99-99",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(missing_task_fields.status_code, 302)
        self.assertEqual(invalid_task_date.status_code, 302)

        with self.app.app_context():
            sharpen_entry = db.session.execute(db.select(SharpeningLog).filter_by(item_id=sharpening_item_id)).first()
            cookware_entry = db.session.execute(db.select(CookwareSession).filter_by(item_id=cookware_item_id)).first()
            task_entry = db.session.execute(db.select(KnifeTaskLog).filter_by(item_id=task_item_id)).first()

        self.assertIsNone(sharpen_entry)
        self.assertIsNone(cookware_entry)
        self.assertIsNone(task_entry)

        with self.app.app_context():
            self.client.post(
                "/sharpening/add",
                data={
                    "csrf_token": "test-csrf-token",
                    "item_id": str(sharpening_item_id),
                    "sharpened_on": "2026-04-20",
                    "method": "Home Sharpener",
                    "notes": "Recent sharpen",
                },
                follow_redirects=False,
            )
            self.client.post(
                "/cookware/add",
                data={
                    "csrf_token": "test-csrf-token",
                    "item_id": str(cookware_item_id),
                    "used_on": "2026-04-20",
                    "made_item": "Soup",
                    "rating": "4",
                    "notes": "Recent use",
                },
                follow_redirects=False,
            )

        with mock.patch("blueprints.logs.DISCORD_WEBHOOK_URL", None), \
             mock.patch("blueprints.logs.SHARPEN_THRESHOLD_DAYS", 999), \
             mock.patch("blueprints.logs.COOKWARE_THRESHOLD_DAYS", 999):
            no_sharpen_notify = self.client.post(
                "/sharpening/notify",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )
            no_cook_notify = self.client.post(
                "/cookware/notify",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(no_sharpen_notify.status_code, 302)
        self.assertEqual(no_cook_notify.status_code, 302)

    def test_log_edit_and_task_validation_paths(self):
        self._login_as_admin()
        self._set_csrf_token()

        sharpening_item_id, _ = self._add_catalog_item(name="Edit Sharpen Knife", sku="ED-1")
        cookware_item_id, _ = self._add_catalog_item(name="Edit Cookware Knife", sku="ED-2", category="Cookware")
        task_item_id, _ = self._add_catalog_item(name="Edit Task Knife", sku="ED-3")
        task_id = self._add_task(name="Slice onions")

        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(sharpening_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(cookware_item_id),
                "used_on": "2026-04-15",
                "made_item": "Soup",
                "rating": "4",
                "notes": "",
            },
            follow_redirects=False,
        )

        with self.app.app_context():
            sharpen_entry = db.session.execute(db.select(SharpeningLog).filter_by(item_id=sharpening_item_id)).scalar_one()
            cookware_entry = db.session.execute(db.select(CookwareSession).filter_by(item_id=cookware_item_id)).scalar_one()

        bad_sharpen_edit = self.client.post(
            f"/sharpening/{sharpen_entry.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "sharpened_on": "bad-date",
                "method": "Whetstone",
                "notes": "",
            },
            follow_redirects=False,
        )
        bad_cookware_edit = self.client.post(
            f"/cookware/{cookware_entry.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "used_on": "bad-date",
                "made_item": "Soup",
                "rating": "not-a-number",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(bad_sharpen_edit.status_code, 302)
        self.assertEqual(bad_cookware_edit.status_code, 302)

        missing_task_name = self.client.post(
            "/tasks/manage/add",
            data={"csrf_token": "test-csrf-token", "name": ""},
            follow_redirects=False,
        )
        duplicate_task_name = self.client.post(
            "/tasks/manage/add",
            data={"csrf_token": "test-csrf-token", "name": "Slice onions"},
            follow_redirects=False,
        )
        self.assertEqual(missing_task_name.status_code, 302)
        self.assertEqual(duplicate_task_name.status_code, 302)

        missing_task_item = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": "",
                "task_id": str(task_id),
                "logged_on": "2026-04-15",
                "notes": "",
            },
            follow_redirects=False,
        )
        missing_task_task = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(task_item_id),
                "task_id": "",
                "logged_on": "2026-04-15",
                "notes": "",
            },
            follow_redirects=False,
        )
        bad_task_date = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(task_item_id),
                "task_id": str(task_id),
                "logged_on": "bad-date",
                "notes": "",
            },
            follow_redirects=False,
        )
        missing_task = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": "999999",
                "task_id": str(task_id),
                "logged_on": "2026-04-15",
                "notes": "",
            },
            follow_redirects=False,
        )
        missing_task_obj = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(task_item_id),
                "task_id": "999999",
                "logged_on": "2026-04-15",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(missing_task_item.status_code, 302)
        self.assertEqual(missing_task_task.status_code, 302)
        self.assertEqual(bad_task_date.status_code, 302)
        self.assertEqual(missing_task.status_code, 302)
        self.assertEqual(missing_task_obj.status_code, 302)

    def test_sharpening_add_edit_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Sharpening Knife", sku="SH-1")

        add_response = self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "First sharpen",
            },
            follow_redirects=False,
        )

        self.assertEqual(add_response.status_code, 302)
        with self.app.app_context():
            entry = db.session.execute(db.select(SharpeningLog).filter_by(item_id=item_id)).scalar_one()
            self.assertEqual(entry.method, "Whetstone")
            self.assertEqual(entry.notes, "First sharpen")

        edit_response = self.client.post(
            f"/sharpening/{entry.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "sharpened_on": "2026-04-14",
                "method": "Professional",
                "notes": "Updated sharpen",
            },
            follow_redirects=False,
        )

        self.assertEqual(edit_response.status_code, 302)
        with self.app.app_context():
            entry = db.session.get(SharpeningLog, entry.id)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.sharpened_on, "2026-04-14")
            self.assertEqual(entry.method, "Professional")
            self.assertEqual(entry.notes, "Updated sharpen")

        delete_response = self.client.post(
            f"/sharpening/{entry.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(SharpeningLog, entry.id))

    def test_sharpening_purge_routes(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Sharpen Purge Knife", sku="SP-1")

        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "sharpened_on": "2026-04-15",
                "method": "Home Sharpener",
                "notes": "Purge me",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            entry = db.session.execute(db.select(SharpeningLog).filter_by(item_id=item_id)).scalar_one()
            self.assertEqual(entry.method, "Home Sharpener")

        purge_item_response = self.client.post(
            f"/sharpening/item/{item_id}/purge",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_item_response.status_code, 302)
        with self.app.app_context():
            remaining = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=item_id)
            ).scalars().all()
            self.assertEqual(remaining, [])

        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "sharpened_on": "2026-04-16",
                "method": "Home Sharpener",
                "notes": "Delete me",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            delete_entry = db.session.execute(db.select(SharpeningLog).filter_by(item_id=item_id)).scalar_one()

        delete_response = self.client.post(
            f"/sharpening/{delete_entry.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(SharpeningLog, delete_entry.id))

    def test_cookware_add_edit_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Cookware Piece", sku="CK-1", category="Cookware")

        add_response = self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "used_on": "2026-04-15",
                "made_item": "Pasta",
                "rating": "5",
                "notes": "First use",
            },
            follow_redirects=False,
        )

        self.assertEqual(add_response.status_code, 302)
        with self.app.app_context():
            session = db.session.execute(
                db.select(CookwareSession).filter_by(item_id=item_id)
            ).scalar_one()
            self.assertEqual(session.made_item, "Pasta")
            self.assertEqual(session.rating, 5)

        edit_response = self.client.post(
            f"/cookware/{session.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "used_on": "2026-04-14",
                "made_item": "Soup",
                "rating": "4",
                "notes": "Updated use",
            },
            follow_redirects=False,
        )

        self.assertEqual(edit_response.status_code, 302)
        with self.app.app_context():
            session = db.session.get(CookwareSession, session.id)
            self.assertIsNotNone(session)
            self.assertEqual(session.used_on, "2026-04-14")
            self.assertEqual(session.made_item, "Soup")
            self.assertEqual(session.rating, 4)
            self.assertEqual(session.notes, "Updated use")

        delete_response = self.client.post(
            f"/cookware/{session.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(CookwareSession, session.id))

    def test_cookware_purge_routes(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Cookware Purge", sku="CP-1", category="Cookware")

        self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "used_on": "2026-04-15",
                "made_item": "Soup",
                "rating": "4",
                "notes": "Purge me",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            session = db.session.execute(db.select(CookwareSession).filter_by(item_id=item_id)).scalar_one()
            self.assertEqual(session.made_item, "Soup")
            self.assertEqual(session.rating, 4)

        purge_item_response = self.client.post(
            f"/cookware/item/{item_id}/purge",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_item_response.status_code, 302)
        with self.app.app_context():
            remaining = db.session.execute(
                db.select(CookwareSession).filter_by(item_id=item_id)
            ).scalars().all()
            self.assertEqual(remaining, [])

        self.client.post(
            "/cookware/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "used_on": "2026-04-16",
                "made_item": "Bread",
                "rating": "5",
                "notes": "Delete me",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            delete_session = db.session.execute(db.select(CookwareSession).filter_by(item_id=item_id)).scalar_one()

        delete_response = self.client.post(
            f"/cookware/{delete_session.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(CookwareSession, delete_session.id))


class TaskSmokeTests(SmokeBaseTest):
    def test_task_add_detail_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Task Knife", sku="TS-1")
        task_id = self._add_task(name="Slice apples")

        detail_response = self.client.get(f"/tasks/manage/{task_id}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(b"Slice apples", detail_response.data)

        add_log_response = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "task_id": str(task_id),
                "logged_on": "2026-04-15",
                "notes": "Fresh log",
            },
            follow_redirects=False,
        )
        self.assertEqual(add_log_response.status_code, 302)
        with self.app.app_context():
            entry = db.session.execute(
                db.select(KnifeTaskLog).filter_by(item_id=item_id, task_id=task_id)
            ).scalar_one()
            self.assertEqual(entry.notes, "Fresh log")

        delete_blocked = self.client.post(
            f"/tasks/manage/{task_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_blocked.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(KnifeTask, task_id))

        purge_log_response = self.client.post(
            f"/tasks/log/{entry.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_log_response.status_code, 302)

        delete_response = self.client.post(
            f"/tasks/manage/{task_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(KnifeTask, task_id))


if __name__ == "__main__":
    unittest.main()
