# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class TaskSmokeTests(SmokeBaseTest):
    def test_task_add_detail_and_delete(self):
        self._login_as_admin()
        self._set_csrf_token()
        item_id, _variant_id = self._add_catalog_item(name="Task Knife", sku="TS-1")
        task_id = self._add_task(name="Slice apples")

        detail_response = self.client.get(f"/tasks/manage/{task_id}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(b"Slice apples", detail_response.data)

        add_log_response = self.client.post(
            "/tasks/add",
            data={
                "csrf_token": "test-csrf-token",
                "item_id": str(item_id),
                "task_id": str(task_id),
                "logged_on": "2026-04-15",
                "notes": "Fresh log",
            },
            follow_redirects=False,
        )
        self.assertEqual(add_log_response.status_code, 302)
        with self.app.app_context():
            entry = db.session.execute(
                db.select(KnifeTaskLog).filter_by(item_id=item_id, task_id=task_id)
            ).scalar_one()
            self.assertEqual(entry.notes, "Fresh log")

        delete_blocked = self.client.post(
            f"/tasks/manage/{task_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(delete_blocked.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(KnifeTask, task_id))

        purge_log_response = self.client.post(
            f"/tasks/log/{entry.id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )
        self.assertEqual(purge_log_response.status_code, 302)

        delete_response = self.client.post(
            f"/tasks/manage/{task_id}/delete",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=False,
        )

        self.assertEqual(delete_response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(KnifeTask, task_id))
