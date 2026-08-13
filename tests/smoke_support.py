# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
import json  # noqa: F401
import os  # noqa: F401
import tempfile  # noqa: F401
import unittest  # noqa: F401
from io import BytesIO  # noqa: F401
from unittest import mock  # noqa: F401

from bs4 import BeautifulSoup  # noqa: F401
from flask import Flask  # noqa: F401
from openpyxl import Workbook  # noqa: F401

os.environ.setdefault("INITIAL_SETUP_TOKEN", "test-initial-setup-token")

import blueprints.catalog as catalog_blueprint  # noqa: F401
from app import _teardown_logging, create_app  # noqa: F401
from blueprints.catalog import (  # noqa: F401
    _build_member_name_lookup,
    _build_set_membership_preview,
    _load_member_snapshot,
)
from constants import UNKNOWN_COLOR, normalize_edge_for_category  # noqa: F401
from extensions import db  # noqa: F401
from helpers import (  # noqa: F401
    AUTH_SESSION_KEY,
    IDENTITY_KIND_PROXY_ADMIN,
    _collection_token,
    _gift_token,
    _notify_discord,
    _verify_collection_token,
    _verify_gift_token,
    check_wishlist_targets,
)
from models import (  # noqa: F401
    ActivityEvent,
    CookwareSession,
    Item,
    ItemAttachment,
    ItemSetMember,
    ItemVariant,
    KnifeTask,
    KnifeTaskLog,
    Ownership,
    Person,
    Set,
    SharpeningLog,
    User,
)
from msrp_diff import find_stale_msrp_rows  # noqa: F401
from msrp_scrape import _scrape_price_from_page  # noqa: F401
from scraping import (  # noqa: F401
    _build_set_member_entries,
    _collect_visible_set_piece_rows,
    _dedupe_product_links,
    _extract_cutco_price,
    _extract_product_variant_colors,
    _extract_sku_from_href,
    _find_cutco_item_link,
    _infer_visible_member_sku,
    _member_hover_title,
    _normalize_set_member_sku,
    _product_link_name,
    _resolve_cutco_item_page_url,
    _resolve_visible_member_sku,
    _should_queue_slug,
    scrape_item_specs,
    scrape_purple_campaign_variants,
    scrape_set_variant_options,
)
from time_utils import container_timezone, format_container_time  # noqa: F401

__all__ = [
    "AUTH_SESSION_KEY",
    "IDENTITY_KIND_PROXY_ADMIN",
    "UNKNOWN_COLOR",
    "ActivityEvent",
    "BeautifulSoup",
    "BytesIO",
    "CookwareSession",
    "Flask",
    "Item",
    "ItemAttachment",
    "ItemSetMember",
    "ItemVariant",
    "KnifeTask",
    "KnifeTaskLog",
    "Ownership",
    "Person",
    "Set",
    "SharpeningLog",
    "SmokeBaseTest",
    "User",
    "Workbook",
    "_build_member_name_lookup",
    "_build_set_member_entries",
    "_build_set_membership_preview",
    "_collect_visible_set_piece_rows",
    "_collection_token",
    "_dedupe_product_links",
    "_extract_cutco_price",
    "_extract_product_variant_colors",
    "_extract_sku_from_href",
    "_find_cutco_item_link",
    "_gift_token",
    "_infer_visible_member_sku",
    "_load_member_snapshot",
    "_member_hover_title",
    "_normalize_set_member_sku",
    "_notify_discord",
    "_product_link_name",
    "_resolve_cutco_item_page_url",
    "_resolve_visible_member_sku",
    "_scrape_price_from_page",
    "_should_queue_slug",
    "_verify_collection_token",
    "_verify_gift_token",
    "catalog_blueprint",
    "check_wishlist_targets",
    "container_timezone",
    "create_app",
    "db",
    "find_stale_msrp_rows",
    "format_container_time",
    "json",
    "mock",
    "normalize_edge_for_category",
    "os",
    "scrape_item_specs",
    "scrape_purple_campaign_variants",
    "scrape_set_variant_options",
    "tempfile",
    "unittest",
]


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
                "ATTACHMENTS_DIR": f"{self.temp_dir.name}/uploads/items",
                "INITIAL_SETUP_TOKEN": "test-initial-setup-token",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        _teardown_logging(self.temp_dir.name)
        self.temp_dir.cleanup()

    def _login_as_admin(self):
        self._set_csrf_token()
        self.client.post(
            "/setup",
            data={
                "csrf_token": "test-csrf-token",
                "setup_token": "test-initial-setup-token",
                "username": "smoke-admin",
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
            },
            follow_redirects=False,
        )

    def _set_csrf_token(self, value="test-csrf-token"):
        with self.client.session_transaction() as session:
            session["csrf_token"] = value
        return value

    def _add_proxy_user(self, username, *, role="user", subject=None):
        with self.app.app_context():
            user = User(
                username=username,
                role=role,
                auth_source="proxy",
                external_subject=subject or username,
            )
            db.session.add(user)
            db.session.commit()
            return user.id

    def _add_catalog_item(
        self,
        *,
        name="Test Knife",
        sku="TK-1",
        category="Kitchen Knives",
        alternate_skus="",
    ):
        payload = {
            "csrf_token": "test-csrf-token",
            "name": name,
            "sku": sku,
            "alternate_skus": alternate_skus,
            "edge_type": "Straight",
            "cutco_url": "https://example.com/test-knife",
            "notes": "Initial note",
            "colors": "",
            "availability": "public",
        }
        if category is not None:
            payload["category"] = category
        response = self.client.post(
            "/catalog/add",
            data=payload,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = db.session.execute(db.select(Item).filter_by(sku=sku)).scalar_one()
            variant = db.session.execute(
                db.select(ItemVariant).filter_by(item_id=item.id)
            ).scalar_one()
        return item.id, variant.id

    def _add_person(self, name="Anthony", notes="Primary collector"):
        response = self.client.post(
            "/people/add",
            data={"csrf_token": "test-csrf-token", "name": name, "notes": notes},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            person = db.session.execute(
                db.select(Person).filter_by(name=name)
            ).scalar_one()
        return person.id

    def _add_task(self, name="Slice apples"):
        response = self.client.post(
            "/tasks/manage/add",
            data={"csrf_token": "test-csrf-token", "name": name},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = db.session.execute(
                db.select(KnifeTask).filter_by(name=name)
            ).scalar_one()
        return task.id

    def _add_set(self, name="Sample Set", sku="SS-1", item_ids=()):
        with self.app.app_context():
            item_set = Set(name=name, sku=sku)
            db.session.add(item_set)
            db.session.flush()
            for item_id in item_ids:
                db.session.add(
                    ItemSetMember(item_id=item_id, set_id=item_set.id, quantity=1)
                )
            db.session.commit()
            return item_set.id
