"""Metrics emitted by measured filesystem operations."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logsync.destination import safe_open_dir
from logsync.metrics import MetricSample, metrics
from logsync.runtime import discover_users

from tests.unittests.support import reset_metrics, sync_path


_OPERATION = "logsync_operation"


def _count_samples() -> tuple[MetricSample, ...]:
    """Return only completed-measurement counter samples."""
    return tuple(sample for sample in metrics.samples() if sample.name.endswith("_count_total"))


class OperationMetricsTests(unittest.TestCase):
    def test_root_directory_open_uses_existing_operation_metric(self) -> None:
        """Record opening the filesystem root under the existing destination open-directory operation."""
        reset_metrics()

        fd = safe_open_dir("/")
        os.close(fd)

        opened = next(
            sample
            for sample in metrics.samples()
            if sample.name == f"{_OPERATION}_count_total"
            and sample.labels.get("filesystem") == "destination"
            and sample.labels.get("operation") == "open_directory"
        )
        self.assertEqual(opened.labels["outcome"], "success")
        self.assertEqual(opened.value, 1)

    def test_records_aggregate_operation_metrics(self) -> None:
        """Record operation metrics without user, path, or filename labels."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            reset_metrics()

            sync_path(source, dest)

            samples = metrics.samples()
            write = next(
                sample
                for sample in samples
                if sample.name == f"{_OPERATION}_count_total"
                and sample.labels.get("operation") == "write"
                and sample.labels.get("outcome") == "success"
            )
            destination_fsync = next(
                sample
                for sample in samples
                if sample.name == f"{_OPERATION}_count_total"
                and sample.labels.get("filesystem") == "destination"
                and sample.labels.get("operation") == "fsync"
            )
            lease = next(
                sample
                for sample in samples
                if sample.name == f"{_OPERATION}_count_total"
                and sample.labels.get("filesystem") == "source"
                and sample.labels.get("operation") == "lease"
            )
            self.assertNotIn("user", write.labels)
            self.assertEqual(write.value, 1)
            self.assertEqual(destination_fsync.value, 1)
            self.assertEqual(lease.value, 3)
            errors = [sample for sample in _count_samples() if sample.labels.get("outcome") == "error"]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].name, f"{_OPERATION}_count_total")
            self.assertEqual(errors[0].labels["filesystem"], "source")
            self.assertEqual(errors[0].labels["operation"], "open_file")
            self.assertTrue(all("app.log" not in value for sample in samples for value in sample.labels.values()))

    def test_records_user_discovery_scan_operation(self) -> None:
        """Record opening the source-user discovery scan under the existing source scandir operation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alice").mkdir()
            reset_metrics()

            self.assertEqual(discover_users(f"{root}/"), ["alice"])

            scan = next(
                sample
                for sample in _count_samples()
                if sample.name == f"{_OPERATION}_count_total"
                and sample.labels.get("filesystem") == "source"
                and sample.labels.get("operation") == "scandir"
            )
            self.assertEqual(scan.labels["outcome"], "success")
            self.assertEqual(scan.value, 1)

    def test_missing_active_file_is_only_a_low_level_operation_error(self) -> None:
        """Handle a missing active generation while retaining the failed source-open metric."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "logsync-gc-app.log").write_bytes(b"one...\n")
            reset_metrics()

            stats = sync_path(source, dest)

            errors = [sample for sample in _count_samples() if sample.labels.get("outcome") == "error"]
            self.assertEqual(stats.errors, 0)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].name, f"{_OPERATION}_count_total")
            self.assertEqual(errors[0].labels["filesystem"], "source")
            self.assertEqual(errors[0].labels["operation"], "open_file")

    def test_records_failed_destination_operation(self) -> None:
        """Record a failed destination write under the error outcome."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            reset_metrics()

            with patch("logsync.sync.os.write", side_effect=OSError("injected write failure")):
                stats = sync_path(source, dest)

            failed = next(
                sample
                for sample in _count_samples()
                if sample.name == f"{_OPERATION}_count_total"
                and sample.labels.get("operation") == "write"
                and sample.labels["outcome"] == "error"
            )
            self.assertEqual(stats.errors, 1)
            self.assertEqual(failed.value, 1)
