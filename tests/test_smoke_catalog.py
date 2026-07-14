# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class CatalogSmokeTests(SmokeBaseTest):
    def test_catalog_page_filters_and_forms_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(
            name="Filter Knife", sku="FL-1", category="Kitchen Knives"
        )
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            item.is_unicorn = True
            item.availability = "rep only"
            db.session.add(Item(name="Uncategorized Knife", sku="UC-1", category=None))
            db.session.add(
                Item(
                    name="Set Only Knife",
                    sku="SO-1",
                    category="Kitchen Knives",
                    set_only=True,
                    in_catalog=False,
                    availability="non-catalog",
                )
            )
            db.session.add(
                Item(
                    name="Off Catalog Knife",
                    sku="OC-1",
                    category="Kitchen Knives",
                    set_only=False,
                    in_catalog=False,
                    availability="non-catalog",
                )
            )
            db.session.commit()

        catalog_response = self.client.get(
            "/catalog?q=Filter&category=Kitchen+Knives&unicorn=1&sort=sku&dir=desc"
        )
        availability_response = self.client.get("/catalog?availability=rep+only")
        uncategorized_response = self.client.get("/catalog?category=__uncategorized__")
        set_only_response = self.client.get("/catalog?status=set_only")
        off_catalog_response = self.client.get("/catalog?status=off_catalog")
        non_catalog_response = self.client.get("/catalog?status=non_catalog")
        add_page_response = self.client.get("/catalog/add")
        edit_page_response = self.client.get(f"/catalog/{item_id}/edit")

        self.assertEqual(catalog_response.status_code, 200)
        self.assertIn(b"Filter Knife", catalog_response.data)
        self.assertIn(b"Rep only", catalog_response.data)
        self.assertEqual(availability_response.status_code, 200)
        self.assertIn(b"Filter Knife", availability_response.data)
        self.assertNotIn(b"Uncategorized Knife", availability_response.data)
        self.assertEqual(uncategorized_response.status_code, 200)
        self.assertIn(b"Uncategorized Knife", uncategorized_response.data)
        self.assertIn(b"Availability", add_page_response.data)
        self.assertIn(b"Set-only", set_only_response.data)
        self.assertIn(b"Set Only Knife", set_only_response.data)
        self.assertIn(b"Off Catalog Knife", off_catalog_response.data)
        self.assertIn(b"Set Only Knife", non_catalog_response.data)
        self.assertIn(b"Off Catalog Knife", non_catalog_response.data)
        self.assertEqual(add_page_response.status_code, 200)
        self.assertIn(b"Add Item", add_page_response.data)
        self.assertIn(b"Availability", add_page_response.data)
        self.assertEqual(edit_page_response.status_code, 200)
        self.assertIn(b"Filter Knife", edit_page_response.data)
        self.assertIn(b"Availability", edit_page_response.data)

        item_detail_response = self.client.get(f"/views/item/{item_id}")
        self.assertEqual(item_detail_response.status_code, 200)
        self.assertIn(b"Rep only", item_detail_response.data)

        search_response = self.client.get("/search?q=Filter")
        self.assertEqual(search_response.status_code, 200)
        self.assertIn(b"Rep only", search_response.data)

    def test_catalog_category_sort_uses_name_tiebreaker(self):
        self._login_as_admin()
        self._set_csrf_token()

        with self.app.app_context():
            db.session.add(
                Item(name="Beta Knife", sku="BT-1", category="Kitchen Knives")
            )
            db.session.add(
                Item(name="Alpha Knife", sku="AL-1", category="Kitchen Knives")
            )
            db.session.commit()

        response = self.client.get("/catalog?sort=category&dir=asc")
        self.assertEqual(response.status_code, 200)
        self.assertLess(
            response.data.find(b"Alpha Knife"), response.data.find(b"Beta Knife")
        )

    def test_catalog_edge_sort_uses_name_tiebreaker(self):
        self._login_as_admin()
        self._set_csrf_token()

        with self.app.app_context():
            db.session.add(
                Item(name="Beta Edge Knife", sku="BE-1", edge_type="Straight")
            )
            db.session.add(
                Item(name="Alpha Edge Knife", sku="AE-1", edge_type="Straight")
            )
            db.session.commit()

        response = self.client.get("/catalog?sort=edge_type&dir=asc")
        self.assertEqual(response.status_code, 200)
        self.assertLess(
            response.data.find(b"Alpha Edge Knife"),
            response.data.find(b"Beta Edge Knife"),
        )

    def test_catalog_variant_sort_uses_variant_count(self):
        self._login_as_admin()
        self._set_csrf_token()

        with self.app.app_context():
            item_id, _ = self._add_catalog_item(name="Beta Variant Knife", sku="BV-1")
            db.session.add(Item(name="Alpha Variant Knife", sku="AV-1"))
            db.session.commit()
            db.session.add(ItemVariant(item_id=item_id, color="Blue"))
            db.session.commit()

        response = self.client.get("/catalog?sort=variants&dir=desc")
        self.assertEqual(response.status_code, 200)
        self.assertLess(
            response.data.find(b"Beta Variant Knife"),
            response.data.find(b"Alpha Variant Knife"),
        )

    def test_catalog_validation_and_sort_fallbacks(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Variant Knife", sku="VR-1")
        cookware_item_id, cookware_variant_id = self._add_catalog_item(
            name="Cookware Variant", sku="CVR-1", category="Cookware"
        )
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
            data={
                "csrf_token": "test-csrf-token",
                "color": "Classic Brown",
                "notes": "",
            },
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
            data={
                "csrf_token": "test-csrf-token",
                "color": "Classic Brown",
                "notes": "",
            },
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

        set_detail_fallback = self.client.get(
            f"/sets/{set_id}?person=1&sort=bogus&dir=sideways"
        )
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

        add_page = self.client.get("/catalog/add")
        self.assertEqual(add_page.status_code, 200)
        self.assertIn(b"suggest-field", add_page.data)
        self.assertIn(b"Alternate SKUs", add_page.data)

        set_add_page = self.client.get("/sets/add")
        self.assertEqual(set_add_page.status_code, 200)
        self.assertGreaterEqual(set_add_page.data.count(b"suggest-field"), 2)
        self.assertIn(b"Add Set", set_add_page.data)
        self.assertIn(b"Back to Sets", set_add_page.data)

        item_id, _variant_id = self._add_catalog_item()

        edit_page = self.client.get(f"/catalog/{item_id}/edit")
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(b"suggest-field", edit_page.data)
        self.assertIn(b"Alternate SKUs", edit_page.data)
        self.assertNotIn(b'name="next"', edit_page.data)

        filtered_edit_page = self.client.get(
            f"/catalog/{item_id}/edit?next=/catalog?category=Kitchen+Knives"
        )
        self.assertEqual(filtered_edit_page.status_code, 200)
        self.assertIn(b'name="next"', filtered_edit_page.data)
        self.assertIn(b"/catalog?category=Kitchen Knives", filtered_edit_page.data)
        self.assertIn(
            b'href="/catalog?category=Kitchen Knives"', filtered_edit_page.data
        )

        alias_item_id, _alias_variant_id = self._add_catalog_item(
            name="Alias Pan",
            sku="PAN-1",
            alternate_skus="PAN-LEGACY, PAN-OLD",
        )
        alias_edit_page = self.client.get(f"/catalog/{alias_item_id}/edit")
        self.assertEqual(alias_edit_page.status_code, 200)
        self.assertIn(b"PAN-LEGACY, PAN-OLD", alias_edit_page.data)

        set_edit_setup = self.client.post(
            "/sets/add",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Set Suggestions",
                "sku": "SS-1",
                "notes": "For testing",
            },
            follow_redirects=False,
        )
        self.assertEqual(set_edit_setup.status_code, 302)
        with self.app.app_context():
            set_id = (
                db.session.execute(db.select(Set).filter_by(name="Set Suggestions"))
                .scalar_one()
                .id
            )

        set_edit_page = self.client.get(f"/sets/{set_id}/edit")
        self.assertEqual(set_edit_page.status_code, 200)
        self.assertGreaterEqual(set_edit_page.data.count(b"suggest-field"), 2)
        self.assertIn(b"Update Set", set_edit_page.data)
        self.assertIn(b"Back to Sets", set_edit_page.data)

        filtered_set_edit_page = self.client.get(
            f"/sets/{set_id}/edit?next=/sets?person=1"
        )
        self.assertEqual(filtered_set_edit_page.status_code, 200)
        self.assertIn(b'name="next"', filtered_set_edit_page.data)
        self.assertIn(b"/sets?person=1", filtered_set_edit_page.data)
        self.assertIn(b'href="/sets?person=1"', filtered_set_edit_page.data)

        edit_response = self.client.post(
            f"/catalog/{item_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "next": "/catalog?category=Kitchen Knives",
                "name": "Test Knife Updated",
                "sku": "TK-1",
                "category": "Kitchen Knives",
                "edge_type": "Straight",
                "cutco_url": "https://example.com/test-knife-updated",
                "notes": "Updated note",
                "is_unicorn": "on",
                "edge_is_unicorn": "on",
                "availability": "public",
            },
            follow_redirects=False,
        )

        self.assertEqual(edit_response.status_code, 302)
        self.assertIn("/catalog?category=Kitchen%20Knives", edit_response.location)
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertIsNotNone(item)
            self.assertEqual(item.name, "Test Knife Updated")
            self.assertEqual(item.cutco_url, "https://example.com/test-knife-updated")
            self.assertEqual(item.notes, "Updated note")
            self.assertTrue(item.is_unicorn)
            self.assertTrue(item.edge_is_unicorn)
            self.assertEqual(item.alternate_skus, None)

    def test_catalog_set_and_variant_management_routes(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="Set Knife", sku="1111")
        incomplete_item_id, incomplete_variant_id = self._add_catalog_item(
            name="Incomplete Knife", sku="3333"
        )
        person_id = self._add_person(name="Set Owner", notes="")

        ownership_response = self.client.post(
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
        self.assertEqual(ownership_response.status_code, 302)

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
            item_set = db.session.execute(
                db.select(Set).filter_by(name="Set Group")
            ).scalar_one()
            set_id = item_set.id
            item_set.member_data = json.dumps(
                {
                    "members": [
                        {"sku": "9999", "quantity": 1},
                        {"sku": "9998", "quantity": 1},
                    ]
                }
            )
            item_set.members.append(ItemSetMember(item_id=item_id, quantity=1))
            item_set.members.append(
                ItemSetMember(item_id=incomplete_item_id, quantity=1)
            )
            red_variant = db.session.get(ItemVariant, variant_id)
            if red_variant is not None:
                red_variant.color = "Red"
            clear_set = Set(
                name="Clear Set",
                sku="CS-1",
                member_data=json.dumps({"members": [{"sku": "1111", "quantity": 1}]}),
            )
            clear_set.members.append(ItemSetMember(item_id=item_id, quantity=1))
            incomplete_set = Set(
                name="Incomplete Set",
                sku="IS-1",
                member_data=json.dumps({"members": [{"sku": "3333", "quantity": 1}]}),
            )
            incomplete_set.members.append(
                ItemSetMember(item_id=incomplete_item_id, quantity=1)
            )
            db.session.add(clear_set)
            db.session.add(incomplete_set)
            db.session.commit()

        sets_page = self.client.get("/sets")
        set_detail_page = self.client.get(f"/sets/{set_id}")
        set_edit_page = self.client.get(f"/sets/{set_id}/edit")

        self.assertEqual(sets_page.status_code, 200)
        self.assertIn(b"Set Group", sets_page.data)
        self.assertIn(b"SG-1", sets_page.data)
        self.assertEqual(set_detail_page.status_code, 200)
        self.assertIn(b"Set Group", set_detail_page.data)
        self.assertIn(b"Set Overview", set_detail_page.data)
        self.assertIn(b"Top Colors", set_detail_page.data)
        self.assertIn(b"/variants?color=Red", set_detail_page.data)
        self.assertEqual(set_edit_page.status_code, 200)
        self.assertIn(b"Set Members", set_edit_page.data)
        self.assertIn("← Sets".encode(), set_detail_page.data)

        filtered_set_edit_page = self.client.get(
            f"/sets/{set_id}/edit?next=/sets?person=1"
        )
        self.assertEqual(filtered_set_edit_page.status_code, 200)
        self.assertIn(b'name="next"', filtered_set_edit_page.data)
        self.assertIn(b"/sets?person=1", filtered_set_edit_page.data)
        self.assertIn(b'href="/sets?person=1"', filtered_set_edit_page.data)

        filtered_sets_page = self.client.get("/sets?missing=1&incomplete=1")
        self.assertEqual(filtered_sets_page.status_code, 200)
        self.assertIn(
            b"next=/sets?missing%3D1%26incomplete%3D1", filtered_sets_page.data
        )

        filtered_set_detail_page = self.client.get(
            f"/sets/{set_id}?next=/sets?missing%3D1%26incomplete%3D1"
        )
        self.assertEqual(filtered_set_detail_page.status_code, 200)
        self.assertIn(
            b'href="/sets?missing=1&amp;incomplete=1"', filtered_set_detail_page.data
        )
        self.assertIn(
            f'href="/sets/{set_id}/edit?next=/sets?missing%3D1%26incomplete%3D1"'.encode(),
            filtered_set_detail_page.data,
        )

        missing_sets_page = self.client.get("/sets?missing=1")
        self.assertEqual(missing_sets_page.status_code, 200)
        self.assertIn(b"Set Group", missing_sets_page.data)
        self.assertNotIn(b"Clear Set", missing_sets_page.data)
        self.assertIn(b"Has items not in catalog", missing_sets_page.data)

        incomplete_sets_page = self.client.get("/sets?incomplete=1")
        self.assertEqual(incomplete_sets_page.status_code, 200)
        self.assertIn(b"Incomplete Set", incomplete_sets_page.data)
        self.assertNotIn(b"Clear Set", incomplete_sets_page.data)
        self.assertIn(b"Incomplete only", incomplete_sets_page.data)

        edit_response = self.client.post(
            f"/sets/{set_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "next": "/sets?person=1",
                "name": "Set Group Updated",
                "sku": "SG-2",
                "notes": "Updated set",
                "member_item_ids": [str(item_id)],
                f"member_qty_{item_id}": "2",
            },
            follow_redirects=False,
        )
        self.assertEqual(edit_response.status_code, 302)
        self.assertIn("/sets?person=1", edit_response.location)
        with self.app.app_context():
            item_set = db.session.get(Set, set_id)
            self.assertIsNotNone(item_set)
            self.assertEqual(item_set.name, "Set Group Updated")
            self.assertEqual(item_set.sku, "SG-2")
            self.assertEqual(item_set.notes, "Updated set")
            self.assertEqual(item_set.members[0].quantity, 2)

        add_variant_response = self.client.post(
            f"/catalog/{item_id}/variants/add",
            data={
                "csrf_token": "test-csrf-token",
                "color": "Pearl White",
                "notes": "Alt color",
            },
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

        single_item_id, single_variant_id = self._add_catalog_item(
            name="Single Variant Knife", sku="4444"
        )
        with self.app.app_context():
            variant = db.session.get(ItemVariant, single_variant_id)
            self.assertIsNotNone(variant)
            variant.color = "Purple"
            variant.notes = "Bogus import"
            variant.source = "manual"
            db.session.commit()

        single_variant_page = self.client.get(f"/catalog/{single_item_id}/variants")
        self.assertEqual(single_variant_page.status_code, 200)
        self.assertIn(b"Reset to Unknown", single_variant_page.data)
        self.assertNotIn(b"Delete variant", single_variant_page.data)

        reset_variant_response = self.client.post(
            f"/variants/{single_variant_id}/reset-unknown",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(reset_variant_response.status_code, 302)
        with self.app.app_context():
            variant = db.session.get(ItemVariant, single_variant_id)
            self.assertIsNotNone(variant)
            self.assertEqual(variant.color, UNKNOWN_COLOR)
            self.assertIsNone(variant.notes)
            self.assertEqual(variant.source, "fallback_unknown")

    def test_catalog_sync_preview_renders_with_mocked_scrapes(self):
        self._login_as_admin()
        self._set_csrf_token()

        self._add_catalog_item(name="Existing Sync Knife", sku="EX-1")
        existing_set_item_id, _existing_set_variant_id = self._add_catalog_item(
            name="Existing Sync Set Knife",
            sku="EXS-KNIFE-1",
        )
        self._add_set(
            name="Existing Sync Set", sku="EXS-1", item_ids=(existing_set_item_id,)
        )

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
                "name": "Existing Sync Set",
                "sku": "EXS-1",
                "url": "https://example.com/existing-set",
                "member_skus": ["EXS-KNIFE-1"],
                "member_quantities": {"EXS-KNIFE-1": 2},
                "member_entries": [
                    {
                        "sku": "EXS-KNIFE-1",
                        "name": "Existing Sync Set Knife",
                        "quantity": 2,
                    },
                ],
            },
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
            },
        ]

        with (
            mock.patch(
                "blueprints.catalog._CATALOG_SYNC_JOB_FILE",
                f"{self.temp_dir.name}/catalog_sync_job.json",
            ),
            mock.patch(
                "blueprints.catalog.scrape_catalog", return_value=(scraped_items, [])
            ),
            mock.patch("blueprints.catalog.scrape_sets", return_value=scraped_sets),
            mock.patch(
                "blueprints.catalog.scrape_item_specs",
                return_value={
                    "edge_type": "Straight",
                    "msrp": 49.99,
                    "blade_length": "4 in",
                    "overall_length": "8 in",
                    "weight": "1 lb",
                },
            ),
            mock.patch(
                "blueprints.catalog.scrape_item_variant_colors", return_value=()
            ),
            mock.patch(
                "blueprints.catalog._start_catalog_sync_background_job",
                side_effect=catalog_blueprint._run_catalog_sync_job,
            ),
        ):
            response = self.client.get("/catalog/sync?run=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Catalog Sync Preview", response.data)
        self.assertIn(
            b'data-submit-progress="Adding the selected catalog items', response.data
        )
        self.assertIn(b'data-submitting-label="Importing', response.data)
        self.assertIn(b"New Items", response.data)
        self.assertIn(b"New Sync Knife", response.data)
        self.assertIn(b"New Sets", response.data)
        self.assertIn(b"New Sync Set", response.data)
        self.assertIn(b"Set Quantity Updates", response.data)
        self.assertIn(b"Existing Sync Set", response.data)
        self.assertIn(b"quantity changed", response.data)
        self.assertIn(
            b"Create placeholder items for missing set members", response.data
        )
        self.assertIn(
            b'name="create_missing_set_members" value="on" checked', response.data
        )
        self.assertIn(
            b"Scrapes Cutco.com to discover new items and sets.", response.data
        )
        self.assertIn(b"Limited Edition", response.data)
        self.assertNotIn(b"EX-1 ,", response.data)
        self.assertIn(b"Not in catalog", response.data)

        soup = BeautifulSoup(response.data, "html.parser")
        members_cell = soup.find(string=lambda text: text and "New Sync Set" in text)
        self.assertIsNotNone(members_cell)
        members_row = members_cell.find_parent("tr")
        self.assertIsNotNone(members_row)
        self.assertIn("EX-1", members_row.get_text(" ", strip=True))
        self.assertIn("NS-1", members_row.get_text(" ", strip=True))
        self.assertIn("NS-2", members_row.get_text(" ", strip=True))
        self.assertIn(",", members_row.get_text(" ", strip=True))
        self.assertNotIn("Found in scrape", members_row.get_text(" ", strip=True))
        self.assertNotIn(
            "Will create a placeholder", members_row.get_text(" ", strip=True)
        )
        self.assertNotIn("Will be skipped", members_row.get_text(" ", strip=True))

    def test_flatware_treats_edge_as_n_a(self):
        self.assertEqual(
            normalize_edge_for_category("Flatware", "Double-D"),
            ("N/A", False),
        )

    def test_catalog_sync_idle_page_does_not_scrape_inline(self):
        self._login_as_admin()

        with (
            mock.patch("blueprints.catalog.scrape_catalog") as scrape_catalog_mock,
            mock.patch("blueprints.catalog.scrape_sets") as scrape_sets_mock,
        ):
            response = self.client.get("/catalog/sync")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Catalog sync hasn't been started yet", response.data)
        scrape_catalog_mock.assert_not_called()
        scrape_sets_mock.assert_not_called()

    def test_catalog_sync_run_uses_real_flask_app_for_background_job(self):
        self._login_as_admin()

        with (
            mock.patch(
                "blueprints.catalog._CATALOG_SYNC_JOB_FILE",
                f"{self.temp_dir.name}/catalog_sync_job.json",
            ),
            mock.patch(
                "blueprints.catalog._start_catalog_sync_background_job"
            ) as start_mock,
        ):
            response = self.client.get("/catalog/sync?run=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Catalog sync is running in the background", response.data)
        self.assertTrue(start_mock.called)
        app_arg = start_mock.call_args.args[0]
        self.assertIsInstance(app_arg, Flask)
        self.assertIs(app_arg, self.app)

    def test_catalog_sync_preview_hides_placeholder_option_when_nothing_is_missing(
        self,
    ):
        self._login_as_admin()
        self._set_csrf_token()

        self._add_catalog_item(name="Existing Sync Knife", sku="EX-1")
        self._add_catalog_item(name="Existing Sync Set Knife", sku="EXS-KNIFE-1")
        scraped_items = [
            {
                "name": "Existing Sync Knife",
                "sku": "EX-1",
                "category": "Kitchen Knives",
                "url": "https://example.com/existing-sync",
            },
        ]
        scraped_sets = [
            {
                "name": "Existing Sync Set",
                "sku": "EXS-1",
                "url": "https://example.com/existing-set",
                "member_skus": ["EXS-KNIFE-1"],
                "member_quantities": {"EXS-KNIFE-1": 1},
                "member_entries": [
                    {
                        "sku": "EXS-KNIFE-1",
                        "name": "Existing Sync Set Knife",
                        "quantity": 1,
                    },
                ],
            }
        ]

        with (
            mock.patch(
                "blueprints.catalog.scrape_catalog", return_value=(scraped_items, [])
            ),
            mock.patch("blueprints.catalog.scrape_sets", return_value=scraped_sets),
            mock.patch(
                "blueprints.catalog.scrape_item_specs",
                return_value={
                    "edge_type": "Straight",
                    "msrp": 49.99,
                    "blade_length": "4 in",
                    "overall_length": "8 in",
                    "weight": "1 lb",
                },
            ),
        ):
            response = self.client.get("/catalog/sync")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            b"Create placeholder items for missing set members", response.data
        )
        self.assertNotIn(
            b'name="create_missing_set_members" value="on" checked', response.data
        )

    def test_catalog_sync_reports_cutco_outage_when_no_data_returns(self):
        self._login_as_admin()
        self._set_csrf_token()

        with (
            mock.patch(
                "blueprints.catalog._CATALOG_SYNC_JOB_FILE",
                f"{self.temp_dir.name}/catalog_sync_job.json",
            ),
            mock.patch("blueprints.catalog.scrape_catalog", return_value=([], [])),
            mock.patch("blueprints.catalog.scrape_sets", return_value=[]),
            mock.patch(
                "blueprints.catalog._start_catalog_sync_background_job",
                side_effect=catalog_blueprint._run_catalog_sync_job,
            ),
        ):
            response = self.client.get("/catalog/sync?run=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Catalog sync failed:", response.data)
        self.assertIn(b"could not be reached", response.data.lower())

    def test_set_membership_preview_ignores_sku_alias_changes_when_names_match(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(name="Gift Box", sku="2130CD")

        with self.app.app_context():
            item_set = Set(name="Gift Set", sku="GS-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=1)
            )
            db.session.commit()

            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [{"sku": "2026D", "name": "Gift Box", "quantity": 1}],
                {
                    item.sku.upper(): item
                    for item in Item.query.filter(Item.sku.isnot(None)).all()
                },
                _build_member_name_lookup(
                    Item.query.filter(Item.sku.isnot(None)).all()
                ),
            )

        self.assertFalse(preview["has_changes"])
        self.assertEqual(preview["summary"], "No membership changes detected.")

    def test_set_membership_preview_ignores_quantity_suffix_name_formatting(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(
            name="1952 Stainless Salad Fork", sku="1952"
        )

        with self.app.app_context():
            item_set = Set(name="Salad Set", sku="SAL-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=12)
            )
            db.session.commit()

            catalog_items = Item.query.filter(Item.sku.isnot(None)).all()
            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [
                    {
                        "sku": "1952",
                        "name": "1952 Stainless Salad Fork (12)",
                        "quantity": 12,
                    }
                ],
                {item.sku.upper(): item for item in catalog_items},
                _build_member_name_lookup(catalog_items),
            )

        self.assertFalse(preview["has_changes"])
        self.assertEqual(preview["summary"], "No membership changes detected.")

    def test_set_membership_preview_ignores_word_quantity_suffix_name_formatting(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(
            name="1759 Table Knife", sku="1759"
        )

        with self.app.app_context():
            item_set = Set(name="Table Set", sku="TAB-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=4)
            )
            db.session.commit()

            catalog_items = Item.query.filter(Item.sku.isnot(None)).all()
            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [{"sku": "1759", "name": "1759 Table Knife (four)", "quantity": 4}],
                {item.sku.upper(): item for item in catalog_items},
                _build_member_name_lookup(catalog_items),
            )

        self.assertFalse(preview["has_changes"])
        self.assertEqual(preview["summary"], "No membership changes detected.")

    def test_set_membership_preview_sorts_members_by_sku(self):
        self._login_as_admin()
        self._set_csrf_token()

        first_item_id, _ = self._add_catalog_item(name="Small Knife", sku="84")
        second_item_id, _ = self._add_catalog_item(name="Cleaver", sku="1737")

        with self.app.app_context():
            item_set = Set(name="Sorted Set", sku="SORT-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=second_item_id, set_id=item_set.id, quantity=1)
            )
            db.session.add(
                ItemSetMember(item_id=first_item_id, set_id=item_set.id, quantity=2)
            )
            db.session.commit()

            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [
                    {"sku": "1737", "name": "Cleaver", "quantity": 1},
                    {"sku": "84", "name": "Small Knife", "quantity": 2},
                ],
            )

        self.assertEqual(
            [member["sku"] for member in preview["current_rows"]], ["84", "1737"]
        )
        self.assertEqual(
            [member["sku"] for member in preview["incoming_rows"]], ["84", "1737"]
        )

    def test_set_membership_preview_includes_resolution_notes(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(name="1737 Cleaver", sku="1737")

        with self.app.app_context():
            item_set = Set(name="Resolution Set", sku="RES-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=1)
            )
            db.session.commit()

            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [
                    {"sku": "1737", "name": "1737 Cleaver Only", "quantity": 1},
                    {"sku": "990C", "name": "990C", "quantity": 1},
                ],
                {
                    item.sku.upper(): item
                    for item in Item.query.filter(Item.sku.isnot(None)).all()
                },
                {},
            )

        added_notes = {
            row["sku"]: row.get("resolution_note")
            for row in preview["change_rows"]
            if row["action"] == "added"
        }
        self.assertIn("990C", added_notes)
        self.assertIn("placeholder", added_notes["990C"].lower())
        self.assertNotIn("1737", added_notes)

    def test_set_membership_preview_detects_different_items_with_same_name(self):
        self._login_as_admin()
        self._set_csrf_token()

        current_item_id, _ = self._add_catalog_item(name="Shear Utility", sku="1705D")
        self._add_catalog_item(name="Shear Utility", sku="2117D")

        with self.app.app_context():
            item_set = Set(name="Shear Set", sku="SHEAR-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=current_item_id, set_id=item_set.id, quantity=1)
            )
            db.session.commit()

            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [{"sku": "2117D", "name": "Shear Utility", "quantity": 1}],
                {
                    item.sku.upper(): item
                    for item in Item.query.filter(Item.sku.isnot(None)).all()
                },
                {},
            )

        self.assertTrue(preview["has_changes"])
        self.assertEqual(preview["added"], 1)
        self.assertEqual(preview["removed"], 1)
        self.assertEqual(
            sorted(
                row["sku"]
                for row in preview["change_rows"]
                if row["action"] in {"added", "removed"}
            ),
            ["1705D", "2117D"],
        )

    def test_set_membership_preview_shows_resolved_incoming_sku(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _ = self._add_catalog_item(name="Shear Utility", sku="1705D")

        with self.app.app_context():
            item_set = Set(name="Shear Set", sku="SHEAR-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=1)
            )
            db.session.commit()

            catalog_items = Item.query.filter(Item.sku.isnot(None)).all()
            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [{"sku": "2117D", "name": "Shear Utility", "quantity": 1}],
                {item.sku.upper(): item for item in catalog_items},
                _build_member_name_lookup(catalog_items),
            )

        self.assertFalse(preview["has_changes"])
        self.assertEqual(preview["incoming_rows"][0]["display_sku"], "1705D")
        self.assertEqual(preview["incoming_rows"][0]["source_sku"], "2117D")

    def test_set_membership_preview_handles_dict_name_lookup_matches(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _ = self._add_catalog_item(name="Shear Utility", sku="1705D")

        with self.app.app_context():
            item_set = Set(name="Shear Set", sku="SHEAR-1")
            db.session.add(item_set)
            db.session.flush()
            db.session.add(
                ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=1)
            )
            db.session.commit()

            preview = _build_set_membership_preview(
                db.session.get(Set, item_set.id),
                [{"sku": "2117D", "name": "Shear Utility", "quantity": 1}],
                {},
                {
                    "shear utility": {
                        "id": item_id,
                        "sku": "1705D",
                        "name": "Shear Utility",
                    }
                },
            )

        self.assertFalse(preview["has_changes"])
        self.assertEqual(preview["incoming_rows"][0]["item_id"], item_id)
        self.assertEqual(preview["incoming_rows"][0]["display_sku"], "1705D")

    def test_variant_sync_page_renders_and_creates_missing_variants(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _unknown_variant_id = self._add_catalog_item(
            name="Variant Sync Knife", sku="VS-1"
        )
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            db.session.add(ItemVariant(item_id=item.id, color="Classic Brown"))
            db.session.add(ItemVariant(item_id=item.id, color="Red"))
            db.session.commit()

        with mock.patch(
            "blueprints.data.scrape_item_variant_colors",
            return_value=("Classic Brown", "Pearl White"),
        ):
            page_response = self.client.get("/variant-sync")
            self.assertEqual(page_response.status_code, 200)
            self.assertIn(b"Variant Sync", page_response.data)
            self.assertIn(b"Entire catalog", page_response.data)
            self.assertIn(b"Preview Variants", page_response.data)
            self.assertIn(b"Back", page_response.data)

            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "selected",
                    "selected_skus": "VS-1",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Variant Sync Preview", preview_response.data)
        self.assertIn(b"Preview Summary", preview_response.data)
        self.assertIn(b"existing", preview_response.data)
        self.assertIn(b"create", preview_response.data)
        self.assertIn(b"not seen in sync", preview_response.data)

        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)
        preview_json = preview_json_input["value"]

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json,
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertIn(b"Variant Sync Result", confirm_response.data)
        self.assertIn(b"Sync Summary", confirm_response.data)
        self.assertIn(b"Variants created", confirm_response.data)
        self.assertIn(b"Variant colors detected", confirm_response.data)

    def test_variant_sync_replaces_unknown_only_variant_with_real_color(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _unknown_variant_id = self._add_catalog_item(
            name="Handle Mitt", sku="HM-2"
        )

        with mock.patch(
            "blueprints.data.scrape_item_variant_colors",
            return_value=("Blue",),
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "selected",
                    "selected_skus": "HM-2",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json_input["value"],
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertEqual([variant.color for variant in item.variants], ["Blue"])
            self.assertEqual(
                [variant.source for variant in item.variants], ["variant_sync"]
            )

    def test_variant_sync_discovers_set_only_variants_by_sku(self):
        self._login_as_admin()
        self._set_csrf_token()
        expected_url = (
            "https://www.cutco.com/p/traditional-flatware-accessories/"
            "1570W&view=product"
        )

        with self.app.app_context():
            item = Item(
                name="6-Pc. Traditional Accessory Set",
                sku="1570",
                set_only=True,
                in_catalog=False,
                availability="non-catalog",
            )
            db.session.add(item)
            db.session.flush()
            item_id = item.id
            db.session.add(ItemVariant(item=item, color="Pearl"))
            db.session.commit()

        def scrape_variants(url):
            return ("Pearl", "Classic") if url == expected_url else ()

        with (
            mock.patch(
                "blueprints.data.scrape_item_variant_colors",
                side_effect=scrape_variants,
            ) as variant_scraper,
            mock.patch(
                "blueprints.data_workflows.discover_cutco_item_page_url",
                return_value=expected_url,
            ) as url_discovery,
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "selected",
                    "selected_skus": "1570",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        url_discovery.assert_called_once_with("1570")
        self.assertIn(mock.call(expected_url), variant_scraper.call_args_list)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json_input["value"],
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertEqual(item.cutco_url, expected_url)
            self.assertEqual(
                sorted(variant.color for variant in item.variants),
                ["Classic", "Pearl"],
            )
            self.assertEqual(
                next(
                    variant.source
                    for variant in item.variants
                    if variant.color == "Classic"
                ),
                "variant_sync",
            )

    def test_variant_sync_adds_set_variants_to_member_items(self):
        from models import SetVariant

        self._login_as_admin()
        self._set_csrf_token()
        expected_url = "https://www.cutco.com/p/traditional-flatware-accessories/1570W"

        with self.app.app_context():
            member = Item(
                name="Traditional Gravy Ladle",
                sku="1573",
                category="Flatware",
                set_only=True,
                in_catalog=False,
                availability="non-catalog",
            )
            db.session.add(member)
            db.session.flush()
            member_id = member.id
            db.session.add(ItemVariant(item=member, color=UNKNOWN_COLOR))
            item_set = Set(name="6-Pc. Traditional Accessory Set", sku="1570")
            db.session.add(item_set)
            db.session.flush()
            set_id = item_set.id
            db.session.add(
                ItemSetMember(set_id=item_set.id, item_id=member.id, quantity=1)
            )
            db.session.commit()

        with (
            mock.patch(
                "blueprints.data.scrape_item_variant_colors",
                return_value=("Pearl", "Classic"),
            ),
            mock.patch(
                "blueprints.data_workflows.discover_cutco_item_page_url",
                return_value=expected_url,
            ),
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "selected",
                    "selected_skus": "1570",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Sets Variants", preview_response.data)
        self.assertIn(b"eligible member item", preview_response.data)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json_input["value"],
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            item_set = db.session.get(Set, set_id)
            member = db.session.get(Item, member_id)
            self.assertEqual(item_set.cutco_url, expected_url)
            self.assertEqual(
                sorted(variant.color for variant in item_set.variants),
                ["Classic", "Pearl"],
            )
            self.assertEqual(
                len(
                    db.session.execute(db.select(SetVariant).filter_by(set_id=set_id))
                    .scalars()
                    .all()
                ),
                2,
            )
            self.assertEqual(
                sorted(variant.color for variant in member.variants),
                ["Classic", "Pearl"],
            )
            self.assertTrue(
                all(variant.source == "set_variant_sync" for variant in member.variants)
            )

    def test_variant_sync_can_mark_purple_campaign_variants_as_unicorns(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _unknown_variant_id = self._add_catalog_item(
            name="Super Shears", sku="77"
        )

        with (
            mock.patch(
                "blueprints.data.scrape_item_variant_colors",
                return_value=(),
            ),
            mock.patch(
                "blueprints.data.scrape_purple_campaign_variants",
                return_value=(
                    {
                        "name": "Super Shears",
                        "promo_code": "77L",
                        "sku_hint": "77",
                        "color": "Purple",
                    },
                    {
                        "name": "Gift Set",
                        "promo_code": "1836LD",
                        "sku_hint": "1836",
                        "color": "Purple",
                    },
                    {
                        "name": "Package",
                        "promo_code": "3840LD",
                        "sku_hint": "3840",
                        "color": "Purple",
                    },
                ),
            ),
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "all",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Promo Variants", preview_response.data)
        self.assertIn(b"Mark purple promo variants as unicorns", preview_response.data)
        self.assertIn(
            b"Suppressed because this is a campaign bundle item", preview_response.data
        )
        self.assertIn(b"suppressed", preview_response.data)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json_input["value"],
                "mark_purple_variants_unicorn": "on",
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertEqual([variant.color for variant in item.variants], ["Purple"])
            self.assertEqual(
                [variant.source for variant in item.variants], ["variant_sync"]
            )
            self.assertEqual([variant.is_unicorn for variant in item.variants], [True])

    def test_variant_sync_creates_purple_sheath_variants(self):
        self._login_as_admin()
        self._set_csrf_token()

        santoku_item_id, _ = self._add_catalog_item(name='7" Santoku', sku="1766")
        santoku_sheath_item_id, _ = self._add_catalog_item(
            name='7" Santoku Sheath', sku="1766-2", category="Sheaths"
        )
        trimmer_item_id, _ = self._add_catalog_item(
            name="Santoku-Style Trimmer", sku="3721"
        )
        trimmer_sheath_item_id, _ = self._add_catalog_item(
            name="Santoku-Style Trimmer Sheath", sku="3721-2", category="Sheaths"
        )

        with (
            mock.patch(
                "blueprints.data.scrape_item_variant_colors",
                return_value=(),
            ),
            mock.patch(
                "blueprints.data.scrape_purple_campaign_variants",
                return_value=(
                    {
                        "name": '7" Santoku with Sheath',
                        "promo_code": "1766LSH",
                        "sku_hint": "1766",
                        "color": "Purple",
                    },
                    {
                        "name": "Santoku-Style Trimmer with Sheath",
                        "promo_code": "3721LSH",
                        "sku_hint": "3721",
                        "color": "Purple",
                    },
                ),
            ),
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "all",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"7&#34; Santoku Sheath", preview_response.data)
        self.assertIn(b"Santoku-Style Trimmer Sheath", preview_response.data)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json_input["value"],
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            santoku_item = db.session.get(Item, santoku_item_id)
            santoku_sheath_item = db.session.get(Item, santoku_sheath_item_id)
            trimmer_item = db.session.get(Item, trimmer_item_id)
            trimmer_sheath_item = db.session.get(Item, trimmer_sheath_item_id)
            self.assertEqual(
                [variant.color for variant in santoku_item.variants], ["Purple"]
            )
            self.assertEqual(
                [variant.source for variant in santoku_item.variants], ["variant_sync"]
            )
            self.assertEqual(
                [variant.color for variant in santoku_sheath_item.variants], ["Purple"]
            )
            self.assertEqual(
                [variant.source for variant in santoku_sheath_item.variants],
                ["variant_sync"],
            )
            self.assertEqual(
                [variant.color for variant in trimmer_item.variants], ["Purple"]
            )
            self.assertEqual(
                [variant.source for variant in trimmer_item.variants], ["variant_sync"]
            )
            self.assertEqual(
                [variant.color for variant in trimmer_sheath_item.variants], ["Purple"]
            )
            self.assertEqual(
                [variant.source for variant in trimmer_sheath_item.variants],
                ["variant_sync"],
            )

    def test_variant_sync_can_confirm_purple_section_only(self):
        self._login_as_admin()
        self._set_csrf_token()

        normal_item_id, _ = self._add_catalog_item(
            name="Normal Variant Knife", sku="NV-1"
        )
        promo_item_id, _ = self._add_catalog_item(name="Super Shears", sku="77")
        promo_sheath_item_id, _ = self._add_catalog_item(
            name="Super Shears Sheath", sku="77-2", category="Sheaths"
        )

        with (
            mock.patch(
                "blueprints.data.scrape_item_variant_colors",
                return_value=("Blue",),
            ),
            mock.patch(
                "blueprints.data.scrape_purple_campaign_variants",
                return_value=(
                    {
                        "name": "Super Shears",
                        "promo_code": "77L",
                        "sku_hint": "77",
                        "color": "Purple",
                    },
                    {
                        "name": "Super Shears with Sheath",
                        "promo_code": "77LSH",
                        "sku_hint": "77",
                        "color": "Purple",
                    },
                ),
            ),
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "all",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn(b"Confirm Purple Promo Only", preview_response.data)
        soup = BeautifulSoup(preview_response.data, "html.parser")
        preview_json_input = soup.select_one('input[name="preview_json"]')
        self.assertIsNotNone(preview_json_input)

        confirm_response = self.client.post(
            "/variant-sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "preview_json": preview_json_input["value"],
                "confirm_target": "promo",
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

        self.assertEqual(confirm_response.status_code, 200)
        with self.app.app_context():
            normal_item = db.session.get(Item, normal_item_id)
            promo_item = db.session.get(Item, promo_item_id)
            promo_sheath_item = db.session.get(Item, promo_sheath_item_id)
            self.assertEqual(
                [variant.color for variant in normal_item.variants], [UNKNOWN_COLOR]
            )
            self.assertEqual(
                [variant.color for variant in promo_item.variants], ["Purple"]
            )
            self.assertEqual(
                [variant.color for variant in promo_sheath_item.variants], ["Purple"]
            )

    def test_variant_sync_skips_cutting_boards(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _unknown_variant_id = self._add_catalog_item(
            name="Cutting Board Test",
            sku="CB-1",
            category="Cutting Boards",
        )

        with mock.patch("blueprints.data.scrape_item_variant_colors") as scrape_mock:
            page_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "selected",
                    "selected_skus": "CB-1",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(page_response.status_code, 200)
        self.assertIn(
            b"Cutting board items are treated as a single fallback variant.",
            page_response.data,
        )
        scrape_mock.assert_not_called()
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            self.assertEqual(len(item.variants), 1)

    def test_variant_sync_shows_fallback_only_variant(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _unknown_variant_id = self._add_catalog_item(
            name="Fallback Variant Knife", sku="FV-1"
        )
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            db.session.add(ItemVariant(item_id=item.id, color="Classic"))
            db.session.add(ItemVariant(item_id=item.id, color=UNKNOWN_COLOR))
            db.session.commit()

        with mock.patch(
            "blueprints.data.scrape_item_variant_colors",
            return_value=("Classic",),
        ):
            preview_response = self.client.post(
                "/variant-sync",
                data={
                    "csrf_token": "test-csrf-token",
                    "scope": "selected",
                    "selected_skus": "FV-1",
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertNotIn(b"fallback only", preview_response.data)
        self.assertIn(b"Classic", preview_response.data)

    def test_catalog_sync_uses_populates_tasks(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Use Sync Knife", sku="US-1")
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            item.cutco_url = "https://example.com/use-sync"
            db.session.commit()

        with mock.patch(
            "blueprints.catalog.scrape_item_uses",
            return_value=["Slice onions", "Peel potatoes"],
        ):
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

        existing_item_id, _existing_variant_id = self._add_catalog_item(
            name="Sync Existing Knife", sku="SX-EX-1"
        )
        stale_item_id, _stale_variant_id = self._add_catalog_item(
            name="Sync Stale Knife", sku="SX-STALE-1"
        )
        existing_set_id = self._add_set(
            name="Sync Existing Set",
            sku="SX-SET-1",
            item_ids=(existing_item_id, stale_item_id),
        )

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "selected_skus": ["SX-NEW-1"],
                "name_SX-NEW-1": "Sync New Knife",
                "category_SX-NEW-1": "Kitchen Knives",
                "url_SX-NEW-1": "https://example.com/sync-new",
                "edge_type_SX-NEW-1": "Straight",
                "item_unicorn_SX-NEW-1": "on",
                "msrp_SX-NEW-1": "not-a-number",
                "blade_length_SX-NEW-1": "4 in",
                "overall_length_SX-NEW-1": "8 in",
                "weight_SX-NEW-1": "1 lb",
                "variant_colors_SX-NEW-1": json.dumps(["Classic Blue"]),
                "selected_sets": ["Sync New Set"],
                "set_count": "1",
                "set_name_0": "Sync New Set",
                "set_sku_0": "SX-SET-NEW",
                "set_member_entries_0": json.dumps(
                    [
                        {"sku": "SX-NEW-1", "name": "Sync New Knife", "quantity": 2},
                        {"sku": "SX-NEW-1", "name": "Sync New Knife", "quantity": 1},
                        {
                            "sku": "SX-MISS-1",
                            "name": "Sync Missing Knife",
                            "quantity": 1,
                        },
                    ]
                ),
                "create_missing_set_members": "on",
                "existing_set_count": "1",
                "existing_set_name_0": "Sync Existing Set",
                "existing_set_member_entries_0": json.dumps(
                    [
                        {
                            "sku": "SX-EX-1",
                            "name": "Sync Existing Knife",
                            "quantity": 3,
                        },
                        {
                            "sku": "SX-EX-1",
                            "name": "Sync Existing Knife",
                            "quantity": 2,
                        },
                        {
                            "sku": "SX-EX-MISS-1",
                            "name": "Sync Existing Missing Knife",
                            "quantity": 1,
                        },
                    ]
                ),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            new_item = db.session.execute(
                db.select(Item).filter_by(sku="SX-NEW-1")
            ).scalar_one()
            self.assertIsNone(new_item.msrp)
            self.assertTrue(new_item.is_unicorn)
            self.assertEqual(new_item.availability, "non-catalog")
            self.assertFalse(new_item.in_catalog)
            self.assertEqual(
                [variant.color for variant in new_item.variants], ["Classic Blue"]
            )
            self.assertEqual(
                [variant.source for variant in new_item.variants], ["catalog_sync"]
            )
            new_set = db.session.execute(
                db.select(Set).filter_by(name="Sync New Set")
            ).scalar_one()
            self.assertEqual(len(new_set.members), 2)
            new_member_quantities = {
                db.session.get(Item, membership.item_id).sku: membership.quantity
                for membership in new_set.members
            }
            self.assertEqual(new_member_quantities["SX-NEW-1"], 3)
            created_member = db.session.execute(
                db.select(Item).filter_by(sku="SX-MISS-1")
            ).scalar_one()
            self.assertFalse(created_member.in_catalog)
            self.assertTrue(created_member.set_only)
            self.assertIsNotNone(new_set.member_data)
            self.assertIn("SX-MISS-1", new_set.member_data)
            existing_set = db.session.get(Set, existing_set_id)
            self.assertEqual(len(existing_set.members), 2)
            existing_member_quantities = {
                db.session.get(Item, membership.item_id).sku: membership.quantity
                for membership in existing_set.members
            }
            self.assertEqual(existing_member_quantities["SX-EX-1"], 5)
            created_existing_member = db.session.execute(
                db.select(Item).filter_by(sku="SX-EX-MISS-1")
            ).scalar_one()
            self.assertFalse(created_existing_member.in_catalog)
            self.assertTrue(created_existing_member.set_only)
            self.assertIsNotNone(existing_set.member_data)
            existing_member_skus = {
                db.session.get(Item, membership.item_id).sku
                for membership in existing_set.members
            }
            self.assertNotIn("SX-STALE-1", existing_member_skus)
            self.assertIn("SX-EX-1", existing_member_skus)
            self.assertIn("SX-EX-MISS-1", existing_member_skus)

    def test_missing_set_member_discovers_product_url_by_sku_for_variants(self):
        from blueprints.catalog_sync import _create_missing_set_member_item

        expected_url = (
            "https://www.cutco.com/p/traditional-flatware-accessories/"
            "1570W&view=product"
        )

        def scrape_variants(url):
            return ("Pearl", "Classic") if url == expected_url else ()

        with (
            self.app.app_context(),
            mock.patch(
                "blueprints.catalog_sync._resolve_cutco_item_page_url",
                side_effect=lambda url, **_kwargs: url,
            ),
            mock.patch(
                "blueprints.catalog_sync.scrape_item_variant_colors",
                side_effect=scrape_variants,
            ) as variant_scraper,
            mock.patch(
                "blueprints.catalog_sync.discover_cutco_item_page_url",
                return_value=expected_url,
            ) as url_discovery,
        ):
            item = _create_missing_set_member_item(
                {"sku": "1570W", "name": "6-Pc. Traditional Accessory Set"},
                "Traditional Flatware Set",
            )
            db.session.commit()

            self.assertEqual(item.cutco_url, expected_url)
            self.assertTrue(item.set_only)
            self.assertEqual(
                sorted(variant.color for variant in item.variants),
                ["Classic", "Pearl"],
            )
            self.assertEqual(
                [variant.source for variant in item.variants],
                ["catalog_sync", "catalog_sync"],
            )
            url_discovery.assert_called_once_with("1570W")
            self.assertIn(mock.call(expected_url), variant_scraper.call_args_list)

    def test_catalog_sync_does_not_backfill_existing_set_only_item_variants(self):
        from blueprints.catalog_sync import _aggregate_resolved_members

        with self.app.app_context():
            item = Item(
                name="Traditional Flatware Accessories",
                sku="1570W",
                set_only=True,
                in_catalog=False,
                availability="non-catalog",
            )
            db.session.add(item)
            db.session.flush()
            db.session.add(ItemVariant(item=item, color=UNKNOWN_COLOR))
            db.session.commit()

            with mock.patch(
                "blueprints.catalog_sync.scrape_item_variant_colors"
            ) as variant_scraper:
                resolved, created = _aggregate_resolved_members(
                    [
                        {
                            "sku": "1570W",
                            "name": "Traditional Flatware Accessories",
                            "quantity": 1,
                        }
                    ],
                    {"1570W": item},
                    {"traditional flatware accessories": item},
                )
                db.session.commit()

            self.assertEqual(created, 0)
            self.assertIn(item.id, resolved)
            self.assertIsNone(item.cutco_url)
            self.assertEqual(
                [variant.color for variant in item.variants], [UNKNOWN_COLOR]
            )
            variant_scraper.assert_not_called()

    def test_catalog_sync_preview_detects_variants_for_new_sets_only(self):
        from blueprints.catalog_sync import _build_catalog_sync_preview

        new_set_url = "https://www.cutco.com/p/traditional-flatware-accessories/1570W"
        with self.app.app_context():
            db.session.add(Set(name="Existing Set", sku="1571"))
            db.session.commit()

            with mock.patch(
                "blueprints.catalog_sync.scrape_item_variant_colors",
                return_value=("Pearl", "Classic"),
            ) as variant_scraper:
                preview = _build_catalog_sync_preview(
                    [],
                    [
                        {
                            "name": "6-Pc. Traditional Accessory Set",
                            "sku": "1570",
                            "url": new_set_url,
                            "member_entries": [],
                        },
                        {
                            "name": "Existing Set",
                            "sku": "1571",
                            "url": "https://example.com/existing-set",
                            "member_entries": [],
                        },
                    ],
                )

        self.assertEqual(len(preview["new_sets"]), 1)
        self.assertEqual(preview["new_sets"][0]["variant_colors"], ["Pearl", "Classic"])
        variant_scraper.assert_called_once_with(new_set_url)

    def test_catalog_sync_adds_variants_for_new_set_and_members_only(self):
        from models import SetVariant
        from startup import _ensure_unknown_variants

        self._login_as_admin()
        self._set_csrf_token()
        member_id, _unknown_variant_id = self._add_catalog_item(
            name="Traditional Gravy Ladle",
            sku="1573",
            category="Flatware",
        )
        set_url = "https://www.cutco.com/p/traditional-flatware-accessories/1570W"

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "set_count": "1",
                "selected_sets": "6-Pc. Traditional Accessory Set",
                "set_name_0": "6-Pc. Traditional Accessory Set",
                "set_sku_0": "1570",
                "set_url_0": set_url,
                "set_variant_colors_0": json.dumps(["Pearl", "Classic"]),
                "set_member_entries_0": json.dumps(
                    [
                        {
                            "sku": "1573",
                            "name": "Traditional Gravy Ladle",
                            "quantity": 1,
                        }
                    ]
                ),
                "existing_set_count": "0",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item_set = db.session.execute(
                db.select(Set).filter_by(sku="1570")
            ).scalar_one()
            member = db.session.get(Item, member_id)
            self.assertEqual(item_set.cutco_url, set_url)
            self.assertEqual(
                sorted(variant.color for variant in item_set.variants),
                ["Classic", "Pearl"],
            )
            self.assertEqual(
                len(
                    db.session.execute(
                        db.select(SetVariant).filter_by(set_id=item_set.id)
                    )
                    .scalars()
                    .all()
                ),
                2,
            )
            self.assertEqual(
                sorted(variant.color for variant in member.variants),
                ["Classic", "Pearl"],
            )
            _ensure_unknown_variants()
            db.session.commit()
            self.assertEqual(
                sorted(variant.color for variant in member.variants),
                ["Classic", "Pearl"],
            )

        with self.app.app_context():
            existing_set = Set(name="Existing Variant Set", sku="1571")
            db.session.add(existing_set)
            db.session.commit()
            existing_set_id = existing_set.id

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "set_count": "0",
                "existing_set_count": "1",
                "existing_set_name_0": "Existing Variant Set",
                "existing_set_member_entries_0": "[]",
                "set_variant_colors_0": json.dumps(["Pearl", "Classic"]),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            existing_set = db.session.get(Set, existing_set_id)
            self.assertEqual(existing_set.variants, [])

    def test_catalog_sync_confirm_reconciles_existing_set_members(self):
        self._login_as_admin()
        self._set_csrf_token()

        keep_item_id, _ = self._add_catalog_item(name="Keep Knife", sku="K-1")
        drop_item_id, _ = self._add_catalog_item(name="Drop Knife", sku="D-1")
        new_item_id, _ = self._add_catalog_item(name="New Knife", sku="N-1")
        existing_set_id = self._add_set(
            name="Sync Existing Set",
            sku="SX-SET-1",
            item_ids=(keep_item_id, drop_item_id),
        )

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "selected_skus": [],
                "selected_sets": ["Sync Existing Set"],
                "existing_set_count": "1",
                "existing_set_name_0": "Sync Existing Set",
                "existing_set_member_entries_0": json.dumps(
                    [
                        {"sku": "K-1", "name": "Keep Knife", "quantity": 2},
                        {"sku": "N-1", "name": "New Knife", "quantity": 1},
                    ]
                ),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            existing_set = db.session.get(Set, existing_set_id)
            member_qtys = {
                db.session.get(Item, member.item_id).sku: member.quantity
                for member in existing_set.members
            }
            self.assertEqual(member_qtys, {"K-1": 2, "N-1": 1})
            self.assertNotIn("D-1", member_qtys)
            self.assertIsNotNone(existing_set.member_data)
            self.assertIn("N-1", existing_set.member_data)

    def test_catalog_sync_confirm_preserves_existing_set_members_when_snapshot_is_empty(
        self,
    ):
        self._login_as_admin()
        self._set_csrf_token()

        existing_item_id, _existing_variant_id = self._add_catalog_item(
            name="Sync Existing Knife", sku="SX-EX-1"
        )
        stale_item_id, _stale_variant_id = self._add_catalog_item(
            name="Sync Stale Knife", sku="SX-STALE-1"
        )
        existing_set_id = self._add_set(
            name="Sync Existing Set",
            sku="SX-SET-1",
            item_ids=(existing_item_id, stale_item_id),
        )

        response = self.client.post(
            "/catalog/sync/confirm",
            data={
                "csrf_token": "test-csrf-token",
                "selected_skus": [],
                "selected_sets": ["Sync Existing Set"],
                "existing_set_count": "1",
                "existing_set_name_0": "Sync Existing Set",
                "existing_set_member_entries_0": json.dumps([]),
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            existing_set = db.session.get(Set, existing_set_id)
            self.assertEqual(len(existing_set.members), 2)
            existing_member_skus = {
                db.session.get(Item, membership.item_id).sku
                for membership in existing_set.members
            }
            self.assertIn("SX-EX-1", existing_member_skus)
            self.assertIn("SX-STALE-1", existing_member_skus)
            self.assertEqual(json.loads(existing_set.member_data), [])

    def test_restore_set_memberships_relinks_from_member_snapshot(self):
        self._login_as_admin()
        self._set_csrf_token()

        first_item_id, _first_variant_id = self._add_catalog_item(
            name="Restore Knife One", sku="RS-1"
        )
        second_item_id, _second_variant_id = self._add_catalog_item(
            name="Restore Knife Two", sku="RS-2"
        )
        set_id = self._add_set(
            name="Restore Set", sku="RS-SET", item_ids=(first_item_id, second_item_id)
        )

        with self.app.app_context():
            item_set = db.session.get(Set, set_id)
            item_set.member_data = json.dumps(
                {
                    "members": [
                        {"sku": "RS-1", "name": "Restore Knife One", "quantity": 2},
                        {"sku": "RS-2", "name": "Restore Knife Two", "quantity": 1},
                    ]
                }
            )
            for membership in list(item_set.members):
                db.session.delete(membership)
            db.session.commit()

        response = self.client.post(
            f"/sets/{set_id}/restore-memberships",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            item_set = db.session.get(Set, set_id)
            self.assertEqual(len(item_set.members), 2)
            qty_map = {
                db.session.get(Item, member.item_id).sku: member.quantity
                for member in item_set.members
            }
            self.assertEqual(qty_map["RS-1"], 2)
            self.assertEqual(qty_map["RS-2"], 1)
            self.assertIn("RS-1", item_set.member_data)
            self.assertIn("RS-2", item_set.member_data)

    def test_bulk_restore_set_memberships_relinks_selected_sets(self):
        self._login_as_admin()
        self._set_csrf_token()

        first_item_id, _first_variant_id = self._add_catalog_item(
            name="Bulk Restore Knife One", sku="BR-1"
        )
        second_item_id, _second_variant_id = self._add_catalog_item(
            name="Bulk Restore Knife Two", sku="BR-2"
        )
        first_set_id = self._add_set(
            name="Bulk Restore Set One", sku="BR-SET-1", item_ids=(first_item_id,)
        )
        second_set_id = self._add_set(
            name="Bulk Restore Set Two", sku="BR-SET-2", item_ids=(second_item_id,)
        )

        with self.app.app_context():
            first_set = db.session.get(Set, first_set_id)
            second_set = db.session.get(Set, second_set_id)
            first_set.member_data = json.dumps(
                {
                    "members": [
                        {"sku": "BR-1", "name": "Bulk Restore Knife One", "quantity": 1}
                    ]
                }
            )
            second_set.member_data = json.dumps(
                {
                    "members": [
                        {"sku": "BR-2", "name": "Bulk Restore Knife Two", "quantity": 2}
                    ]
                }
            )
            for item_set in (first_set, second_set):
                for membership in list(item_set.members):
                    db.session.delete(membership)
            db.session.commit()

        response = self.client.post(
            "/sets/bulk-restore-memberships",
            data={
                "csrf_token": "test-csrf-token",
                "next": "/sets?missing=1&incomplete=1",
                "set_ids": [str(first_set_id), str(second_set_id)],
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/sets?missing=1&incomplete=1", response.headers["Location"])

        with self.app.app_context():
            first_set = db.session.get(Set, first_set_id)
            second_set = db.session.get(Set, second_set_id)
            self.assertEqual(len(first_set.members), 1)
            self.assertEqual(len(second_set.members), 1)
            self.assertEqual(
                db.session.get(Item, first_set.members[0].item_id).sku, "BR-1"
            )
            self.assertEqual(
                db.session.get(Item, second_set.members[0].item_id).sku, "BR-2"
            )

    def test_bulk_resync_set_memberships_relinks_selected_sets_from_scrape(self):
        self._login_as_admin()
        self._set_csrf_token()

        first_item_id, _first_variant_id = self._add_catalog_item(
            name="Bulk Resync Knife One", sku="BS-1"
        )
        second_item_id, _second_variant_id = self._add_catalog_item(
            name="Bulk Resync Knife Two", sku="BS-2"
        )
        first_set_id = self._add_set(
            name="Bulk Resync Set One", sku="BS-SET-1", item_ids=(first_item_id,)
        )
        second_set_id = self._add_set(
            name="Bulk Resync Set Two", sku="BS-SET-2", item_ids=(second_item_id,)
        )

        with self.app.app_context():
            first_set = db.session.get(Set, first_set_id)
            second_set = db.session.get(Set, second_set_id)
            first_set.member_data = json.dumps(
                {
                    "members": [
                        {"sku": "BS-1", "name": "Bulk Resync Knife One", "quantity": 1}
                    ]
                }
            )
            second_set.member_data = json.dumps(
                {
                    "members": [
                        {"sku": "BS-2", "name": "Bulk Resync Knife Two", "quantity": 1}
                    ]
                }
            )
            for item_set in (first_set, second_set):
                for membership in list(item_set.members):
                    db.session.delete(membership)
            db.session.commit()

        with mock.patch(
            "blueprints.catalog.scrape_sets",
            return_value=[
                {
                    "name": "Bulk Resync Set One",
                    "sku": "BS-SET-1",
                    "member_entries": [
                        {"sku": "BS-1", "name": "Bulk Resync Knife One", "quantity": 2},
                    ],
                },
                {
                    "name": "Bulk Resync Set Two",
                    "sku": "BS-SET-2",
                    "member_entries": [
                        {"sku": "BS-2", "name": "Bulk Resync Knife Two", "quantity": 3},
                    ],
                },
            ],
        ):
            response = self.client.post(
                "/sets/bulk-resync-memberships",
                data={
                    "csrf_token": "test-csrf-token",
                    "next": "/sets?missing=1&incomplete=1",
                    "set_ids": [str(first_set_id), str(second_set_id)],
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/sets?missing=1&incomplete=1", response.headers["Location"])

        with self.app.app_context():
            first_set = db.session.get(Set, first_set_id)
            second_set = db.session.get(Set, second_set_id)
            self.assertEqual(len(first_set.members), 1)
            self.assertEqual(len(second_set.members), 1)
            self.assertEqual(
                db.session.get(Item, first_set.members[0].item_id).sku, "BS-1"
            )
            self.assertEqual(
                db.session.get(Item, second_set.members[0].item_id).sku, "BS-2"
            )
            self.assertEqual(first_set.members[0].quantity, 2)
            self.assertEqual(second_set.members[0].quantity, 3)
            self.assertIn("BS-1", first_set.member_data)
            self.assertIn("BS-2", second_set.member_data)

    def test_catalog_purge_and_delete_routes(self):
        self._login_as_admin()
        self._set_csrf_token()

        keep_item_id, keep_variant_id = self._add_catalog_item(
            name="Keep Knife", sku="KP-1"
        )
        drop_item_id, _drop_variant_id = self._add_catalog_item(
            name="Drop Knife", sku="DR-1"
        )
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
