import os
from io import BytesIO
import tempfile
import unittest

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app import create_app
from extensions import db
from models import Item, ItemVariant, Ownership, Person


class AppSmokeTests(unittest.TestCase):
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

    def _add_catalog_item(self, *, name="Test Knife", sku="TK-1"):
        response = self.client.post(
            "/catalog/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": name,
                "sku": sku,
                "category": "Kitchen Knives",
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
        return item, variant

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
            person = Person.query.filter_by(name="Anthony").one_or_none()
            self.assertIsNotNone(person)
            self.assertEqual(person.notes, "Primary collector")

    def test_people_delete_removes_a_record(self):
        self._login_as_admin()
        self._set_csrf_token()

        self.client.post(
            "/people/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "To Delete",
                "notes": "",
            },
            follow_redirects=False,
        )

        with self.app.app_context():
            person = Person.query.filter_by(name="To Delete").one()
            person_id = person.id

        response = self.client.post(
            f"/people/{person_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Person, person_id))

    def test_catalog_add_and_edit_item(self):
        self._login_as_admin()
        self._set_csrf_token()

        item, _variant = self._add_catalog_item()
        item_id = item.id

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

    def test_ownership_add_edit_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()

        item, variant = self._add_catalog_item(name="Ownership Knife", sku="OK-1")

        self.client.post(
            "/people/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Collector One",
                "notes": "",
            },
            follow_redirects=False,
        )
        with self.app.app_context():
            person = db.session.execute(db.select(Person).filter_by(name="Collector One")).scalar_one()

        add_response = self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person.id),
                "variant_id": str(variant.id),
                "status": "Owned",
                "target_price": "",
                "notes": "First ownership",
            },
            follow_redirects=False,
        )

        self.assertEqual(add_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person.id, variant_id=variant.id)
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

    def test_import_confirm_creates_ownership(self):
        self._login_as_admin()
        self._set_csrf_token()
        item, _variant = self._add_catalog_item(name="Import Knife", sku="IM-1")

        response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "0",
                "own_count": "1",
                "total_rows": "1",
                "own_accept_0": "on",
                "own_row_0": "2",
                "own_item_id_0": str(item.id),
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
                db.select(ItemVariant).filter_by(item_id=item.id, color="Classic Brown")
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(person_id=person.id, variant_id=variant.id)
            ).scalar_one()
            self.assertEqual(ownership.status, "Owned")
            self.assertEqual(ownership.notes, "Imported ownership")


if __name__ == "__main__":
    unittest.main()
