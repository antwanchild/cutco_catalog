# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
from admin_jobs_support import (
    AdminJobBaseTest,
    Flask,
    Item,
    UTC,
    datetime,
    db,
    json,
    mock,
    msrp_jobs,
    timedelta,
)


class AdminMsrpJobSmokeTests(AdminJobBaseTest):
    def test_specs_backfill_run_starts_background_job(self):
        self._login_as_admin()
        self._set_csrf_token()

        with (
            mock.patch(
                "blueprints.admin._read_specs_job", return_value={"status": "idle"}
            ),
            mock.patch("blueprints.admin._write_specs_job") as write_mock,
            mock.patch("blueprints.admin.threading.Thread") as thread_mock,
        ):
            thread_instance = mock.Mock()
            thread_mock.return_value = thread_instance

            response = self.client.post(
                "/admin/specs-backfill/run",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        write_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.args[0]["status"], "running")
        self.assertIs(thread_mock.call_args.kwargs["args"][0], self.app)
        self.assertIsInstance(thread_mock.call_args.kwargs["args"][0], Flask)
        thread_instance.start.assert_called_once()

    def test_msrp_diff_run_starts_background_job(self):
        self._login_as_admin()
        self._set_csrf_token()

        with (
            mock.patch(
                "blueprints.admin._read_msrp_job", return_value={"status": "idle"}
            ),
            mock.patch("blueprints.admin._write_msrp_job") as write_mock,
            mock.patch("blueprints.admin.threading.Thread") as thread_mock,
        ):
            thread_instance = mock.Mock()
            thread_mock.return_value = thread_instance

            response = self.client.post(
                "/admin/msrp-diff/run",
                data={"csrf_token": "test-csrf-token", "update_db": "on"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        write_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.args[0]["status"], "running")
        self.assertTrue(write_mock.call_args.args[0]["update_db"])
        self.assertIs(thread_mock.call_args.kwargs["args"][0], self.app)
        self.assertIsInstance(thread_mock.call_args.kwargs["args"][0], Flask)
        thread_instance.start.assert_called_once()

    def test_msrp_diff_reset_clears_job_state(self):
        self._login_as_admin()
        self._set_csrf_token()

        with mock.patch("blueprints.admin._reset_msrp_job") as reset_mock:
            response = self.client.post(
                "/admin/msrp-diff/reset",
                data={"csrf_token": "test-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        reset_mock.assert_called_once()

    def test_stale_msrp_job_is_recovered_on_read(self):
        stale_started_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        job_data = {
            "status": "running",
            "progress": ["Starting MSRP diff…"],
            "results": None,
            "error": None,
            "started_at": stale_started_at,
            "finished_at": None,
            "update_db": True,
            "heartbeat_at": stale_started_at,
        }
        job_file = f"{self.temp_dir.name}/msrp_job.json"
        with open(job_file, "w", encoding="utf-8") as fh:
            json.dump(job_data, fh)

        with mock.patch.object(msrp_jobs, "_MSRP_JOB_FILE", job_file):
            recovered = msrp_jobs._read_msrp_job()

        self.assertEqual(recovered["status"], "error")
        self.assertIn("stale", recovered["error"].lower())
        self.assertIsNotNone(recovered["finished_at"])

    def test_msrp_price_fetch_timeout_finishes_without_blocking(self):
        by_sku = {
            "A-1": {"name": "Alpha", "url": "https://example.com/a", "price": None},
            "B-1": {"name": "Beta", "url": "https://example.com/b", "price": None},
        }
        first_future = mock.Mock()
        first_future.result.return_value = 11.0
        second_future = mock.Mock()

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_args = None
                self.submit_calls = []

            def submit(self, fn, url, name, sku=None):
                self.submit_calls.append((fn, url, name, sku))
                return first_future if len(self.submit_calls) == 1 else second_future

            def shutdown(self, wait=True, cancel_futures=False):
                self.shutdown_args = {"wait": wait, "cancel_futures": cancel_futures}

        def fake_as_completed(future_map, timeout=None):
            yield first_future
            raise TimeoutError()

        fake_executor = FakeExecutor()

        with (
            mock.patch("msrp_jobs.ThreadPoolExecutor", return_value=fake_executor),
            mock.patch("msrp_jobs.as_completed", new=fake_as_completed),
        ):
            fetched, timed_out = msrp_jobs._fetch_live_prices_by_sku(
                by_sku,
                workers=2,
                log_fn=lambda _msg: None,
            )

        self.assertEqual(fetched, 1)
        self.assertEqual(timed_out, 1)
        self.assertEqual(by_sku["A-1"]["price"], 11.0)
        self.assertIsNone(by_sku["B-1"]["price"])
        first_future.result.assert_called_once()
        second_future.cancel.assert_called_once()
        self.assertEqual(
            fake_executor.shutdown_args, {"wait": False, "cancel_futures": True}
        )

    def test_msrp_diff_flags_cutco_outage_when_no_prices_return(self):
        job_file = f"{self.temp_dir.name}/msrp_job.json"
        with (
            mock.patch.object(msrp_jobs, "_MSRP_JOB_FILE", job_file),
            mock.patch.object(
                msrp_jobs, "_build_msrp_price_targets_from_db"
            ) as targets_mock,
            mock.patch.object(
                msrp_jobs, "_fetch_live_prices_by_sku", return_value=(0, 0)
            ),
            mock.patch.object(msrp_jobs, "record_activity"),
            mock.patch.object(msrp_jobs, "check_wishlist_targets", return_value=[]),
        ):
            targets_mock.return_value = {
                "A-1": {
                    "name": "Alpha",
                    "url": "https://www.cutco.com/p/a",
                    "price": None,
                },
            }
            msrp_jobs._run_msrp_diff_job(self.app, False)

        with open(job_file, "r", encoding="utf-8") as fh:
            job_data = json.load(fh)

        self.assertEqual(job_data["status"], "error")
        self.assertIn("cutco.com", job_data["error"].lower())

    def test_msrp_price_targets_prefer_db_urls_for_known_skus(self):
        live_items = [
            {
                "sku": "125",
                "name": "Medium Cutting Board",
                "url": "https://www.cutco.com/shop/cutting-boards",
                "price": None,
            },
            {
                "sku": "999",
                "name": "New Thing",
                "url": "https://www.cutco.com/p/new-thing",
                "price": None,
            },
        ]

        with self.app.app_context():
            with mock.patch.object(msrp_jobs.Item, "query") as query_mock:
                query_mock.filter.return_value.all.return_value = [
                    mock.Mock(
                        sku="125",
                        cutco_url="https://www.cutco.com/p/medium-cutting-board",
                    ),
                ]
                targets = msrp_jobs._build_msrp_price_targets(live_items)

        self.assertEqual(
            targets["125"]["url"], "https://www.cutco.com/p/medium-cutting-board"
        )
        self.assertEqual(targets["999"]["url"], "https://www.cutco.com/p/new-thing")

    def test_msrp_price_targets_from_db_uses_stored_item_urls(self):
        with self.app.app_context():
            item_a = Item(
                name="Knife A", sku="A-1", cutco_url="https://www.cutco.com/p/knife-a"
            )
            item_b = Item(
                name="Knife B",
                sku="125",
                cutco_url="https://www.cutco.com/p/cutting-boards/125",
            )
            db.session.add_all([item_a, item_b])
            db.session.commit()

            targets = msrp_jobs._build_msrp_price_targets_from_db(Item.query.all())

        self.assertEqual(targets["A-1"]["url"], "https://www.cutco.com/p/knife-a")
        self.assertEqual(
            targets["125"]["url"], "https://www.cutco.com/p/medium-cutting-board"
        )
