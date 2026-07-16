# pyright: reportOptionalMemberAccess=false, reportArgumentType=false
import sqlite3
import tempfile
import unittest

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from app import _teardown_logging, create_app
from smoke_support import ActivityEvent, SmokeBaseTest, User, db
from models import (
    MIN_PASSWORD_LENGTH,
    USER_AUTH_SOURCE_PROXY,
    USER_ROLE_ADMIN,
    USER_ROLE_USER,
    record_audit_event,
)
from schema_migrations import SCHEMA_VERSION, SchemaHistory, SchemaState


class UserAuthLegacyMigrationTests(unittest.TestCase):
    def test_version_13_database_adds_user_auth_foundation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = f"{temp_dir}/legacy.db"
            connection = sqlite3.connect(database_path)
            connection.executescript("""
                CREATE TABLE schema_state (
                    name VARCHAR(40) PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at VARCHAR(32) NOT NULL
                );
                INSERT INTO schema_state (name, version, updated_at)
                VALUES ('schema', 13, '2026-01-01T00:00:00+00:00');

                CREATE TABLE schema_history (
                    version INTEGER PRIMARY KEY,
                    name VARCHAR(80) NOT NULL,
                    applied_at VARCHAR(32) NOT NULL
                );

                CREATE TABLE activity_events (
                    id INTEGER PRIMARY KEY,
                    kind VARCHAR(40) NOT NULL,
                    title VARCHAR(160) NOT NULL,
                    details TEXT,
                    occurred_at VARCHAR(32) NOT NULL,
                    actor VARCHAR(40),
                    action VARCHAR(20),
                    entity_type VARCHAR(40),
                    entity_id INTEGER,
                    entity_name VARCHAR(160),
                    source VARCHAR(160),
                    payload TEXT
                );
                INSERT INTO activity_events (
                    id, kind, title, occurred_at, actor
                ) VALUES (
                    1, 'audit', 'Legacy event',
                    '2026-01-01T00:00:00+00:00', 'admin'
                );
                """)
            connection.commit()
            connection.close()

            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test-secret-key",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
                    "LOG_DIR": temp_dir,
                    "ATTACHMENTS_DIR": f"{temp_dir}/uploads/items",
                }
            )
            try:
                with app.app_context():
                    inspector = sa_inspect(db.engine)
                    self.assertIn("users", inspector.get_table_names())
                    activity_columns = {
                        column["name"]
                        for column in inspector.get_columns("activity_events")
                    }
                    self.assertIn("actor_user_id", activity_columns)
                    schema_state = db.session.get(SchemaState, "schema")
                    self.assertEqual(schema_state.version, SCHEMA_VERSION)
                    legacy_event = db.session.get(ActivityEvent, 1)
                    self.assertEqual(legacy_event.title, "Legacy event")
                    self.assertEqual(legacy_event.actor, "admin")
                    self.assertIsNone(legacy_event.actor_user_id)
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()
                _teardown_logging(temp_dir)


