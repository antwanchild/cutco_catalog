# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class ImportSmokeTests(SmokeBaseTest):
    def test_import_template_downloads_csv(self):
        self._login_as_admin()
        response = self.client.get("/import/template")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn(
            "cutco_import_starter.csv", response.headers["Content-Disposition"]
        )

    def test_import_check_accepts_basic_csv(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "check",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\nParing Knife,1720,yes,Classic Brown\n"
                    ),
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
                "own_quantity_purchased_0": "3",
                "own_quantity_given_away_0": "1",
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
            person = db.session.execute(
                db.select(Person).filter_by(name="Importer")
            ).scalar_one()
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item_id, color="Classic Brown")
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(
                    person_id=person.id, variant_id=variant.id
                )
            ).scalar_one()
            self.assertEqual(ownership.status, "Owned")
            self.assertEqual(ownership.notes, "Imported ownership")
            self.assertEqual(ownership.quantity_purchased, 3)
            self.assertEqual(ownership.quantity_given_away, 1)

    def test_import_keeps_plain_and_engraved_copies_separate(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, variant_id = self._add_catalog_item(
            name="Imported Engraving Knife", sku="IM-ENG-1"
        )
        person_id = self._add_person(name="Engraved Importer", notes="")

        with self.app.app_context():
            db.session.add(
                Ownership(
                    person_id=person_id,
                    variant_id=variant_id,
                    status="Owned",
                    quantity_purchased=2,
                )
            )
            db.session.commit()

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,quantity purchased,copy_type,engraving_text\n"
                        b"Imported Engraving Knife,IM-ENG-1,Engraved Importer,Unknown / Unspecified,1,engraved,AC\n"
                    ),
                    "engraved-import.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Ownership Entries", preview_response.data)
        self.assertIn(b'name="own_accept_0"', preview_response.data)

        soup = BeautifulSoup(preview_response.data, "html.parser")
        confirm_payload = {"csrf_token": "test-csrf-token"}
        for field in soup.select('form[action="/import/confirm"] input'):
            name = field.get("name")
            if not name:
                continue
            if field.get("type") == "checkbox":
                if field.has_attr("checked"):
                    confirm_payload[name] = "on"
                continue
            confirm_payload[name] = field.get("value", "")

        confirm_response = self.client.post(
            "/import/confirm", data=confirm_payload, follow_redirects=False
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            ownerships = Ownership.query.filter_by(
                person_id=person_id, variant_id=variant_id
            ).all()
            self.assertEqual(len(ownerships), 2)
            plain = next(row for row in ownerships if row.copy_type == "plain")
            engraved = next(row for row in ownerships if row.copy_type == "engraved")
            self.assertEqual(plain.quantity_purchased, 2)
            self.assertEqual(engraved.quantity_purchased, 1)
            self.assertEqual(engraved.engraving_text, "AC")
            self.assertEqual(engraved.engraving_signature, "engraved:ac")

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
                "item_quantity_purchased_0": "2",
                "item_quantity_given_away_0": "1",
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
            item = db.session.execute(
                db.select(Item).filter_by(sku="IM-2")
            ).scalar_one()
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item.id, color="Pearl White")
            ).scalar_one()
            person = db.session.execute(
                db.select(Person).filter_by(name="Importer")
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(
                    person_id=person.id, variant_id=variant.id
                )
            ).scalar_one()

            self.assertEqual(item.notes, "Imported note")
            self.assertEqual(ownership.status, "Owned")
            self.assertEqual(ownership.quantity_purchased, 2)
            self.assertEqual(ownership.quantity_given_away, 1)

    def test_import_confirm_accumulates_existing_ownership_quantities(self):
        self._login_as_admin()
        self._set_csrf_token()

        first_response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "1",
                "own_count": "0",
                "total_rows": "1",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Upsert Knife",
                "item_sku_0": "UP-1",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Straight",
                "item_category_0": "Kitchen Knives",
                "item_notes_0": "",
                "item_person_0": "Upsert Collector",
                "item_status_0": "Owned",
                "item_quantity_purchased_0": "2",
                "item_quantity_given_away_0": "1",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )
        self.assertEqual(first_response.status_code, 200)

        second_response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "1",
                "own_count": "0",
                "total_rows": "1",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Upsert Knife",
                "item_sku_0": "UP-1",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Straight",
                "item_category_0": "Kitchen Knives",
                "item_notes_0": "",
                "item_person_0": "Upsert Collector",
                "item_status_0": "Owned",
                "item_quantity_purchased_0": "5",
                "item_quantity_given_away_0": "2",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(second_response.status_code, 200)
        with self.app.app_context():
            item = db.session.execute(
                db.select(Item).filter_by(sku="UP-1")
            ).scalar_one()
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item.id, color="Classic Brown")
            ).scalar_one()
            ownerships = (
                db.session.execute(
                    db.select(Ownership).filter_by(variant_id=variant.id)
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(ownerships), 1)
            ownership = ownerships[0]
            self.assertEqual(ownership.quantity_purchased, 7)
            self.assertEqual(ownership.quantity_given_away, 3)

    def test_import_confirm_marks_non_catalog_items_off_catalog(self):
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
                "item_name_0": "Rep Only Piece",
                "item_sku_0": "RO-1",
                "item_color_0": "Stainless",
                "item_edge_0": "Unknown",
                "item_category_0": "Cookware",
                "item_non_catalog_0": "on",
                "item_notes_0": "",
                "item_person_0": "",
                "item_status_0": "Owned",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            item = db.session.execute(
                db.select(Item).filter_by(sku="RO-1")
            ).scalar_one()
            self.assertFalse(item.in_catalog)
            self.assertEqual(item.availability, "non-catalog")
            self.assertEqual(
                [variant.color for variant in item.variants], [UNKNOWN_COLOR]
            )

    def test_import_confirm_normalizes_stainless_title_to_unknown_variant(self):
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
                "item_name_0": "1952 Stainless Salad Fork",
                "item_sku_0": "1952",
                "item_color_0": "Stainless",
                "item_edge_0": "Unknown",
                "item_category_0": "Kitchen Knives",
                "item_notes_0": "",
                "item_person_0": "",
                "item_status_0": "Owned",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            item = db.session.execute(
                db.select(Item).filter_by(sku="1952")
            ).scalar_one()
            self.assertEqual(item.name, "1952 Stainless Salad Fork")
            self.assertEqual(
                [variant.color for variant in item.variants], [UNKNOWN_COLOR]
            )

    def test_import_confirm_adds_duplicate_same_variant_quantities_together(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _ = self._add_catalog_item(name="Carving Fork", sku="1733")

        response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "2",
                "own_count": "0",
                "total_rows": "2",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Carving Fork",
                "item_sku_0": "1733",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Unknown",
                "item_category_0": "Kitchen Knives",
                "item_person_0": "Collector One",
                "item_status_0": "Owned",
                "item_quantity_purchased_0": "2",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "item_notes_0": "",
                "item_accept_1": "on",
                "item_row_1": "3",
                "item_name_1": "Carving Fork",
                "item_sku_1": "1733",
                "item_color_1": "Classic Brown",
                "item_edge_1": "Unknown",
                "item_category_1": "Kitchen Knives",
                "item_person_1": "Collector One",
                "item_status_1": "Owned",
                "item_quantity_purchased_1": "3",
                "item_sku_unicorn_1": "",
                "item_variant_unicorn_1": "",
                "item_edge_unicorn_1": "",
                "item_notes_1": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            classic_variant = next(
                variant for variant in item.variants if variant.color == "Classic Brown"
            )
            ownership = Ownership.query.filter_by(variant_id=classic_variant.id).one()
            self.assertEqual(ownership.quantity_purchased, 5)
            self.assertEqual(ownership.person.name, "Collector One")

    def test_import_confirm_keeps_existing_name_for_matching_sku(self):
        self._login_as_admin()
        self._set_csrf_token()

        existing_item_id, _existing_variant_id = self._add_catalog_item(
            name="Original Knife", sku="IM-KEEP-1"
        )

        response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "1",
                "own_count": "0",
                "total_rows": "1",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Imported Different Name",
                "item_sku_0": "IM-KEEP-1",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Straight",
                "item_category_0": "Kitchen Knives",
                "item_notes_0": "Should not rename",
                "item_person_0": "",
                "item_status_0": "Owned",
                "item_sets_0": "",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Import complete", response.data)
        with self.app.app_context():
            item = db.session.get(Item, existing_item_id)
            self.assertEqual(item.name, "Original Knife")
            self.assertEqual(item.sku, "IM-KEEP-1")

    def test_import_confirm_keeps_existing_category_for_matching_sku(self):
        self._login_as_admin()
        self._set_csrf_token()

        existing_item_id, _existing_variant_id = self._add_catalog_item(
            name="Category Knife",
            sku="IM-CAT-1",
            category="Kitchen Knives",
        )

        response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "item_count": "1",
                "own_count": "0",
                "total_rows": "1",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Category Knife",
                "item_sku_0": "IM-CAT-1",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Straight",
                "item_category_0": "Cookware",
                "item_notes_0": "Should not change category",
                "item_person_0": "",
                "item_status_0": "Owned",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "error_count": "0",
                "conflict_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Import complete", response.data)
        with self.app.app_context():
            item = db.session.get(Item, existing_item_id)
            self.assertEqual(item.category, "Kitchen Knives")

    def test_import_keeps_same_name_rows_with_different_skus_separate(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\n"
                        b"Duplicate Knife,DN-1,yes,Classic Brown\n"
                        b"Duplicate Knife,DN-2,yes,Pearl White\n"
                    ),
                    "duplicates.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"New Catalog Items (2)", response.data)
        self.assertIn(b"DN-1", response.data)
        self.assertIn(b"DN-2", response.data)
        self.assertIn(b"Import matching prefers SKU first.", response.data)

        soup = BeautifulSoup(response.data, "html.parser")
        item_count_input = soup.select_one('input[name="item_count"]')
        self.assertIsNotNone(item_count_input)
        self.assertEqual(item_count_input["value"], "2")

        confirm_response = self.client.post(
            "/import/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "total_rows": "2",
                "error_count": "0",
                "conflict_count": "0",
                "item_count": "2",
                "own_count": "0",
                "item_accept_0": "on",
                "item_row_0": "2",
                "item_name_0": "Duplicate Knife",
                "item_sku_0": "DN-1",
                "item_color_0": "Classic Brown",
                "item_edge_0": "Unknown",
                "item_category_0": "Kitchen Knives",
                "item_availability_0": "public",
                "item_sku_unicorn_0": "",
                "item_variant_unicorn_0": "",
                "item_edge_unicorn_0": "",
                "item_person_0": "",
                "item_status_0": "Owned",
                "item_notes_0": "",
                "item_accept_1": "on",
                "item_row_1": "3",
                "item_name_1": "Duplicate Knife",
                "item_sku_1": "DN-2",
                "item_color_1": "Pearl White",
                "item_edge_1": "Unknown",
                "item_category_1": "Kitchen Knives",
                "item_availability_1": "public",
                "item_sku_unicorn_1": "",
                "item_variant_unicorn_1": "",
                "item_edge_unicorn_1": "",
                "item_person_1": "",
                "item_status_1": "Owned",
                "item_notes_1": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        self.assertIn(b"Import complete", confirm_response.data)
        with self.app.app_context():
            first_item = db.session.execute(
                db.select(Item).filter_by(sku="DN-1")
            ).scalar_one()
            second_item = db.session.execute(
                db.select(Item).filter_by(sku="DN-2")
            ).scalar_one()
            self.assertEqual(first_item.name, "Duplicate Knife")
            self.assertEqual(second_item.name, "Duplicate Knife")
            self.assertNotEqual(first_item.id, second_item.id)

    def test_import_preview_keeps_duplicate_sku_rows_visible(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\n"
                        b"Carving Fork,1733,yes,Classic Brown\n"
                        b"Carving Fork,1733,yes,Classic Brown\n"
                    ),
                    "duplicate_sku.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"New Catalog Items (2)", response.data)
        self.assertIn(b"1733", response.data)
        self.assertIn(b"import-sku-group", response.data)
        self.assertIn(b"Import matching prefers SKU first.", response.data)

    def test_import_preview_groups_same_sku_rows_and_lists_variants(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\n"
                        b"Carving Fork,1733,yes,Classic Brown\n"
                        b"Carving Fork,1733,yes,Pearl White\n"
                    ),
                    "grouped_variants.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"New Catalog Items (2)", response.data)
        self.assertIn(b"import-sku-group", response.data)
        self.assertIn(b"Keep", response.data)
        self.assertIn(b"Classic Brown", response.data)
        self.assertIn(b"Pearl White", response.data)
        self.assertIn(b'name="item_row_0" value="2"', response.data)
        self.assertIn(b'name="item_row_1" value="3"', response.data)
        self.assertIn(b"Same SKU rows are grouped together here", response.data)

    def test_import_preview_groups_same_sku_ownership_rows(self):
        self._login_as_admin()
        self._set_csrf_token()
        self._add_catalog_item(name="Ownership Group Knife", sku="OG-1")

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\n"
                        b"Ownership Group Knife,OG-1,Anthony,Classic Brown\n"
                        b"Ownership Group Knife,OG-1,Anthony,Pearl White\n"
                    ),
                    "ownership_grouped.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ownership Entries (2)", response.data)
        self.assertIn(b"import-sku-group", response.data)
        self.assertIn(b"Keep", response.data)
        self.assertIn(b"Classic Brown", response.data)
        self.assertIn(b"Pearl White", response.data)
        self.assertIn(b"Row 2", response.data)
        self.assertIn(b"Row 3", response.data)

    def test_import_preview_renders_xlsx_rows(self):
        self._login_as_admin()
        self._set_csrf_token()
        self._add_catalog_item(name="Preview Knife", sku="PR-1")
        self._add_person(name="Anthony", notes="")

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(
            [
                "Name",
                "Model #",
                "COLOR",
                "Owned?",
                "availability",
                "person",
                "Price",
                "Gift Box",
                "Sheath",
                "Quantity Purchased",
                "Given Away",
                "Beast",
            ]
        )
        sheet.append(
            [
                "Preview Knife",
                "PR-1",
                "Classic Brown",
                "Anthony",
                "rep",
                "",
                "12.50",
                "yes",
                "Leather",
                "2",
                "1",
                "",
            ]
        )
        sheet.append(
            [
                "Preview New Knife",
                "PN-1",
                "Pearl White",
                "Wishlist",
                "non-catalog",
                "Collector Two",
                "34.00",
                "",
                "",
                "4",
                "2",
                "",
            ]
        )
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
        self.assertIn(b"<th>SKU</th>", response.data)
        self.assertIn(b"Price: 12.50", response.data)
        self.assertIn(b"Rep only", response.data)
        self.assertIn(b"badge-off-catalog", response.data)
        self.assertIn(b"item + own", response.data)
        self.assertIn(b'item_quantity_purchased_0" value="4"', response.data)
        self.assertIn(b'item_quantity_given_away_0" value="2"', response.data)
        self.assertIn(b'own_quantity_purchased_0" value="2"', response.data)
        self.assertIn(b'own_quantity_given_away_0" value="1"', response.data)
        self.assertIn(b"stacked-head", response.data)
        self.assertIn(b"Purchased", response.data)
        self.assertIn(b"Given Away", response.data)
        self.assertNotIn(b"Gift Box:", response.data)
        self.assertNotIn(b"Sheath:", response.data)

    def test_import_preview_rejects_decimal_quantity_values(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,quantity purchased,quantity given away\n"
                        b"Decimal Knife,DM-1,yes,Classic Brown,2.5,1.25\n"
                    ),
                    "decimal_qty.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Quantity Purchased must be a whole number.", response.data)
        self.assertIn(b"Quantity Given Away must be a whole number.", response.data)

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
        self.assertIn(
            b"Recommended header order: <code>name,sku,owned,color,availability,quantity purchased,quantity given away,category,edge,copy_type,engraved,engraving_text,engraving_notes,is_sku_unicorn,is_variant_unicorn,is_edge_unicorn,set_members,price</code>",
            response.data,
        )
        self.assertIn(b"Missing required headers: name", response.data)
        self.assertIn(b"No ownership/status column found", response.data)

    def test_import_accepts_item_name_alias_for_name(self):
        self._login_as_admin()
        self._set_csrf_token()

        response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"item_name,sku,owned,color\n"
                        b"Alias Name Knife,ALIAS-1,yes,Classic Brown\n"
                    ),
                    "alias_name.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"New Catalog Items (1)", response.data)
        self.assertIn(b"Alias Name Knife", response.data)

    def test_import_page_lists_non_catalog_column(self):
        self._login_as_admin()

        response = self.client.get("/import")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Start Here", response.data)
        self.assertIn(b"Assign all rows to", response.data)
        self.assertIn(b"Import Column Mapping", response.data)
        self.assertIn(b"<code>availability</code>", response.data)
        self.assertIn(
            b"<code>name,sku,owned,color,availability,quantity purchased,quantity given away,category,edge,copy_type,engraved,engraving_text,engraving_notes,is_sku_unicorn,is_variant_unicorn,is_edge_unicorn,set_members,price</code>",
            response.data,
        )
        self.assertIn(b"Rep only", response.data)
        self.assertIn(b"Costco", response.data)

    def test_completion_import_page_renders(self):
        self._login_as_admin()

        response = self.client.get("/completion-import")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"All SKU Completion Import", response.data)
        self.assertIn(b"Completion Overview", response.data)
        self.assertIn(b"Paste rows or drop CSV", response.data)
        self.assertIn(b"person, sku, quantity, note", response.data)
        self.assertIn(b"ordered from rep", response.data)
        self.assertIn(b"roll set SKUs into their member items", response.data)
        self.assertIn(b"Recent Completion Imports", response.data)

    def test_completion_gaps_page_renders_and_exports_missing_csv(self):
        self._login_as_admin()
        self._set_csrf_token()

        owned_item_id, owned_variant_id = self._add_catalog_item(
            name="Gap Owned", sku="GAP-1"
        )
        _missing_item_id, _missing_variant_id = self._add_catalog_item(
            name="Gap Missing", sku="GAP-2"
        )
        second_missing_item_id, _second_missing_variant_id = self._add_catalog_item(
            name="Gap Missing Other",
            sku="A-GAP-3",
            category="Accessories",
        )
        person_id = self._add_person(name="Gap Collector", notes="")

        with self.app.app_context():
            item = db.session.get(Item, owned_item_id)
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(
                    item_id=item.id, color="Unknown / Unspecified"
                )
            ).scalar_one()
            db.session.add(
                Ownership(
                    person_id=person_id,
                    variant_id=variant.id,
                    status="Owned",
                    quantity_purchased=1,
                )
            )
            db.session.commit()

        self.assertEqual(
            self.client.get(f"/people/{person_id}/collection").status_code, 200
        )
        response = self.client.get("/completion-gaps")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Completion Gaps", response.data)
        self.assertIn(b"View on screen", response.data)
        self.assertIn(b"Download missing SKUs CSV", response.data)
        self.assertIn(f'<option value="{person_id}" selected>'.encode(), response.data)

        screen_response = self.client.get(
            f"/completion-gaps?view=screen&person_id={person_id}"
        )
        self.assertEqual(screen_response.status_code, 200)
        self.assertIn(b"Screen View", screen_response.data)
        self.assertIn(b"Copy CSV", screen_response.data)
        self.assertIn(b"Gap Collector", screen_response.data)
        self.assertIn(b"GAP-2", screen_response.data)
        self.assertIn(b"A-GAP-3", screen_response.data)
        self.assertNotIn(b"GAP-1", screen_response.data)
        self.assertLess(
            screen_response.data.index(b"A-GAP-3"), screen_response.data.index(b"GAP-2")
        )

        export_response = self.client.post(
            "/completion-gaps",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response.mimetype, "text/csv")
        self.assertIn(
            "cutco_completion_gaps_", export_response.headers["Content-Disposition"]
        )
        self.assertIn(b"person,missing_sku,item,category", export_response.data)
        self.assertLess(
            export_response.data.index(b"A-GAP-3"), export_response.data.index(b"GAP-2")
        )
        self.assertIn(b"Gap Collector,GAP-2,Gap Missing", export_response.data)
        self.assertIn(
            b"Gap Collector,A-GAP-3,Gap Missing Other,Accessories", export_response.data
        )
        self.assertNotIn(b"GAP-1", export_response.data)

    def test_completion_import_rolls_up_set_members_and_updates_ownership(self):
        self._login_as_admin()
        self._set_csrf_token()

        member_item_id, _member_variant_id = self._add_catalog_item(
            name="Completion Knife",
            sku="COMP-1",
        )
        other_item_id, _other_variant_id = self._add_catalog_item(
            name="Completion Fork",
            sku="COMP-2",
        )
        _missing_item_id, _missing_variant_id = self._add_catalog_item(
            name="Completion Missing",
            sku="COMP-MISS",
        )
        self._add_set(
            name="Completion Set",
            sku="COMP-SET",
            item_ids=(member_item_id, other_item_id),
        )
        person_id = self._add_person(name="Completion Collector", notes="")

        with self.app.app_context():
            item = db.session.get(Item, member_item_id)
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(
                    item_id=item.id, color="Unknown / Unspecified"
                )
            ).scalar_one()
            db.session.add(
                Ownership(
                    person_id=person_id,
                    variant_id=variant.id,
                    status="Owned",
                    quantity_purchased=1,
                )
            )
            db.session.commit()

        preview_response = self.client.post(
            "/completion-import",
            data={
                "csrf_token": "test-csrf-token",
                "rows_text": (
                    "person,sku,color,quantity,note\n"
                    "Completion Collector,COMP-1,,2,direct order\n"
                    "Completion Collector,COMP-SET,Pearl,2,from set\n"
                    "Completion Collector,NOPE-1,,1,missing sku\n"
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"All SKU Completion Import Preview", preview_response.data)
        self.assertIn(b"Preview Summary", preview_response.data)
        self.assertIn(b"Rolled-Up Totals", preview_response.data)
        self.assertIn(b"Unresolved SKUs", preview_response.data)
        self.assertIn(b"Update ownership", preview_response.data)
        self.assertIn(b"Create ownership", preview_response.data)
        self.assertIn(b"Set member", preview_response.data)
        self.assertIn(b"Pearl", preview_response.data)
        self.assertIn(b"Item SKU not found", preview_response.data)
        self.assertIn(b"Download rolled-up CSV", preview_response.data)

        soup = BeautifulSoup(preview_response.data, "html.parser")
        confirm_payload = {"csrf_token": "test-csrf-token"}
        for inp in soup.select('form[action="/completion-import/confirm"] input'):
            name = inp.get("name")
            if not name:
                continue
            if inp.get("type") == "checkbox":
                if inp.has_attr("checked"):
                    confirm_payload[name] = "on"
                continue
            confirm_payload[name] = inp.get("value", "")

        export_payload = {"csrf_token": "test-csrf-token"}
        for inp in soup.select('form[action="/completion-import/export"] input'):
            name = inp.get("name")
            if not name:
                continue
            export_payload[name] = inp.get("value", "")

        confirm_response = self.client.post(
            "/completion-import/confirm",
            data=confirm_payload,
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        self.assertIn(b"Completion Import Result", confirm_response.data)
        self.assertIn(b"Import Summary", confirm_response.data)
        self.assertIn(b"Rows processed", confirm_response.data)
        self.assertIn(b"mostly created new ownership entries", confirm_response.data)
        self.assertIn(b"Ownership entries updated", confirm_response.data)
        self.assertIn(b"Ownership entries created", confirm_response.data)
        self.assertIn(b"Download rolled-up CSV", confirm_response.data)
        self.assertIn(b"Missing Catalog SKUs", confirm_response.data)
        self.assertIn(b"Download missing SKUs CSV", confirm_response.data)

        with self.app.app_context():
            for item_id in (member_item_id, other_item_id):
                pearl_variant = db.session.execute(
                    db.select(ItemVariant).filter_by(item_id=item_id, color="Pearl")
                ).scalar_one()
                pearl_ownership = db.session.execute(
                    db.select(Ownership).filter_by(
                        person_id=person_id, variant_id=pearl_variant.id
                    )
                ).scalar_one()
                self.assertEqual(pearl_ownership.quantity_purchased, 2)

        confirm_soup = BeautifulSoup(confirm_response.data, "html.parser")
        missing_payload = {"csrf_token": "test-csrf-token"}
        for inp in confirm_soup.select(
            'form[action="/completion-import/missing-export"] input'
        ):
            name = inp.get("name")
            if not name:
                continue
            missing_payload[name] = inp.get("value", "")

        export_response = self.client.post(
            "/completion-import/export",
            data=export_payload,
            follow_redirects=False,
        )

        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(export_response.mimetype, "text/csv")
        self.assertIn(
            "attachment; filename=cutco_completion_result_",
            export_response.headers["Content-Disposition"],
        )
        self.assertIn(
            "person,sku,item,color,total_quantity,action,notes,source_rows",
            export_response.data.decode(),
        )
        self.assertIn(
            "Completion Collector,COMP-1,Completion Knife",
            export_response.data.decode(),
        )

        missing_response = self.client.post(
            "/completion-import/missing-export",
            data=missing_payload,
            follow_redirects=False,
        )

        self.assertEqual(missing_response.status_code, 200)
        self.assertEqual(missing_response.mimetype, "text/csv")
        self.assertIn(
            "attachment; filename=cutco_completion_missing_",
            missing_response.headers["Content-Disposition"],
        )
        self.assertIn(
            "person,missing_sku,item,category,availability",
            missing_response.data.decode(),
        )
        self.assertIn(
            "Completion Collector,COMP-MISS,Completion Missing",
            missing_response.data.decode(),
        )

        history_response = self.client.get("/completion-import")
        self.assertEqual(history_response.status_code, 200)
        self.assertIn(b"Recent Completion Imports", history_response.data)
        self.assertIn(b'href="/completion-import"', history_response.data)
        self.assertIn(b"Completion import complete", history_response.data)

        with self.app.app_context():
            item = db.session.get(Item, member_item_id)
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(
                    item_id=item.id, color="Unknown / Unspecified"
                )
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(
                    person_id=person_id, variant_id=variant.id
                )
            ).scalar_one()
            other_variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=other_item_id, color="Pearl")
            ).scalar_one()
            other_ownership = db.session.execute(
                db.select(Ownership).filter_by(
                    person_id=person_id, variant_id=other_variant.id
                )
            ).scalar_one()
            no_item_count = Item.query.filter_by(sku="NOPE-1").count()

        self.assertEqual(ownership.quantity_purchased, 3)
        self.assertEqual(other_ownership.quantity_purchased, 2)
        self.assertEqual(no_item_count, 0)

    def test_import_preview_warns_on_same_sku_different_name(self):
        self._login_as_admin()
        self._set_csrf_token()
        self._add_catalog_item(name="Original Knife", sku="IM-WARN-1")

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\n"
                        b"Imported Different Name,IM-WARN-1,yes,Classic Brown\n"
                    ),
                    "preview.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"SKU or alias already exists", preview_response.data)
        self.assertIn(b"Imported Different Name", preview_response.data)
        self.assertIn(b"Original Knife", preview_response.data)

    def test_import_preview_marks_unicorn_rows_off_catalog(self):
        self._login_as_admin()
        self._set_csrf_token()

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,is_sku_unicorn\n"
                        b"Unicorn Knife,UNI-1,yes,Classic Brown,x\n"
                    ),
                    "unicorn_off_catalog.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"badge-off-catalog", preview_response.data)
        self.assertIn(b"Unicorn Knife", preview_response.data)

    def test_import_preview_matches_alternate_sku(self):
        self._login_as_admin()
        self._set_csrf_token()
        self._add_catalog_item(
            name="Existing Pan",
            sku="PAN-1",
            alternate_skus="PAN-ALT, PAN-VENDOR",
        )

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,is_sku_unicorn\n"
                        b"Imported Pan,PAN-VENDOR,yes,Classic Brown,x\n"
                    ),
                    "alias_preview.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(
            b"row(s) matched existing catalog items and will update ownership only",
            preview_response.data,
        )
        self.assertIn(b"SKU or alias already exists", preview_response.data)
        self.assertIn(b"Imported Pan", preview_response.data)
        self.assertIn(b"Existing Pan", preview_response.data)
        self.assertIn(b"Non-catalog", preview_response.data)
        self.assertIn(b"badge badge-unicorn", preview_response.data)
        self.assertNotIn(b"New Catalog Items (1)", preview_response.data)

    def test_import_preview_flags_set_sku_collisions(self):
        self._login_as_admin()
        self._set_csrf_token()
        self._add_set(name="Cookware Set", sku="SET-COLLISION-990")

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,edge,category\n"
                        b"Collision Test Piece,SET-COLLISION-990,yes,Classic Brown,Straight,Cookware\n"
                    ),
                    "set_collision.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Set SKU Collisions", preview_response.data)
        self.assertIn(b"Set SKU", preview_response.data)
        self.assertIn(b"badge badge-warning", preview_response.data)
        self.assertIn(b"unchecked by default", preview_response.data)
        self.assertNotIn(b"New Catalog Items (1)", preview_response.data)

    def test_collection_import_expands_colored_set_into_member_ownership(self):
        self._login_as_admin()
        self._set_csrf_token()
        member_id, _variant_id = self._add_catalog_item(
            name="Traditional Gravy Ladle",
            sku="1573",
            category="Flatware",
        )
        self._add_set(
            name="6-Pc. Traditional Accessory Set",
            sku="1570",
            item_ids=(member_id,),
        )

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,person\n"
                        b"6-Pc. Traditional Accessory Set,1570,yes,Pearl,Set Collector\n"
                    ),
                    "colored_set.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Traditional Gravy Ladle", preview_response.data)
        self.assertIn(b"Pearl", preview_response.data)
        self.assertNotIn(b"Set SKU Collisions", preview_response.data)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        confirm_payload = {"csrf_token": "test-csrf-token"}
        for field in soup.select('form[action="/import/confirm"] input'):
            name = field.get("name")
            if not name:
                continue
            if field.get("type") == "checkbox":
                if field.has_attr("checked"):
                    confirm_payload[name] = "on"
                continue
            confirm_payload[name] = field.get("value", "")

        confirm_response = self.client.post(
            "/import/confirm", data=confirm_payload, follow_redirects=False
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            pearl_variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=member_id, color="Pearl")
            ).scalar_one()
            ownership = db.session.execute(
                db.select(Ownership).filter_by(variant_id=pearl_variant.id)
            ).scalar_one()
            self.assertEqual(ownership.person.name, "Set Collector")

    def test_import_preview_normalizes_color_display(self):
        self._login_as_admin()
        self._set_csrf_token()

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color\n"
                        b"Color Knife,IM-COLOR-1,yes,BLACK\n"
                        b"Unknown Knife,IM-COLOR-2,yes,\n"
                    ),
                    "colors.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Black", preview_response.data)
        self.assertIn(b"Unknown", preview_response.data)
        self.assertIn(
            b'name="item_color_1" value="Unknown / Unspecified"', preview_response.data
        )
        self.assertNotIn(b"BLACK", preview_response.data)

    def test_import_preview_hides_cookware_unknown_color(self):
        self._login_as_admin()
        self._set_csrf_token()

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,category\n"
                        b"Cookware Piece,CW-UNK,yes,,Cookware\n"
                    ),
                    "cookware_unknown.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Cookware Piece", preview_response.data)
        self.assertIn(b"\xe2\x80\x94", preview_response.data)
        self.assertIn(
            b'name="item_color_0" value="Unknown / Unspecified"', preview_response.data
        )
        self.assertNotIn(
            b"<td>\n          Unknown\n        </td>", preview_response.data
        )

    def test_import_preview_treats_cutting_board_as_edgeless(self):
        self._login_as_admin()
        self._set_csrf_token()

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,category\n"
                        b"Cutting Board Test,CB-EDGE-1,yes,Classic Brown,Cutting Board\n"
                    ),
                    "cutting_board.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Cutting Board Test", preview_response.data)
        self.assertNotIn(b'<select name="item_edge_0"', preview_response.data)
        self.assertIn(
            b'<input type="hidden" name="item_edge_0" value="N/A">',
            preview_response.data,
        )

    def test_import_preview_csv_and_error_paths(self):
        self._login_as_admin()
        self._set_csrf_token()

        existing_item_id, _existing_variant_id = self._add_catalog_item(
            name="Import Existing Knife", sku="IM-EX-1"
        )
        self._add_person(name="Import Existing Collector", notes="")

        preview_response = self.client.post(
            "/import",
            data={
                "mode": "preview",
                "csrf_token": "test-csrf-token",
                "csvfile": (
                    BytesIO(
                        b"name,sku,owned,color,availability,quantity purchased,quantity given away,category,edge,"
                        b"sku_unicorn,variant_unicorn,edge_unicorn,price,Gift Box,Sheath\n"
                        b"Import Existing Knife,IM-EX-1,Import Existing Collector,Classic Brown,public,2,n/a,Kitchen Knives,Straight,"
                        b"no,no,no,12.50,yes,Leather\n"
                        b"Import New Knife,IM-NEW-1,no,Pearl White,non-catalog,,,,x,x,x,34.00,,\n"
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
        self.assertNotIn(b"Gift Box:", preview_response.data)
        self.assertNotIn(b"Sheath:", preview_response.data)
        self.assertIn(b"badge-off-catalog", preview_response.data)

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
        self.assertIn(
            b"Could not read headers from this file", invalid_check_response.data
        )

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
