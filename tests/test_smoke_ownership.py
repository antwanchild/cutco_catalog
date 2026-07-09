# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *

class OwnershipSmokeTests(SmokeBaseTest):
    def test_ownership_add_edit_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()

        _item_id, variant_id = self._add_catalog_item(
            name="Ownership Knife", sku="OK-1"
        )
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
                "quantity_purchased": "2",
                "quantity_given_away": "1",
            },
            follow_redirects=False,
        )

        self.assertEqual(add_response.status_code, 302)
        with self.app.app_context():
            ownership = db.session.execute(
                db.select(Ownership).filter_by(
                    person_id=person_id, variant_id=variant_id
                )
            ).scalar_one()
            self.assertEqual(ownership.status, "Owned")
            self.assertEqual(ownership.notes, "First ownership")
            self.assertEqual(ownership.quantity_purchased, 2)
            self.assertEqual(ownership.quantity_given_away, 1)

        edit_response = self.client.post(
            f"/ownership/{ownership.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "status": "Wishlist",
                "target_price": "89.00",
                "notes": "Updated ownership",
                "quantity_purchased": "5",
                "quantity_given_away": "",
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
            self.assertEqual(ownership.quantity_purchased, 5)
            self.assertIsNone(ownership.quantity_given_away)

        delete_response = self.client.post(
            f"/ownership/{ownership.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Ownership, ownership.id))
