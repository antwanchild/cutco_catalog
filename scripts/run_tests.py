"""Run the full unittest suite and save the ten slowest test timings."""

import time
import unittest
from pathlib import Path
from typing import cast


class TimingResult(unittest.TextTestResult):
    """Record elapsed time for each test without changing result handling."""

    def __init__(self, *args, **kwargs):
        """Initialize the result and timing collection."""
        super().__init__(*args, **kwargs)
        self.timings = []

    def startTest(self, test):
        """Start the timer before running a test."""
        self._started_at = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        """Record the elapsed time when a test finishes."""
        self.timings.append((time.perf_counter() - self._started_at, test.id()))
        super().stopTest(test)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover("tests")
    result = cast(
        TimingResult,
        unittest.TextTestRunner(verbosity=2, resultclass=TimingResult).run(suite),
    )
    report = "Slowest tests:\n" + "".join(
        f"{duration:0.3f}s {test_id}\n"
        for duration, test_id in sorted(result.timings, reverse=True)[:10]
    )
    Path("test-timings.txt").write_text(report)
    print(report)
    raise SystemExit(0 if result.wasSuccessful() else 1)
