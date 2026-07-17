# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class AdminSmokeTests(SmokeBaseTest):
    def test_msrp_diff_defaults_to_write_mode(self):
        self._login_as_admin()

        response = self.client.get("/admin/msrp-diff")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="update_db" checked', response.data)

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
