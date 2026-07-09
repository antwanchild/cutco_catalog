# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *

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
            person = db.session.execute(
                db.select(Person).filter_by(name="Anthony")
            ).scalar_one_or_none()
            self.assertIsNotNone(person)
            self.assertEqual(person.notes, "Primary collector")

    def test_people_bulk_status_and_purge_collection(self):
        self._login_as_admin()
        self._set_csrf_token()
        _item_id, variant_id = self._add_catalog_item(name="Bulk Knife", sku="BL-1")
        _second_item_id, second_variant_id = self._add_catalog_item(
            name="Bulk Knife Two", sku="BL-2"
        )
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
                db.select(Ownership).filter_by(
                    person_id=person_id, variant_id=variant_id
                )
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
                db.select(Ownership).filter_by(
                    person_id=person_id, variant_id=second_variant_id
                )
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
            remaining = (
                db.session.execute(db.select(Ownership).filter_by(person_id=person_id))
                .scalars()
                .all()
            )
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
