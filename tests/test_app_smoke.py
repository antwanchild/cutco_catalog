import os
from io import BytesIO
import tempfile
import unittest

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import create_app
from extensions import db
from models import (
    CookwareSession,
    Item,
    ItemVariant,
    KnifeTask,
    KnifeTaskLog,
    Ownership,
    Person,
    SharpeningLog,
)


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


class PublicSmokeTests(SmokeBaseTest):
    def test_public_pages_load(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/robots.txt").status_code, 200)

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

    def test_admin_login_sets_session_flag(self):
        response = self.client.post(
            "/admin/login",
            data={"token": "test-admin-token"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertTrue(session.get("is_admin"))


class ImportSmokeTests(SmokeBaseTest):
    def test_import_template_downloads_csv(self):
        response = self.client.get("/import/template")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn("cutco_import_template.csv", response.headers["Content-Disposition"])

    def test_import_compatibility_check_accepts_basic_csv(self):
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
        self.assertIn(b"Compatibility check passed.", response.data)

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
