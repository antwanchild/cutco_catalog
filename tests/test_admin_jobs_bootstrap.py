# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
from admin_jobs_support import (
    AdminJobBaseTest,
    BOOTSTRAP_VERSION,
    Item,
    KNIFE_TASK_PRESETS,
    KnifeTask,
    SCHEMA_VERSION,
    SchemaState,
    BootstrapState,
    apply_schema_migrations,
    db,
    initialize_database,
)
from startup import (
    _categorize_uncategorized_bbq_tools,
    _categorize_uncategorized_gift_boxes,
)


class AdminBootstrapSmokeTests(AdminJobBaseTest):
    def test_bbq_tool_backfill_is_conservative(self):
        with self.app.app_context():
            bbq_turner = Item(name="BBQ Turner", sku="BBQ-1", edge_type="Unknown")
            barbecue_tongs = Item(name="Barbecue Tongs", sku="BBQ-2")
            barbeque_fork = Item(name="Barbeque Fork", sku="BBQ-3")
            kitchen_fork = Item(name="Turning Fork", sku="BBQ-4")
            bbq_set = Item(name="3-Pc. BBQ Tool Set", sku="BBQ-5")
            categorized = Item(name="BBQ Turner", sku="BBQ-6", category="Kitchen Tools")
            db.session.add_all(
                [
                    bbq_turner,
                    barbecue_tongs,
                    barbeque_fork,
                    kitchen_fork,
                    bbq_set,
                    categorized,
                ]
            )
            db.session.flush()

            _categorize_uncategorized_bbq_tools()
            db.session.commit()

            for item in (bbq_turner, barbecue_tongs, barbeque_fork):
                self.assertEqual(item.category, "BBQ Tools")
                self.assertEqual(item.edge_type, "N/A")
                self.assertFalse(item.edge_is_unicorn)
            self.assertIsNone(kitchen_fork.category)
            self.assertIsNone(bbq_set.category)
            self.assertEqual(categorized.category, "Kitchen Tools")

    def test_gift_box_backfill_is_conservative(self):
        with self.app.app_context():
            exact = Item(name="Gift Box", sku="GB-1", edge_type="Unknown")
            described = Item(
                name="Gift Box for Super Shears", sku="GB-2", edge_type="Straight"
            )
            suffix = Item(name="Trimmer Gift Box", sku="GB-3")
            boxed_set = Item(name="Gift-Boxed Knife Set", sku="GB-4")
            categorized = Item(name="Gift Box", sku="GB-5", category="Accessories")
            db.session.add_all([exact, described, suffix, boxed_set, categorized])
            db.session.flush()

            _categorize_uncategorized_gift_boxes()
            db.session.commit()

            for item in (exact, described, suffix):
                self.assertEqual(item.category, "Gift Boxes")
                self.assertEqual(item.edge_type, "N/A")
                self.assertFalse(item.edge_is_unicorn)
            self.assertIsNone(boxed_set.category)
            self.assertEqual(categorized.category, "Accessories")

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
