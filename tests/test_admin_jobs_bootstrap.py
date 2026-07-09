# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
from admin_jobs_support import (
    AdminJobBaseTest,
    BOOTSTRAP_VERSION,
    KNIFE_TASK_PRESETS,
    KnifeTask,
    SCHEMA_VERSION,
    SchemaState,
    BootstrapState,
    apply_schema_migrations,
    db,
    initialize_database,
)


class AdminBootstrapSmokeTests(AdminJobBaseTest):
    def test_startup_bootstrap_is_idempotent(self):
        with self.app.app_context():
            schema_state = db.session.get(SchemaState, "schema")
            self.assertIsNotNone(schema_state)
            assert schema_state is not None
            self.assertEqual(schema_state.version, SCHEMA_VERSION)
            initial_task_names = {
                task.name
                for task in db.session.execute(db.select(KnifeTask)).scalars().all()
            }
            bootstrap_state = db.session.get(BootstrapState, "bootstrap")
            self.assertIsNotNone(bootstrap_state)
            assert bootstrap_state is not None
            self.assertEqual(bootstrap_state.version, BOOTSTRAP_VERSION)
            initial_updated_at = bootstrap_state.updated_at
            initial_version = bootstrap_state.version
            apply_schema_migrations()
            initialize_database()
            second_state = db.session.get(BootstrapState, "bootstrap")
            second_task_names = {
                task.name
                for task in db.session.execute(db.select(KnifeTask)).scalars().all()
            }
            self.assertEqual(initial_task_names, set(KNIFE_TASK_PRESETS))
            self.assertIsNotNone(second_state)
            assert second_state is not None
            self.assertEqual(second_task_names, initial_task_names)
            self.assertEqual(second_state.version, initial_version)
            self.assertEqual(second_state.updated_at, initial_updated_at)
            self.assertEqual(len(second_task_names), len(KNIFE_TASK_PRESETS))
