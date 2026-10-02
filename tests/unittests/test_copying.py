"""Normal copying and source draining."""

# pylint: disable=protected-access

# Test setup is intentionally repeated across thematic modules.
# pylint: disable=duplicate-code

import argparse
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from logsync.cli import positive_int
from logsync.sync import SyncStats, _Context, _find_start_of_data, _sync_range
from logsync.destination import get_user_anchor_path as real_get_user_anchor_path
from logsync.metrics import metrics

from tests.unittests.support import reset_metrics, sync_path


class CopyingTests(unittest.TestCase):
    def test_data_transfer_defers_only_sigint(self) -> None:
        """Defer SIGINT but not SIGTERM across the copy, sync, punch, and source-sync transaction."""
        with (
            patch("logsync.sync.critical_section") as critical_section,
            patch("logsync.sync._copy_range", return_value=8),
            patch("logsync.sync.os.fsync"),
            patch("logsync.sync.punch_hole"),
        ):
            ctx = _Context(
                progress=SyncStats(),
                user="alice",
                chunk_size=8,
                filename_limit=255,
            )
            _sync_range(
                ctx,
                10,
                11,
                start_of_data=0,
                end_of_data=8,
            )

        critical_section.assert_called_once_with(False)

    def test_skips_punched_holes_on_later_passes(self) -> None:
        """Copy only newly appended data on later passes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            log = source / "app.log"
            log.write_bytes(b"one...\n")

            sync_path(source, dest)
            with log.open("ab") as file:
                file.write(b"two...\n")

            sync_path(source, dest)

            self.assertEqual((dest / "app.log").read_bytes(), b"one...\ntwo...\n")

    def test_skips_sparse_hole_and_allocated_zero_prefix(self) -> None:
        """Skip both a sparse leading hole and allocated NUL padding."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            log = source / "app.log"
            with log.open("wb") as file:
                file.seek(4096)
                file.write(b"\0" * 8 + b"live...\n")
            dest.mkdir()
            (dest / "app.log").write_bytes(b"drained\n")

            sync_path(source, dest)

            self.assertEqual((dest / "app.log").read_bytes(), b"drained\nlive...\n")

    def test_avoids_destination_for_files_without_complete_records(self) -> None:
        """Avoid credentials and destination access for files without complete records."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "empty.log").write_bytes(b"")
            (source / "drained.log").write_bytes(b"\0" * 16)
            (source / "partial.log").write_bytes(b"partial")

            credential_setup = MagicMock()
            with patch("logsync.destination.get_user_anchor_path") as destination_path:
                stats = sync_path(source, dest, credential_setup=credential_setup)

            destination_path.assert_not_called()
            credential_setup.assert_not_called()
            self.assertEqual(stats.files_scanned, 3)
            self.assertEqual(list(dest.iterdir()), [])

    def test_selects_credentials_once_before_destination_access(self) -> None:
        """Select credentials once before calculating the anchor for each destination access."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "a.log").write_bytes(b"one...\n")
            (source / "b.log").write_bytes(b"two...\n")
            events: list[str] = []

            def select_credentials() -> None:
                events.append("credentials")

            credential_setup = MagicMock(side_effect=select_credentials)

            def record_destination(*args, **kwargs):
                events.append("destination")
                return real_get_user_anchor_path(*args, **kwargs)

            with patch(
                "logsync.destination.get_user_anchor_path",
                side_effect=record_destination,
            ):
                sync_path(source, dest, credential_setup=credential_setup)

            self.assertEqual(events, ["credentials", "destination", "destination"])

    def test_selects_credentials_before_acquiring_a_collection_lease(self) -> None:
        """Complete lazy credential setup before acquiring and releasing a source lease as the user."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            events: list[str] = []

            with (
                patch("logsync.sync.get_write_lease", side_effect=lambda _fd: events.append("lease:acquire") or 0),
                patch("logsync.sync.verify_lease_unbroken", side_effect=lambda _fd, _epoch: events.append("lease:verify") or True),
                patch("logsync.sync.release_write_lease", side_effect=lambda _fd: events.append("lease:release")),
            ):
                sync_path(source, dest, credential_setup=lambda: events.append("credentials"))

            self.assertEqual(events, ["credentials", "lease:acquire", "lease:verify", "lease:release"])

    def test_uses_one_sync_and_prefix_punch(self) -> None:
        """Sync the destination once before punching one source prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            data = b"one...\ntwo...\n"
            (source / "app.log").write_bytes(data)
            real_fsync = os.fsync
            synced_paths: list[Path] = []

            def record_fsync(fd: int) -> None:
                synced_paths.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
                real_fsync(fd)

            with (
                patch("logsync.sync.os.fsync", side_effect=record_fsync),
                patch("logsync.sync.punch_hole") as punch,
            ):
                sync_path(source, dest)

            self.assertEqual(synced_paths.count(dest / "app.log"), 1)
            self.assertEqual(
                sum(
                    path.name == "app.log" and source in path.parents
                    for path in synced_paths
                ),
                1,
            )
            self.assertEqual(punch.call_count, 1)
            self.assertEqual(punch.call_args.args[1:], (0, len(data)))

    def test_configured_chunk_size_limits_destination_writes(self) -> None:
        """Limit append writes to the configured chunk size."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            data = b"one...\ntwo...\n"
            (source / "app.log").write_bytes(data)
            real_write = os.write
            write_sizes: list[int] = []

            def record_write(fd: int, buffer: bytes) -> int:
                write_sizes.append(len(buffer))
                return real_write(fd, buffer)

            with patch("logsync.sync.os.write", side_effect=record_write):
                stats = sync_path(source, dest, chunk_size=4)

            self.assertEqual(stats.errors, 0)
            self.assertGreater(len(write_sizes), 1)
            self.assertLessEqual(max(write_sizes), 4)
            self.assertEqual((dest / "app.log").read_bytes(), data)

    def test_rejects_nonpositive_chunk_size(self) -> None:
        """Reject nonpositive library and command-line chunk sizes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()

            with self.assertRaises(ValueError):
                sync_path(source, root / "dest", chunk_size=0)

        self.assertEqual(positive_int("4"), 4)
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_int("0")

    def test_operation_errors_identify_the_failed_stage(self) -> None:
        """Distinguish failures from different measured filesystem operations."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            destination = Path(tmp) / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")

            with (
                patch("logsync.sync.os.pread", side_effect=OSError("injected")),
                self.assertLogs("logsync.sync", level="ERROR") as captured,
            ):
                stats = sync_path(source, destination)
            self.assertEqual(stats.errors, 1)
            self.assertIn("source pread_nonzero failed", captured.output[0])

            with (
                patch("logsync.sync._find_start_of_data", return_value=0),
                patch("logsync.sync.os.pread", side_effect=OSError("injected")),
                self.assertLogs("logsync.sync", level="ERROR") as captured,
            ):
                stats = sync_path(source, destination)
            self.assertEqual(stats.errors, 1)
            self.assertIn("source pread_records failed", captured.output[0])

    def test_seek_end_of_data_is_expected(self) -> None:
        """Treat SEEK_DATA end-of-data as a quiet successful metric result."""
        reset_metrics()
        with (
            patch("logsync.sync.os.lseek", side_effect=OSError(errno.ENXIO, "no more data")),
            self.assertNoLogs("logsync.sync", level="WARNING"),
        ):
            ctx = _Context(
                progress=SyncStats(),
                user="alice",
                chunk_size=1024,
                filename_limit=255,
            )
            self.assertIsNone(_find_start_of_data(ctx, 123))

        seek_data = next(
            sample
            for sample in metrics.samples()
            if sample.name == "logsync_operation_count_total" and sample.labels.get("operation") == "seek_data"
        )
        self.assertEqual(seek_data.labels["outcome"], "success")

    def test_finds_start_of_data_across_zero_chunks(self) -> None:
        """Scan allocated zero-filled chunks to find the later start of data."""
        with tempfile.TemporaryFile() as source:
            source.write(b"\0\0live")
            source.flush()
            ctx = _Context(
                progress=SyncStats(),
                user="",
                chunk_size=2,
                filename_limit=255,
            )
            self.assertEqual(
                _find_start_of_data(ctx, source.fileno()),
                2,
            )

        with tempfile.TemporaryFile() as source:
            source.write(b"\0" * 4)
            source.flush()
            self.assertIsNone(
                _find_start_of_data(ctx, source.fileno())
            )

    def test_zero_byte_write_is_an_error(self) -> None:
        """Reject a zero-length append write."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")

            with patch("logsync.sync.os.write", return_value=0):
                stats = sync_path(source, dest)

            self.assertEqual(stats.errors, 1)
