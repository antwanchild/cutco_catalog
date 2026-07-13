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

    def test_mark_copy_as_engraved_splits_plain_quantity(self):
        self._login_as_admin()
        self._set_csrf_token()
        _item_id, variant_id = self._add_catalog_item(
            name="Split Engraving Knife", sku="ENG-SPLIT"
        )
        person_id = self._add_person(name="Engraving Collector", notes="")

        with self.app.app_context():
            plain = Ownership(
                person_id=person_id,
                variant_id=variant_id,
                status="Owned",
                quantity_purchased=3,
            )
            db.session.add(plain)
            db.session.commit()
            ownership_id = plain.id

        form_response = self.client.get(f"/ownership/{ownership_id}/engrave")
        self.assertEqual(form_response.status_code, 200)
        self.assertIn(b"Mark Copy as Engraved", form_response.data)
        self.assertIn(b'max="3"', form_response.data)

        response = self.client.post(
            f"/ownership/{ownership_id}/engrave",
            data={
                "csrf_token": "test-csrf-token",
                "quantity": "1",
                "engraving_text": "Anthony",
                "engraving_notes": "Front blade",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            ownerships = Ownership.query.filter_by(
                person_id=person_id, variant_id=variant_id
            ).all()
            self.assertEqual(len(ownerships), 2)
            plain = next(row for row in ownerships if row.copy_type == "plain")
            engraved = next(row for row in ownerships if row.copy_type == "engraved")
            self.assertEqual(plain.quantity_purchased, 2)
            self.assertEqual(engraved.quantity_purchased, 1)
            self.assertEqual(engraved.engraving_text, "Anthony")
            self.assertEqual(engraved.engraving_notes, "Front blade")
            self.assertEqual(engraved.engraving_signature, "engraved:anthony")

    def test_mark_only_copy_as_engraved_converts_existing_entry(self):
        self._login_as_admin()
        self._set_csrf_token()
        _item_id, variant_id = self._add_catalog_item(
            name="Converted Engraving Knife", sku="ENG-CONVERT"
        )
        person_id = self._add_person(name="Conversion Collector", notes="")

        with self.app.app_context():
            plain = Ownership(
                person_id=person_id,
                variant_id=variant_id,
                status="Owned",
                quantity_purchased=1,
            )
            db.session.add(plain)
            db.session.commit()
            ownership_id = plain.id

        response = self.client.post(
            f"/ownership/{ownership_id}/engrave",
            data={
                "csrf_token": "test-csrf-token",
                "quantity": "1",
                "engraving_text": "One of One",
                "engraving_notes": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            ownerships = Ownership.query.filter_by(
                person_id=person_id, variant_id=variant_id
            ).all()
            self.assertEqual(len(ownerships), 1)
            self.assertEqual(ownerships[0].id, ownership_id)
            self.assertEqual(ownerships[0].copy_type, "engraved")
            self.assertEqual(ownerships[0].quantity_purchased, 1)

    def test_mark_copy_as_engraved_merges_matching_engraving(self):
        self._login_as_admin()
        self._set_csrf_token()
        _item_id, variant_id = self._add_catalog_item(
            name="Merged Engraving Knife", sku="ENG-MERGE"
        )
        person_id = self._add_person(name="Merge Collector", notes="")

        with self.app.app_context():
            plain = Ownership(
                person_id=person_id,
                variant_id=variant_id,
                status="Owned",
                quantity_purchased=1,
            )
            engraved = Ownership(
                person_id=person_id,
                variant_id=variant_id,
                status="Owned",
                quantity_purchased=2,
                copy_type="engraved",
                engraving_text="AC",
                engraving_signature="engraved:ac",
            )
            db.session.add_all([plain, engraved])
            db.session.commit()
            ownership_id = plain.id

        response = self.client.post(
            f"/ownership/{ownership_id}/engrave",
            data={
                "csrf_token": "test-csrf-token",
                "quantity": "1",
                "engraving_text": "  ac  ",
                "engraving_notes": "Updated placement",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            ownerships = Ownership.query.filter_by(
                person_id=person_id, variant_id=variant_id
            ).all()
            self.assertEqual(len(ownerships), 1)
            engraved = ownerships[0]
            self.assertEqual(engraved.copy_type, "engraved")
            self.assertEqual(engraved.quantity_purchased, 3)
            self.assertEqual(engraved.engraving_notes, "Updated placement")
