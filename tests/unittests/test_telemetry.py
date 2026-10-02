"""Metrics emitted by synchronization and command-line passes."""

# pylint: disable=protected-access

# Test setup is intentionally repeated across thematic modules.
# pylint: disable=duplicate-code

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from logsync.cli import main
from logsync.metrics import MetricSample, metrics
from logsync.sync import SyncStats

from tests.unittests.support import reset_metrics, sync_path


_ENTRIES = "logsync_entries"
_USER_ENTRIES = "logsync_user_entries"
_GLOBAL_PHASE = "logsync_global_phase"
_PHASE = "logsync_phase"
_USER = "logsync_user"


def _count_samples() -> tuple[MetricSample, ...]:
    """Return only completed-measurement counter samples."""
    return tuple(sample for sample in metrics.samples() if sample.name.endswith("_count_total"))


class TelemetryTests(unittest.TestCase):
    def test_telemetry_does_not_change_sync_result(self) -> None:
        """Return identical synchronization statistics for equivalent instrumented calls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain_source = root / "plain-source"
            measured_source = root / "measured-source"
            plain_source.mkdir()
            measured_source.mkdir()
            (plain_source / "app.log").write_bytes(b"one...\n")
            (measured_source / "app.log").write_bytes(b"one...\n")

            plain = sync_path(plain_source, root / "plain-dest")
            measured = sync_path(measured_source, root / "measured-dest")

            self.assertEqual(measured, plain)

    def test_records_latest_sync_values(self) -> None:
        """Record aggregate entry types and zero-inclusive per-user entries for completed calls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            reset_metrics()

            first = sync_path(source, dest)
            second = sync_path(source, dest)

            samples = {
                (sample.name, sample.labels.get("type")): sample
                for sample in metrics.samples()
                if sample.metric_type == "gauge"
            }
            self.assertEqual((first.files_active, first.files_gc), (1, 0))
            self.assertEqual((second.files_active, second.files_gc), (0, 1))
            self.assertEqual(samples[(_ENTRIES, "active")].value, 0)
            self.assertEqual(samples[(_ENTRIES, "gc")].value, 1)
            self.assertGreater(samples[(_ENTRIES, "dir")].value, 0)
            entries = next(sample for sample in metrics.samples() if sample.name == _USER_ENTRIES)
            self.assertEqual(entries.labels, {"user": "dest"})
            self.assertEqual(entries.value, second.files_gc + second.directories_scanned)

    def test_credential_failure_is_recorded_in_its_phase(self) -> None:
        """Record lazy credential failure in its phase and synchronization result."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            reset_metrics()

            stats = sync_path(
                source,
                dest,
                credential_setup=MagicMock(side_effect=OSError("missing token")),
            )

            samples = _count_samples()
            self.assertTrue(
                any(
                    sample.name == f"{_PHASE}_count_total"
                    and sample.labels.get("phase") == "credential_setup"
                    and sample.labels["outcome"] == "error"
                    for sample in samples
                )
            )
            self.assertEqual(stats.errors, 1)

    def test_global_user_and_phase_timers_are_inclusive(self) -> None:
        """Record global, per-user, and aggregate phase timers independently with natural nesting."""
        timestamps = iter(range(8))
        reset_metrics()

        with patch("logsync.metrics.time.perf_counter_ns", side_effect=lambda: next(timestamps)):
            with metrics.inventory():
                with metrics.global_phase("user_discovery"):
                    pass
                with metrics.global_phase("user_processing"):
                    with metrics.user("alice"):
                        with metrics.phase("alice", "identity_resolution"):
                            pass

        samples = tuple(sample for sample in metrics.samples() if sample.name.endswith("_duration_seconds_total"))
        user_duration = sum(
            sample.value
            for sample in samples
            if sample.name == f"{_USER}_duration_seconds_total" and sample.labels["user"] == "alice"
        )
        processing_duration = sum(
            sample.value
            for sample in samples
            if sample.name == f"{_GLOBAL_PHASE}_duration_seconds_total" and sample.labels["phase"] == "user_processing"
        )
        self.assertGreater(processing_duration, user_duration)
        self.assertEqual(
            {
                sample.labels["phase"]
                for sample in samples
                if sample.name == f"{_GLOBAL_PHASE}_duration_seconds_total"
            },
            {"user_discovery", "user_processing"},
        )
        self.assertEqual(
            {
                sample.labels["phase"]
                for sample in samples
                if sample.name == f"{_PHASE}_duration_seconds_total"
            },
            {"identity_resolution"},
        )

    def test_failed_nested_scopes_record_error_outcomes(self) -> None:
        """Record error outcomes when an exception leaves nested phase, user, and global scopes."""
        timestamps = iter(range(6))
        reset_metrics()

        with patch("logsync.metrics.time.perf_counter_ns", side_effect=lambda: next(timestamps)):
            with self.assertRaisesRegex(OSError, "injected"):
                with metrics.inventory():
                    with metrics.global_phase("user_processing"):
                        with metrics.user("alice"):
                            with metrics.phase("alice", "source_setup"):
                                raise OSError("injected")

        samples = _count_samples()
        for name in (_GLOBAL_PHASE, _USER, _PHASE):
            self.assertTrue(any(sample.name == f"{name}_count_total" and sample.labels["outcome"] == "error" for sample in samples))

    def test_reported_user_error_marks_user_and_processing_as_failed(self) -> None:
        """Propagate returned file-error totals into user and processing metric outcomes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()
            reset_metrics()

            with (
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch("logsync.runtime.sync_user", return_value=SyncStats(errors=1)),
            ):
                result = main(
                    [
                        "--source-prefix",
                        f"{source_root}/",
                        "--dest-prefix",
                        f"{dest_root}/",
                        "--user",
                        "alice",
                    ]
                )

            samples = _count_samples()
            self.assertEqual(result, 1)
            self.assertTrue(
                any(
                    sample.name == f"{_USER}_count_total" and sample.labels["outcome"] == "error"
                    for sample in samples
                )
            )
            self.assertTrue(
                any(
                    sample.name == f"{_GLOBAL_PHASE}_count_total"
                    and sample.labels["phase"] == "user_processing"
                    and sample.labels["outcome"] == "error"
                    for sample in samples
                )
            )

    def test_publication_includes_completed_pass_phases(self) -> None:
        """Publish user values and the completed global phases that produced them."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            metrics_file = root / "metrics.prom"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()
            reset_metrics()

            with (
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
            ):
                result = main(
                    [
                        "--source-prefix",
                        f"{source_root}/",
                        "--dest-prefix",
                        f"{dest_root}/",
                        "--user",
                        "alice",
                        "--metrics-file",
                        str(metrics_file),
                    ]
                )

            self.assertEqual(result, 0)
            published = metrics_file.read_text(encoding="ascii")
            self.assertIn('logsync_global_phase_duration_seconds_total{phase="user_processing"', published)
            self.assertIn('logsync_entries{type="active"} 0', published)
            self.assertIn('logsync_user_entries{user="alice"} 1', published)
            self.assertNotIn('phase="metrics_publication"', published)
            samples = _count_samples()
            self.assertTrue(
                any(
                    sample.name == f"{_GLOBAL_PHASE}_count_total"
                    and sample.labels["phase"] == "user_processing"
                    for sample in samples
                )
            )
            self.assertTrue(
                any(
                    sample.name == f"{_GLOBAL_PHASE}_count_total"
                    and sample.labels["phase"] == "metrics_publication"
                    for sample in samples
                )
            )

    def test_runtime_publication_failure_does_not_fail_the_pass(self) -> None:
        """Log a publication failure without changing a successful pass outcome."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()
            reset_metrics()
            publisher = MagicMock()
            publisher.publish_if_due.side_effect = OSError("injected failure")

            with (
                patch(
                    "logsync.runtime.MetricsFilePublisher",
                    return_value=publisher,
                ),
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch("logsync.runtime.sync_user", return_value=SyncStats()),
                patch("logsync.runtime.logging.error") as log_error,
            ):
                result = main(
                    [
                        "--source-prefix",
                        f"{source_root}/",
                        "--dest-prefix",
                        f"{dest_root}/",
                        "--user",
                        "alice",
                        "--metrics-file",
                        str(root / "metrics.prom"),
                    ]
                )

            self.assertEqual(result, 0)
            log_error.assert_called_once_with(
                f"failed to publish metrics to {root / 'metrics.prom'}: OSError: injected failure"
            )
            samples = _count_samples()
            self.assertTrue(
                any(
                    sample.name == f"{_GLOBAL_PHASE}_count_total"
                    and sample.labels["phase"] == "metrics_publication"
                    and sample.labels["outcome"] == "error"
                    for sample in samples
                )
            )
            self.assertTrue(
                any(
                    sample.name == f"{_GLOBAL_PHASE}_count_total"
                    and sample.labels["phase"] == "user_processing"
                    and sample.labels["outcome"] == "success"
                    for sample in samples
                )
            )

    def test_startup_publication_failure_remains_fatal(self) -> None:
        """Keep publisher initialization failure fatal before synchronization starts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with (
                patch(
                    "logsync.runtime.MetricsFilePublisher",
                    side_effect=OSError("injected failure"),
                ),
                self.assertRaisesRegex(OSError, "injected failure"),
            ):
                main(
                    [
                        "--source-prefix",
                        f"{root}/source/",
                        "--dest-prefix",
                        f"{root}/home/",
                        "--metrics-file",
                        str(root / "metrics.prom"),
                    ]
                )

    def test_recurring_loop_records_scheduled_sleep(self) -> None:
        """Record recurring-loop sleep as a global phase outside active pass work."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()
            reset_metrics()

            with (
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch(
                    "logsync.runtime.sync_user",
                    side_effect=(SyncStats(), SystemExit()),
                ),
                patch(
                    "logsync.runtime.time.monotonic",
                    side_effect=(0.0, 0.25, 1.0),
                ),
                patch("logsync.runtime.time.sleep") as sleep,
            ):
                with self.assertRaises(SystemExit):
                    main(
                        [
                            "--source-prefix",
                            f"{source_root}/",
                            "--dest-prefix",
                            f"{dest_root}/",
                            "--user",
                            "alice",
                            "--interval",
                            "1",
                        ]
                    )

            sleep.assert_called_once_with(0.75)
            samples = _count_samples()
            self.assertTrue(
                any(
                    sample.name == f"{_GLOBAL_PHASE}_count_total"
                    and sample.labels["phase"] == "sleep"
                    for sample in samples
                )
            )
