# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class PublicSmokeTests(SmokeBaseTest):
    def test_public_pages_load(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/catalog/").status_code, 200)
        self.assertEqual(self.client.get("/sets/").status_code, 200)
        self.assertIn(b"Quick Actions", response.data)
        self.assertIn(b"Browse catalog", response.data)
        self.assertIn(b"Browse sets", response.data)
        self.assertIn(b"Popular Colors", response.data)
        self.assertNotIn(b"Collectors", response.data)
        self.assertNotIn(b"Recently Changed", response.data)
        self.assertNotIn(b"Recent Activity", response.data)
        self.assertIn(b"\xc2\xa9", response.data)
        self.assertEqual(
            response.headers["Referrer-Policy"], "strict-origin-when-cross-origin"
        )
        self.assertNotIn("Strict-Transport-Security", response.headers)
        self.assertEqual(self.client.get("/robots.txt").status_code, 200)

    def test_private_pages_redirect_without_auth(self):
        response = self.client.get("/people")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        response = self.client.get("/people/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        response = self.client.get("/wishlist")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        response = self.client.get("/views/matrix")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        response = self.client.get("/stats")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        response = self.client.get("/stats/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_nav_menus_are_sectioned_for_admin(self):
        self._login_as_admin()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"More", response.data)
        self.assertIn(b"Explore", response.data)
        self.assertIn(b"Track", response.data)
        self.assertIn(b"Tools", response.data)
        self.assertIn(b"Import", response.data)
        self.assertIn(b"Completion Import", response.data)
        self.assertIn(b"Completion Gaps", response.data)
        self.assertIn(b"Export", response.data)
        self.assertIn(b"Variant Sync", response.data)
        self.assertIn(b"/catalog?unicorn=1", response.data)
        self.assertIn(b"Admin", response.data)
        self.assertIn(b"Review", response.data)
        self.assertIn(b"Session", response.data)
        self.assertIn(b"All Variants", response.data)
        self.assertIn(b"MSRP Diff", response.data)

    def test_dashboard_popular_colors_links_to_variants_browse(self):
        self._login_as_admin()
        self._set_csrf_token()

        _red_item_id, red_variant_id = self._add_catalog_item(
            name="Dashboard Red Knife", sku="DR-1"
        )
        _purple_item_id, purple_variant_id = self._add_catalog_item(
            name="Dashboard Purple Knife", sku="DP-1"
        )

        with self.app.app_context():
            red_variant = db.session.get(ItemVariant, red_variant_id)
            purple_variant = db.session.get(ItemVariant, purple_variant_id)
            self.assertIsNotNone(red_variant)
            self.assertIsNotNone(purple_variant)
            red_variant.color = "Red"
            purple_variant.color = "Purple"
            db.session.commit()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Popular Colors", response.data)
        self.assertIn(b"Red", response.data)
        self.assertIn(b"Purple", response.data)
        self.assertIn(b"/variants?color=Red", response.data)
        self.assertIn(b"/variants?color=Purple", response.data)

    def test_public_pages_include_hsts_when_cookie_secure_enabled(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains",
        )

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
        _uncategorized_item_id, _uncategorized_variant_id = self._add_catalog_item(
            name="Search Uncat", sku="SRCH-2", category=None
        )
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

        uncategorized_response = self.client.get(
            "/search?q=Search&category=__uncategorized__"
        )
        self.assertEqual(uncategorized_response.status_code, 200)
        self.assertIn(b"Search Uncat", uncategorized_response.data)
        self.assertIn(b"Uncategorized", uncategorized_response.data)
        self.assertNotIn(b"Search Knife", uncategorized_response.data)

    def test_variants_browse_page_filters_by_color(self):
        self._login_as_admin()
        self._set_csrf_token()

        _red_item_id, red_variant_id = self._add_catalog_item(
            name="Red Knife", sku="R-1"
        )
        _purple_item_id, purple_variant_id = self._add_catalog_item(
            name="Purple Knife", sku="P-1"
        )
        _unknown_item_id, unknown_variant_id = self._add_catalog_item(
            name="Unknown Knife", sku="U-1"
        )

        with self.app.app_context():
            red_variant = db.session.get(ItemVariant, red_variant_id)
            purple_variant = db.session.get(ItemVariant, purple_variant_id)
            unknown_variant = db.session.get(ItemVariant, unknown_variant_id)
            self.assertIsNotNone(red_variant)
            self.assertIsNotNone(purple_variant)
            self.assertIsNotNone(unknown_variant)
            red_variant.color = "Red"
            purple_variant.color = "Purple"
            db.session.commit()

        self.client.get("/variants")
        browse_response = self.client.get("/variants")
        self.assertEqual(browse_response.status_code, 200)
        self.assertIn(b"Variants", browse_response.data)
        self.assertIn(b"Red Knife", browse_response.data)
        self.assertIn(b"Purple Knife", browse_response.data)

        purple_response = self.client.get("/variants?color=Purple")
        self.assertEqual(purple_response.status_code, 200)
        self.assertIn(b"Purple Knife", purple_response.data)
        self.assertNotIn(b"Red Knife", purple_response.data)

        unknown_response = self.client.get("/variants?unknown=1")
        self.assertEqual(unknown_response.status_code, 200)
        self.assertIn(b"Unknown Knife", unknown_response.data)
        self.assertIn(b"Include Unknown", unknown_response.data)

    def test_admin_login_sets_session_flag(self):
        self._set_csrf_token()
        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": "test-csrf-token",
                "token": "test-admin-token",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Secure", response.headers.get("Set-Cookie", ""))
        self.assertIn("Expires=", response.headers.get("Set-Cookie", ""))
        self.assertFalse(self.app.config["SESSION_REFRESH_EACH_REQUEST"])
        with self.client.session_transaction() as session:
            self.assertEqual(
                session.get(AUTH_SESSION_KEY),
                {"kind": IDENTITY_KIND_TOKEN_ADMIN},
            )
            self.assertNotIn("is_admin", session)

    def test_proxy_admin_bypasses_admin_login_form(self):
        self._add_proxy_user("proxy-admin", role="admin")
        headers = {"X-Forwarded-User": "proxy-admin"}
        response = self.client.get(
            "/admin/login", headers=headers, follow_redirects=False
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/diagnostics", response.headers["Location"])
        with self.client.session_transaction() as session:
            self.assertNotIn(AUTH_SESSION_KEY, session)
            self.assertNotIn("is_admin", session)

        home_response = self.client.get("/", headers=headers)
        self.assertEqual(home_response.status_code, 200)
        self.assertIn(b"Admin", home_response.data)

    def test_admin_root_redirects_without_login(self):
        response = self.client.get("/admin", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_proxy_admin_group_unlocks_admin_routes(self):
        self._add_proxy_user("proxy-admin", role="admin")
        response = self.client.get(
            "/admin/diagnostics",
            headers={"X-Forwarded-User": "proxy-admin"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Diagnostics", response.data)

    def test_regression_proxy_auth_header_lookup_is_case_insensitive(self):
        self._add_proxy_user("proxy-user")
        self.app.config.update(
            TRUSTED_AUTH_USERNAME_HEADER="X-Authentik-Username",
            TRUSTED_AUTH_SUBJECT_HEADER="X-Authentik-Username",
        )
        response = self.client.get(
            "/people",
            headers={"x-authentik-username": "proxy-user"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Collectors", response.data)

    def test_proxy_auth_debug_log_names_present_headers_without_values(self):
        self.app.config["TRUSTED_AUTH_USERNAME_HEADER"] = "X-Authentik-Username"
        with self.assertLogs("helpers", level="DEBUG") as captured:
            response = self.client.get(
                "/people",
                headers={"X-Forwarded-User": "proxy-user"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        joined = "\n".join(captured.output)
        self.assertIn("configured='X-Authentik-Username'", joined)
        self.assertIn("X-Forwarded-User", joined)
        self.assertNotIn("proxy-user", joined)

    def test_audit_trail_records_and_lists_changes(self):
        self._login_as_admin()
        self._set_csrf_token()

        self._add_catalog_item(name="Audit Knife", sku="AUD-1")
        person_id = self._add_person(name="Audit Collector", notes="Original note")

        response = self.client.post(
            f"/people/{person_id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "name": "Audit Collector Updated",
                "notes": "Updated note",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            audit_events = (
                db.session.execute(
                    db.select(ActivityEvent).where(ActivityEvent.kind == "audit")
                )
                .scalars()
                .all()
            )
        self.assertGreaterEqual(len(audit_events), 3)

        audit_page = self.client.get("/admin/audit")
        self.assertEqual(audit_page.status_code, 200)
        self.assertIn(b"Audit Knife", audit_page.data)
        self.assertIn(b"Audit Collector Updated", audit_page.data)
        self.assertIn(b"create", audit_page.data)
        self.assertIn(b"update", audit_page.data)

    def test_gift_share_and_collection_card_pages_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        owned_item_id, owned_variant_id = self._add_catalog_item(
            name="Gift Knife Owned", sku="GL-1"
        )
        missing_item_id, _missing_variant_id = self._add_catalog_item(
            name="Gift Knife Missing", sku="GL-2"
        )
        person_id = self._add_person(name="Gift Recipient", notes="")
        set_id = self._add_set(
            name="Gift Set", sku="GS-1", item_ids=(owned_item_id, missing_item_id)
        )

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

        gift_share_response = self.client.get(
            f"/sets/{set_id}/gift-token?person={person_id}"
        )
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

        wishlist_response = self.client.get(
            f"/wishlist?person={person_id}&sort=name&dir=desc"
        )
        self.assertEqual(wishlist_response.status_code, 200)
        self.assertIn(b"Wishlist Knife", wishlist_response.data)
        self.assertIn(b"target met", wishlist_response.data)
        self.assertIn(b"?sort=name&amp;dir=asc", wishlist_response.data)

        with (
            mock.patch(
                "blueprints.people.DISCORD_WEBHOOK_URL", "https://discord.invalid"
            ),
            mock.patch(
                "blueprints.people._notify_discord", return_value=True
            ) as notify_mock,
        ):
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

        item_id, variant_id = self._add_catalog_item(
            name="View Knife",
            sku="VW-1",
            alternate_skus="VW-ALT1, VW-ALT2",
        )
        red_item_id, red_variant_id = self._add_catalog_item(
            name="Stats Red Knife", sku="SR-1"
        )
        purple_item_id, purple_variant_id = self._add_catalog_item(
            name="Stats Purple Knife", sku="SP-1"
        )
        _matrix_sort_item_id, _ = self._add_catalog_item(
            name="A Matrix Knife", sku="AA-1"
        )
        person_id = self._add_person(name="Viewer", notes="")
        set_id = self._add_set(name="View Set", sku="VS-1", item_ids=(item_id,))

        with self.app.app_context():
            view_variant = db.session.get(ItemVariant, variant_id)
            red_variant = db.session.get(ItemVariant, red_variant_id)
            purple_variant = db.session.get(ItemVariant, purple_variant_id)
            self.assertIsNotNone(view_variant)
            self.assertIsNotNone(red_variant)
            self.assertIsNotNone(purple_variant)
            view_variant.color = "Blue"
            red_variant.color = "Red"
            purple_variant.color = "Purple"
            db.session.commit()

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
        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(red_variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(purple_variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "",
            },
            follow_redirects=False,
        )

        item_response = self.client.get(f"/views/item/{item_id}")
        matrix_response = self.client.get("/views/matrix?sort=sku&dir=desc")
        stats_response = self.client.get("/stats")
        gift_share_response = self.client.get(
            f"/sets/{set_id}/gift-token?person={person_id}"
        )
        collection_share_response = self.client.get(
            f"/people/{person_id}/collection-token"
        )

        self.assertEqual(item_response.status_code, 200)
        self.assertIn(b"View Knife", item_response.data)
        self.assertIn(b"Item Overview", item_response.data)
        self.assertIn(b"VW-1", item_response.data)
        self.assertIn(b"Top Colors", item_response.data)
        self.assertIn(b"/variants?color=Blue", item_response.data)
        self.assertIn(b"Aliases", item_response.data)
        self.assertIn(b"2 alt SKUs", item_response.data)
        self.assertIn(b'data-clamp-rows="2"', item_response.data)
        self.assertEqual(matrix_response.status_code, 200)
        self.assertIn(b"Matrix", matrix_response.data)
        self.assertIn(b"?sort=name&amp;dir=asc", matrix_response.data)
        self.assertIn(b"Name", matrix_response.data)
        self.assertIn(b"SKU \xe2\x96\xbc", matrix_response.data)
        self.assertIn(b"#AA-1", matrix_response.data)
        self.assertGreater(
            matrix_response.data.index(b"#AA-1"), matrix_response.data.index(b"#VW-1")
        )
        self.assertEqual(stats_response.status_code, 200)
        self.assertIn(b"Coverage", stats_response.data)
        self.assertIn(b"Top Colors", stats_response.data)
        self.assertIn(b'href="/variants"', stats_response.data)
        self.assertIn(b"/variants?color=Red", stats_response.data)
        self.assertIn(b"/variants?color=Purple", stats_response.data)
        self.assertIn(
            b"Includes public items plus unicorn, rep only, Costco, and non-catalog items that are marked Owned.",
            stats_response.data,
        )
        diagnostics_response = self.client.get("/admin/diagnostics")
        self.assertEqual(diagnostics_response.status_code, 200)
        self.assertIn(b"Diagnostics Overview", diagnostics_response.data)
        self.assertEqual(gift_share_response.status_code, 200)
        self.assertIn(b"Share Gift List", gift_share_response.data)
        self.assertEqual(collection_share_response.status_code, 200)
        self.assertIn(b"Share Collection Card", collection_share_response.data)
        collection_response = self.client.get(f"/people/{person_id}/collection")
        self.assertEqual(collection_response.status_code, 200)
        self.assertIn(b"Collection Overview", collection_response.data)
        self.assertIn(b"Top Colors", collection_response.data)
        self.assertIn(b"/variants?color=Red", collection_response.data)
        self.assertIn(b"/variants?color=Purple", collection_response.data)

    def test_item_attachments_upload_render_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(
            name="Attachment Knife", sku="AT-1"
        )
        image_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01"
            b"\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00"
            b"\x90wS\xde"
            b"\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\x0f\x00\x01\x01\x01\x00"
            b"\x18\xdd\x8d\x18"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        upload_response = self.client.post(
            f"/views/item/{item_id}/attachments",
            data={
                "csrf_token": "test-csrf-token",
                "attachment": (BytesIO(image_bytes), "attachment.png", "image/png"),
                "caption": "Front view",
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(upload_response.status_code, 302)

        with self.app.app_context():
            attachment = db.session.execute(
                db.select(ItemAttachment).filter_by(item_id=item_id)
            ).scalar_one()
            attachment_path = f"{self.temp_dir.name}/uploads/items/{item_id}/{attachment.stored_filename}"
            self.assertTrue(os.path.exists(attachment_path))

        item_response = self.client.get(f"/views/item/{item_id}")
        self.assertEqual(item_response.status_code, 200)
        self.assertIn(b"Attachments (1)", item_response.data)
        self.assertIn(b"Front view", item_response.data)
        self.assertIn(b"attachment.png", item_response.data)

        file_response = self.client.get(f"/attachments/{attachment.id}")
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.mimetype, "image/png")
        self.assertEqual(file_response.data, image_bytes)
        file_response.close()

        delete_response = self.client.post(
            f"/attachments/{attachment.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)

        with self.app.app_context():
            remaining = (
                db.session.execute(db.select(ItemAttachment).filter_by(item_id=item_id))
                .scalars()
                .all()
            )
        self.assertEqual(remaining, [])

    def test_item_attachment_file_cleanup_on_item_delete(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, _variant_id = self._add_catalog_item(name="Cleanup Knife", sku="CL-1")
        image_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01"
            b"\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00"
            b"\x90wS\xde"
            b"\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\x0f\x00\x01\x01\x01\x00"
            b"\x18\xdd\x8d\x18"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        upload_response = self.client.post(
            f"/views/item/{item_id}/attachments",
            data={
                "csrf_token": "test-csrf-token",
                "attachment": (BytesIO(image_bytes), "cleanup.png", "image/png"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(upload_response.status_code, 302)

        with self.app.app_context():
            attachment = db.session.execute(
                db.select(ItemAttachment).filter_by(item_id=item_id)
            ).scalar_one()
            attachment_path = f"{self.temp_dir.name}/uploads/items/{item_id}/{attachment.stored_filename}"
            self.assertTrue(os.path.exists(attachment_path))

        delete_item_response = self.client.post(
            f"/catalog/{item_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_item_response.status_code, 302)
        self.assertFalse(os.path.exists(attachment_path))

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
        self.assertIn(b"Browse Controls", catalog_response.data)
        self.assertIn(b"Catalog Knife", catalog_response.data)
        self.assertIn(b'data-clamp-rows="2"', catalog_response.data)
        self.assertIn(b'data-confirm-title="Delete item"', catalog_response.data)
        self.assertEqual(variants_response.status_code, 200)
        self.assertIn(b"Variants", variants_response.data)
        self.assertEqual(sets_response.status_code, 200)
        self.assertIn(b"Catalog Set", sets_response.data)
        self.assertEqual(set_detail_response.status_code, 200)
        self.assertIn(b"Catalog Set", set_detail_response.data)

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
            added_variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item_id, color="Pearl White")
            ).scalar_one()

        edit_variant_response = self.client.post(
            f"/variants/{added_variant.id}/edit",
            data={
                "csrf_token": "test-csrf-token",
                "color": "Pearl Ivory",
                "notes": "Updated color",
            },
            follow_redirects=False,
        )
        self.assertEqual(edit_variant_response.status_code, 302)

    def test_people_pages_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, variant_id = self._add_catalog_item(name="People Knife", sku="PL-1")
        person_id = self._add_person(name="People Viewer", notes="")
        _wishlist_low_item_id, wishlist_low_variant_id = self._add_catalog_item(
            name="Alpha Wishlist Knife", sku="AA-2"
        )
        _wishlist_high_item_id, wishlist_high_variant_id = self._add_catalog_item(
            name="Zulu Wishlist Knife", sku="ZZ-2"
        )

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

        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(wishlist_low_variant_id),
                "status": "Wishlist",
                "target_price": "24.99",
                "notes": "First wishlist item",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(wishlist_high_variant_id),
                "status": "Wishlist",
                "target_price": "34.99",
                "notes": "Second wishlist item",
            },
            follow_redirects=False,
        )

        people_response = self.client.get("/people")
        collection_response = self.client.get(f"/people/{person_id}/collection")
        edit_page_response = self.client.get(f"/people/{person_id}/edit")
        wishlist_response = self.client.get("/wishlist?sort=name")

        self.assertEqual(people_response.status_code, 200)
        self.assertIn(b"People Viewer", people_response.data)
        self.assertEqual(collection_response.status_code, 200)
        self.assertIn(b"Owned", collection_response.data)
        self.assertIn(b'id="bulk-form"', collection_response.data)
        self.assertIn(b'form="bulk-form"', collection_response.data)
        self.assertIn(b"/ownership/", collection_response.data)
        self.assertEqual(edit_page_response.status_code, 200)
        self.assertIn(b"People Viewer", edit_page_response.data)
        self.assertEqual(wishlist_response.status_code, 200)
        self.assertIn(b"Wishlist", wishlist_response.data)
        self.assertIn(b"?sort=sku", wishlist_response.data)
        self.assertIn(b"Name \xe2\x96\xb2", wishlist_response.data)
        self.assertIn(b"SKU", wishlist_response.data)
        self.assertIn(b"#AA-2", wishlist_response.data)
        self.assertIn(b"#ZZ-2", wishlist_response.data)
        self.assertLess(
            wishlist_response.data.index(b"#AA-2"),
            wishlist_response.data.index(b"#ZZ-2"),
        )

    def test_empty_collection_page_renders_shell(self):
        self._login_as_admin()
        self._set_csrf_token()

        person_id = self._add_person(name="Empty Collector", notes="")
        collection_response = self.client.get(f"/people/{person_id}/collection")

        self.assertEqual(collection_response.status_code, 200)
        self.assertIn(b"No entries yet", collection_response.data)
        self.assertIn(b"collection-empty-state", collection_response.data)
        self.assertIn(b"Browse Catalog", collection_response.data)

    def test_collection_missing_items_show_skus(self):
        self._login_as_admin()
        self._set_csrf_token()

        self._add_catalog_item(name="Gap Knife", sku="GK-1")
        person_id = self._add_person(name="Gap Collector", notes="")
        collection_response = self.client.get(f"/people/{person_id}/collection")

        self.assertEqual(collection_response.status_code, 200)
        self.assertIn(b"Missing Items", collection_response.data)
        self.assertIn(b"Gap Knife", collection_response.data)
        self.assertIn(b"GK-1", collection_response.data)
        self.assertIn(b"+ Add", collection_response.data)

    def test_collection_variant_gaps_show_skus(self):
        self._login_as_admin()
        self._set_csrf_token()

        item_id, owned_variant_id = self._add_catalog_item(
            name="Variant Gap Knife", sku="VG-1"
        )
        with self.app.app_context():
            item = db.session.get(Item, item_id)
            db.session.add(ItemVariant(item_id=item_id, color="Pearl White"))
            db.session.commit()
            missing_variant = next(
                variant for variant in item.variants if variant.color == "Pearl White"
            )
        person_id = self._add_person(name="Variant Gap Collector", notes="")
        self.client.post(
            "/ownership/add",
            data={
                "csrf_token": "test-csrf-token",
                "person_id": str(person_id),
                "variant_id": str(owned_variant_id),
                "status": "Owned",
                "target_price": "",
                "notes": "",
            },
            follow_redirects=False,
        )

        collection_response = self.client.get(f"/people/{person_id}/collection")

        self.assertEqual(collection_response.status_code, 200)
        self.assertIn(b"Variant Gaps", collection_response.data)
        self.assertIn(b"Variant Gap Knife", collection_response.data)
        self.assertIn(b"VG-1", collection_response.data)
        self.assertIn(missing_variant.color.encode(), collection_response.data)
        self.assertIn(b"+ Add", collection_response.data)

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
        self.assertIn(
            "my_export.csv", export_csv_response.headers["Content-Disposition"]
        )
        self.assertIn(b"Exporter", export_csv_response.data)
        self.assertIn(b"quantity_purchased", export_csv_response.data)
        self.assertIn(b"quantity_given_away", export_csv_response.data)
        self.assertEqual(import_page_response.status_code, 200)
        self.assertIn(b"Import", import_page_response.data)
        self.assertIn(b"Start Here", import_page_response.data)
        self.assertIn(b"Case-insensitive matching", import_page_response.data)
        self.assertEqual(import_template_response.status_code, 200)
        self.assertEqual(import_template_response.mimetype, "text/csv")
        self.assertIn(
            b"name,sku,owned,color,availability,quantity purchased,quantity given away,category,edge,"
            b"copy_type,engraved,engraving_text,engraving_notes,is_sku_unicorn,is_variant_unicorn,is_edge_unicorn,set_members,price",
            import_template_response.data,
        )

    def test_log_dashboards_render(self):
        self._login_as_admin()
        self._set_csrf_token()

        sharpening_item_id, _ = self._add_catalog_item(
            name="Sharpen View Knife", sku="SV-1"
        )
        giftbox_item_id, _ = self._add_catalog_item(
            name="Gift Box Sharpener", sku="GB-1"
        )
        accessory_item_id, _ = self._add_catalog_item(
            name="Accessory Sharpener", sku="AC-1", category="Accessories"
        )
        shears_item_id, _ = self._add_catalog_item(
            name="Super Shears", sku="SS-1", category="Accessories"
        )
        gadget_item_id, _ = self._add_catalog_item(
            name="Gadget Sharpener", sku="GD-1", category="Gadgets"
        )
        sheath_item_id, _ = self._add_catalog_item(
            name="Sheath Sharpener", sku="SH-1", category="Sheaths"
        )
        storage_item_id, _ = self._add_catalog_item(
            name="Storage Sharpener", sku="ST-1", category="Storage"
        )
        cutting_board_item_id, _ = self._add_catalog_item(
            name="Cutting Board Sharpener", sku="CB-1", category="Cutting Boards"
        )
        cookware_item_id, _ = self._add_catalog_item(
            name="Cook View Piece", sku="CV-1", category="Cookware"
        )
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
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(cookware_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(giftbox_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(accessory_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(shears_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(gadget_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(sheath_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(storage_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/sharpening/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(cutting_board_item_id),
                "sharpened_on": "2026-04-15",
                "method": "Whetstone",
                "notes": "Allowed in logs, hidden from page lists",
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
        sharpening_select = (
            sharpening_response.data.decode("utf-8")
            .split(
                '<select name="item_id" required class="select-sm" style="min-width:200px">'
            )[1]
            .split("</select>")[0]
        )

        self.assertEqual(sharpening_response.status_code, 200)
        self.assertIn(b"Sharpening", sharpening_response.data)
        self.assertNotIn("Cook View Piece", sharpening_select)
        self.assertNotIn("Gift Box Sharpener", sharpening_select)
        self.assertNotIn("Accessory Sharpener", sharpening_select)
        self.assertIn("Super Shears", sharpening_select)
        self.assertNotIn("Gadget Sharpener", sharpening_select)
        self.assertNotIn("Sheath Sharpener", sharpening_select)
        self.assertNotIn("Storage Sharpener", sharpening_select)
        self.assertNotIn("Cutting Board Sharpener", sharpening_select)
        self.assertEqual(cookware_response.status_code, 200)
        self.assertIn(b"Cookware", cookware_response.data)
        self.assertEqual(tasks_response.status_code, 200)
        self.assertIn(b"Tasks", tasks_response.data)
        self.assertEqual(tasks_manage_response.status_code, 200)
        self.assertIn(b"Slice onions", tasks_manage_response.data)
        self.assertEqual(task_detail_response.status_code, 200)
        self.assertIn(b"Slice onions", task_detail_response.data)

        with self.app.app_context():
            sharpening_logs = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=sharpening_item_id)
            ).all()
            self.assertEqual(len(sharpening_logs), 1)
            cookware_logs = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=cookware_item_id)
            ).all()
            self.assertEqual(len(cookware_logs), 1)
            giftbox_logs = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=giftbox_item_id)
            ).all()
            self.assertEqual(len(giftbox_logs), 1)
            gadget_logs = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=gadget_item_id)
            ).all()
            self.assertEqual(len(gadget_logs), 1)
            sheath_logs = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=sheath_item_id)
            ).all()
            self.assertEqual(len(sheath_logs), 1)
            storage_logs = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=storage_item_id)
            ).all()
            self.assertEqual(len(storage_logs), 1)

        with (
            mock.patch(
                "blueprints.logs._notify_discord", return_value=True
            ) as notify_mock,
            mock.patch(
                "blueprints.logs.DISCORD_WEBHOOK_URL", "https://discord.invalid"
            ),
            mock.patch("blueprints.logs.SHARPEN_THRESHOLD_DAYS", 1),
            mock.patch("blueprints.logs.COOKWARE_THRESHOLD_DAYS", 1),
        ):
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