class UserAuthFoundationTests(SmokeBaseTest):
    def _local_user(
        self,
        username="owner",
        *,
        role=USER_ROLE_USER,
        password="correct horse battery staple",
    ):
        user = User(username=username, role=role)
        user.set_password(password)
        return user

    def test_schema_contains_users_and_audit_actor_link(self):
        with self.app.app_context():
            inspector = sa_inspect(db.engine)
            self.assertIn("users", inspector.get_table_names())
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            self.assertEqual(
                {
                    "id",
                    "username",
                    "display_name",
                    "password_hash",
                    "role",
                    "auth_source",
                    "external_subject",
                    "is_active",
                    "must_change_password",
                    "session_version",
                    "last_login_at",
                    "created_at",
                    "updated_at",
                },
                user_columns,
            )
            activity_columns = {
                column["name"] for column in inspector.get_columns("activity_events")
            }
            self.assertIn("actor_user_id", activity_columns)
            foreign_keys = inspector.get_foreign_keys("activity_events")
            self.assertTrue(
                any(
                    foreign_key["referred_table"] == "users"
                    and foreign_key["constrained_columns"] == ["actor_user_id"]
                    for foreign_key in foreign_keys
                )
            )
            indexes = {
                index["name"] for index in inspector.get_indexes("activity_events")
            }
            self.assertIn("ix_activity_events_actor_user_id", indexes)
            schema_state = db.session.get(SchemaState, "schema")
            self.assertIsNotNone(schema_state)
            self.assertEqual(schema_state.version, SCHEMA_VERSION)
            migration = db.session.get(SchemaHistory, SCHEMA_VERSION)
            self.assertIsNotNone(migration)
            self.assertEqual(migration.name, "user_auth_foundation")

    def test_local_user_normalizes_username_and_hashes_password(self):
        password = "correct horse battery staple"
        with self.app.app_context():
            user = self._local_user("  Example.Admin  ", password=password)
            user.display_name = "Example Admin"
            db.session.add(user)
            db.session.commit()

            self.assertEqual(user.username, "example.admin")
            self.assertEqual(user.label, "Example Admin")
            self.assertNotEqual(user.password_hash, password)
            self.assertNotIn(password, user.password_hash)
            self.assertTrue(user.check_password(password))
            self.assertFalse(user.check_password("incorrect password"))
            self.assertEqual(user.session_version, 1)
            self.assertTrue(user.is_active)

    def test_password_policy_and_forced_change_flag(self):
        with self.app.app_context():
            user = User(username="temporary-admin", role=USER_ROLE_ADMIN)
            with self.assertRaisesRegex(ValueError, str(MIN_PASSWORD_LENGTH)):
                user.set_password("too-short")

            user.set_password(
                "temporary password value",
                require_change=True,
            )
            db.session.add(user)
            db.session.commit()
            self.assertTrue(user.must_change_password)

            previous_version = user.session_version
            user.revoke_sessions()
            db.session.commit()
            self.assertEqual(user.session_version, previous_version + 1)

    def test_usernames_are_unique_after_normalization(self):
        with self.app.app_context():
            db.session.add(self._local_user("CaseSensitive"))
            db.session.commit()
            db.session.add(self._local_user("  casesensitive  "))

            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_persisted_identity_fields_are_immutable(self):
        with self.app.app_context():
            user = self._local_user("stable-identity")
            db.session.add(user)
            db.session.commit()

            with self.assertRaisesRegex(ValueError, "username.*cannot be changed"):
                user.username = "renamed-identity"

            with self.assertRaisesRegex(ValueError, "auth_source.*cannot be changed"):
                user.auth_source = USER_AUTH_SOURCE_PROXY

            with self.assertRaisesRegex(
                ValueError, "external_subject.*cannot be changed"
            ):
                user.external_subject = "new-proxy-subject"

    def test_local_and_proxy_account_requirements(self):
        with self.app.app_context():
            missing_password = User(username="no-password")
            db.session.add(missing_password)
            with self.assertRaisesRegex(ValueError, "require a password"):
                db.session.commit()
            db.session.rollback()

            proxy_user = User(
                username="Proxy.User",
                auth_source=USER_AUTH_SOURCE_PROXY,
                external_subject="  stable-proxy-subject  ",
            )
            db.session.add(proxy_user)
            db.session.commit()
            self.assertEqual(proxy_user.username, "proxy.user")
            self.assertEqual(proxy_user.external_subject, "stable-proxy-subject")
            self.assertIsNone(proxy_user.password_hash)

            proxy_with_password = User(
                username="proxy-password",
                auth_source=USER_AUTH_SOURCE_PROXY,
                external_subject="proxy-password-subject",
            )
            proxy_with_password.set_password("not allowed for proxy")
            db.session.add(proxy_with_password)
            with self.assertRaisesRegex(ValueError, "cannot have a local password"):
                db.session.commit()
            db.session.rollback()

    def test_invalid_role_and_auth_source_are_rejected(self):
        with self.app.app_context():
            with self.assertRaisesRegex(ValueError, "Unsupported user role"):
                User(username="invalid-role", role="superuser")
            with self.assertRaisesRegex(
                ValueError, "Unsupported authentication source"
            ):
                User(username="invalid-source", auth_source="oidc")

    def test_last_active_admin_cannot_be_disabled_demoted_or_deleted(self):
        with self.app.app_context():
            first_admin = self._local_user("first-admin", role=USER_ROLE_ADMIN)
            db.session.add(first_admin)
            db.session.commit()

            first_admin.is_active = False
            with self.assertRaisesRegex(ValueError, "last active admin"):
                db.session.commit()
            db.session.rollback()

            first_admin = db.session.get(User, first_admin.id)
            first_admin.role = USER_ROLE_USER
            with self.assertRaisesRegex(ValueError, "last active admin"):
                db.session.commit()
            db.session.rollback()

            first_admin = db.session.get(User, first_admin.id)
            db.session.delete(first_admin)
            with self.assertRaisesRegex(ValueError, "last active admin"):
                db.session.commit()
            db.session.rollback()

            first_admin = db.session.get(User, first_admin.id)
            second_admin = self._local_user("second-admin", role=USER_ROLE_ADMIN)
            db.session.add(second_admin)
            db.session.commit()
            first_admin.is_active = False
            db.session.commit()
            self.assertFalse(first_admin.is_active)
            self.assertTrue(second_admin.is_active)

    def test_audit_event_can_reference_user_and_keep_actor_snapshot(self):
        with self.app.app_context():
            user = self._local_user("audit-admin", role=USER_ROLE_ADMIN)
            db.session.add(user)
            db.session.commit()

            record_audit_event(
                title="Changed account settings",
                actor=user.username,
                actor_user_id=user.id,
                action="update",
                entity_type="User",
                entity_id=user.id,
                entity_name=user.label,
            )
            db.session.commit()

            event = db.session.execute(
                db.select(ActivityEvent).where(
                    ActivityEvent.title == "Changed account settings"
                )
            ).scalar_one()
            self.assertEqual(event.actor_user_id, user.id)
            self.assertEqual(event.actor_user, user)
            self.assertEqual(event.actor, "audit-admin")
