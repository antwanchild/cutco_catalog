# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *

class LogSmokeTests(SmokeBaseTest):
    def test_log_pages_render_and_notifications(self):
        self._login_as_admin()
        self._set_csrf_token()

        sharpening_item_id, _ = self._add_catalog_item(
            name="Sharpen Page Knife", sku="SR-1"
        )
        cookware_item_id, _ = self._add_catalog_item(
            name="Cookware Page Knife", sku="CW-1", category="Cookware"
        )
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

        with (
            mock.patch(
                "blueprints.logs.DISCORD_WEBHOOK_URL", "https://example.com/webhook"
            ),
            mock.patch("blueprints.logs.SHARPEN_THRESHOLD_DAYS", 1),
            mock.patch("blueprints.logs.COOKWARE_THRESHOLD_DAYS", 1),
            mock.patch("blueprints.logs._notify_discord") as notify_mock,
        ):
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

        sharpening_item_id, _ = self._add_catalog_item(
            name="Validation Sharpen Knife", sku="VL-1"
        )
        cookware_item_id, _ = self._add_catalog_item(
            name="Validation Cookware Knife", sku="VC-1", category="Cookware"
        )
        task_item_id, _ = self._add_catalog_item(
            name="Validation Task Knife", sku="VT-1"
        )
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
            sharpen_entry = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=sharpening_item_id)
            ).first()
            cookware_entry = db.session.execute(
                db.select(CookwareSession).filter_by(item_id=cookware_item_id)
            ).first()
            task_entry = db.session.execute(
                db.select(KnifeTaskLog).filter_by(item_id=task_item_id)
            ).first()

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

        with (
            mock.patch("blueprints.logs.DISCORD_WEBHOOK_URL", None),
            mock.patch("blueprints.logs.SHARPEN_THRESHOLD_DAYS", 999),
            mock.patch("blueprints.logs.COOKWARE_THRESHOLD_DAYS", 999),
        ):
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

        sharpening_item_id, _ = self._add_catalog_item(
            name="Edit Sharpen Knife", sku="ED-1"
        )
        cookware_item_id, _ = self._add_catalog_item(
            name="Edit Cookware Knife", sku="ED-2", category="Cookware"
        )
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
            sharpen_entry = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=sharpening_item_id)
            ).scalar_one()
            cookware_entry = db.session.execute(
                db.select(CookwareSession).filter_by(item_id=cookware_item_id)
            ).scalar_one()

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
        item_id, _variant_id = self._add_catalog_item(
            name="Sharpening Knife", sku="SH-1"
        )

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
            entry = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=item_id)
            ).scalar_one()
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
        item_id, _variant_id = self._add_catalog_item(
            name="Sharpen Purge Knife", sku="SP-1"
        )

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
            entry = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=item_id)
            ).scalar_one()
            self.assertEqual(entry.method, "Home Sharpener")

        purge_item_response = self.client.post(
            f"/sharpening/item/{item_id}/purge",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_item_response.status_code, 302)
        with self.app.app_context():
            remaining = (
                db.session.execute(db.select(SharpeningLog).filter_by(item_id=item_id))
                .scalars()
                .all()
            )
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
            delete_entry = db.session.execute(
                db.select(SharpeningLog).filter_by(item_id=item_id)
            ).scalar_one()

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
        item_id, _variant_id = self._add_catalog_item(
            name="Cookware Piece", sku="CK-1", category="Cookware"
        )

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
        item_id, _variant_id = self._add_catalog_item(
            name="Cookware Purge", sku="CP-1", category="Cookware"
        )

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
            session = db.session.execute(
                db.select(CookwareSession).filter_by(item_id=item_id)
            ).scalar_one()
            self.assertEqual(session.made_item, "Soup")
            self.assertEqual(session.rating, 4)

        purge_item_response = self.client.post(
            f"/cookware/item/{item_id}/purge",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_item_response.status_code, 302)
        with self.app.app_context():
            remaining = (
                db.session.execute(
                    db.select(CookwareSession).filter_by(item_id=item_id)
                )
                .scalars()
                .all()
            )
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
            delete_session = db.session.execute(
                db.select(CookwareSession).filter_by(item_id=item_id)
            ).scalar_one()

        delete_response = self.client.post(
            f"/cookware/{delete_session.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(CookwareSession, delete_session.id))
