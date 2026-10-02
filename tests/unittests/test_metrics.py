"""Tests for logsync's in-memory telemetry and file publisher."""

# pylint: disable=protected-access

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from logsync.metrics import metrics
from logsync.metrics_file import MetricsFilePublisher, _atomic_write, prometheus_text
from logsync.sync import SyncStats
from tests.unittests.support import reset_metrics


_ENTRIES = "logsync_entries"
_USER_ENTRIES = "logsync_user_entries"
_OPERATION = "logsync_operation"
_PHASE = "logsync_phase"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        """Advance the synthetic monotonic clock."""
        self.now += nanoseconds


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        clock = patch("logsync.metrics.time.perf_counter_ns", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        reset_metrics()
        self.metrics = metrics

    def test_records_cumulative_operation_statistics(self) -> None:
        """Record cumulative operation counts and durations."""
        for duration in (10, 20, 40):
            with self.metrics.operation("destination", "write"):
                self.clock.advance(duration)

        samples = {sample.name: sample for sample in self.metrics.samples()}
        self.assertEqual(
            samples[f"{_OPERATION}_count_total"].labels,
            {"filesystem": "destination", "operation": "write", "outcome": "success"},
        )
        self.assertEqual(samples[f"{_OPERATION}_count_total"].value, 3)
        self.assertEqual(samples[f"{_OPERATION}_duration_seconds_total"].value, 70 / 1_000_000_000)

    def test_aggregates_phase_statistics_across_users(self) -> None:
        """Aggregate equal phase labels across users while retaining contextual errors."""
        with self.metrics.phase("alice", "source_scan"):
            self.clock.advance(10)
        with self.metrics.phase("bob", "source_scan"):
            self.clock.advance(20)

        count = next(sample for sample in self.metrics.samples() if sample.name == f"{_PHASE}_count_total")
        duration = next(sample for sample in self.metrics.samples() if sample.name == f"{_PHASE}_duration_seconds_total")
        self.assertEqual(count.labels, {"phase": "source_scan", "outcome": "success"})
        self.assertEqual(count.value, 2)
        self.assertEqual(duration.value, 30 / 1_000_000_000)

    def test_separates_failed_measurements_and_propagates_exception(self) -> None:
        """Record failed durations while preserving and annotating the original exception."""
        injected = OSError("injected")
        with self.assertRaises(OSError) as raised:
            with self.metrics.phase("alice", "source_scan"):
                self.clock.advance(7)
                raise injected

        self.assertIs(raised.exception, injected)
        self.assertEqual(raised.exception.__notes__, ["source_scan for 'alice' failed"])

        duration = next(sample for sample in self.metrics.samples() if sample.name == f"{_PHASE}_duration_seconds_total")
        self.assertEqual(duration.labels["outcome"], "error")
        self.assertEqual(duration.value, 7 / 1_000_000_000)

    def test_replaces_and_aggregates_complete_inventory(self) -> None:
        """Replace gauges atomically, aggregate entry types, and remove users absent from the new pass."""
        with self.metrics.inventory():
            self.metrics.record_sync("alice", SyncStats(files_active=3, files_gc=1, directories_scanned=2))
            self.metrics.record_sync("bob", SyncStats(files_active=4, directories_scanned=3))

        samples = self.metrics.samples()
        active = next(sample for sample in samples if sample.name == _ENTRIES and sample.labels.get("type") == "active")
        entries = {sample.labels["user"]: sample.value for sample in samples if sample.name == _USER_ENTRIES}
        self.assertEqual(active.labels, {"type": "active"})
        self.assertEqual(active.value, 7)
        self.assertEqual(entries, {"alice": 6, "bob": 7})

        with self.metrics.inventory():
            self.metrics.record_sync("alice", SyncStats(files_active=2))

        active = [sample for sample in self.metrics.samples() if sample.name == _ENTRIES and sample.labels.get("type") == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].labels, {"type": "active"})
        self.assertEqual(active[0].value, 2)
        entries = [sample for sample in self.metrics.samples() if sample.name == _USER_ENTRIES]
        self.assertEqual([(sample.labels, sample.value) for sample in entries], [({"user": "alice"}, 2)])

    def test_direct_records_replace_user_contribution(self) -> None:
        """Replace one user's direct contribution without double-counting other users."""
        self.metrics.record_sync("alice", SyncStats(files_active=3))
        self.metrics.record_sync("bob", SyncStats(files_active=4))
        self.metrics.record_sync("alice", SyncStats(files_active=2))

        samples = self.metrics.samples()
        active = next(sample for sample in samples if sample.name == _ENTRIES and sample.labels.get("type") == "active")
        entries = {sample.labels["user"]: sample.value for sample in samples if sample.name == _USER_ENTRIES}
        self.assertEqual(active.value, 6)
        self.assertEqual(entries, {"alice": 2, "bob": 4})

    def test_inventory_scope_discards_values_when_work_raises(self) -> None:
        """Preserve the last complete inventory when a new pass exits with an exception."""
        self.metrics.record_sync("bob", SyncStats(files_active=2))

        with self.assertRaisesRegex(OSError, "injected"):
            with self.metrics.inventory():
                self.metrics.record_sync("alice", SyncStats(files_active=3))
                raise OSError("injected")

        active = next(sample for sample in self.metrics.samples() if sample.name == _ENTRIES and sample.labels.get("type") == "active")
        entries = {sample.labels["user"]: sample.value for sample in self.metrics.samples() if sample.name == _USER_ENTRIES}
        self.assertEqual(active.value, 2)
        self.assertEqual(entries, {"bob": 2})

    def test_empty_inventory_preserves_zero_aggregate_series(self) -> None:
        """Export every aggregate entry type at zero initially and after a pass with no users."""
        initial_entries = {
            sample.labels["type"]: sample.value
            for sample in self.metrics.samples()
            if sample.name == _ENTRIES
        }
        self.assertEqual(initial_entries, {"active": 0, "dir": 0, "gc": 0})

        with self.metrics.inventory():
            pass

        entries = {
            sample.labels["type"]: sample.value
            for sample in self.metrics.samples()
            if sample.name == _ENTRIES
        }
        self.assertEqual(entries, {"active": 0, "dir": 0, "gc": 0})
        self.assertFalse(any(sample.name == _USER_ENTRIES for sample in self.metrics.samples()))

    def test_renders_prometheus_text(self) -> None:
        """Render metadata, cumulative values, escaped labels, and gauges."""
        with self.metrics.operation("destination", "write"):
            self.clock.advance(10)
        self.metrics.record_sync('ali"ce', SyncStats(files_active=3))

        rendered = prometheus_text(self.metrics.samples())
        self.assertIn("# TYPE logsync_operation_count_total counter\n", rendered)
        self.assertIn('user="ali\\"ce"', rendered)
        self.assertIn('logsync_entries{type="active"} 3\n', rendered)
        self.assertIn('logsync_user_entries{user="ali\\"ce"} 3\n', rendered)

    def test_preserves_the_complete_prometheus_series_schema(self) -> None:
        """Render every established metric family with its stable name, type, and labels."""
        measurements = (
            lambda: self.metrics.global_phase("user_discovery"),
            lambda: self.metrics.user("alice"),
            lambda: self.metrics.phase("alice", "source_scan"),
            lambda: self.metrics.operation("source", "pread_copy"),
        )
        for measurement in measurements:
            with measurement():
                self.clock.advance(1)
        self.metrics.record_sync("alice", SyncStats(files_active=3, directories_scanned=2, directories_removed=1))

        rendered = prometheus_text(self.metrics.samples())
        types = {
            line.split()[2]: line.split()[3]
            for line in rendered.splitlines()
            if line.startswith("# TYPE ")
        }
        self.assertEqual(
            types,
            {
                "logsync_global_phase_count_total": "counter",
                "logsync_global_phase_duration_seconds_total": "counter",
                "logsync_user_count_total": "counter",
                "logsync_user_duration_seconds_total": "counter",
                "logsync_phase_count_total": "counter",
                "logsync_phase_duration_seconds_total": "counter",
                "logsync_operation_count_total": "counter",
                "logsync_operation_duration_seconds_total": "counter",
                "logsync_entries": "gauge",
                "logsync_user_entries": "gauge",
            },
        )
        self.assertIn('logsync_global_phase_count_total{phase="user_discovery",outcome="success"}', rendered)
        self.assertIn('logsync_phase_count_total{phase="source_scan",outcome="success"}', rendered)
        self.assertIn('logsync_operation_count_total{filesystem="source",operation="pread_copy",outcome="success"}', rendered)
        self.assertIn('logsync_entries{type="active"} 3', rendered)
        self.assertIn('logsync_entries{type="gc"} 0', rendered)
        self.assertIn('logsync_entries{type="dir"} 2', rendered)
        self.assertIn('logsync_user_entries{user="alice"} 5', rendered)
        self.assertNotIn("logsync_files", rendered)
        self.assertNotIn("logsync_dirs", rendered)

    def test_metrics_file_publisher_observes_minimum_interval(self) -> None:
        """Atomically publish the first snapshot and enforce the minimum update interval."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.prom"
            publisher = MetricsFilePublisher(str(path), 10, clock=self.clock)
            with self.metrics.phase("alice", "scan"):
                self.clock.advance(1)
            self.assertTrue(publisher.publish_if_due())
            first = path.read_text(encoding="ascii")
            self.clock.advance(9)
            self.assertFalse(publisher.publish_if_due())
            self.assertEqual(path.read_text(encoding="ascii"), first)
            self.clock.advance(1)
            self.assertTrue(publisher.publish_if_due())

    def test_metrics_cleanup_does_not_mask_primary_error(self) -> None:
        """Preserve publication failures when temporary-file cleanup also fails."""
        temporary = Path("/metrics/.metrics.prom.tmp")
        with (
            patch("logsync.metrics_file.tempfile.mkstemp", return_value=(42, temporary)),
            patch("logsync.metrics_file.os.fchmod", side_effect=OSError("primary")),
            patch("logsync.metrics_file.os.close", side_effect=OSError("close cleanup")),
            patch("logsync.metrics_file.os.unlink", side_effect=OSError("unlink cleanup")),
            self.assertLogs("logsync.metrics_file", level="ERROR") as captured,
            self.assertRaisesRegex(OSError, "primary"),
        ):
            _atomic_write("/metrics/metrics.prom", "metrics")
        self.assertIn("failed to close temporary metrics file", captured.output[0])
        self.assertIn("failed to remove temporary metrics file", captured.output[1])

    def test_metrics_publication_defers_termination_signals(self) -> None:
        """Defer SIGINT and SIGTERM across atomic metrics publication and cleanup."""
        with TemporaryDirectory() as tmp, patch("logsync.metrics_file.critical_section") as critical_section:
            _atomic_write(str(Path(tmp) / "metrics.prom"), "metrics")
        critical_section.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
