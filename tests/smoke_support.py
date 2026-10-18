import os
import tempfile
import unittest

os.environ.setdefault("INITIAL_SETUP_TOKEN", "test-initial-setup-token")

from app import _teardown_logging, create_app
from extensions import db
from models import (
    Item,
    ItemSetMember,
    ItemVariant,
    KnifeTask,
    Person,
    Set,
    User,
)


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
