"""Source quarantine and garbage collection."""

# pylint: disable=protected-access

import errno
import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logsync.leases import get_write_lease
from logsync.metrics import metrics
from logsync.sync import DEFAULT_CHUNK_SIZE, SyncStats, _Context, _source_files, _sync_file, sync_user

from tests.unittests.support import reset_metrics, sync_path


def _context(progress: SyncStats, *, keep_user_top_dir: bool = True) -> _Context:
    return _Context(
        progress=progress,
        user="test",
        chunk_size=DEFAULT_CHUNK_SIZE,
        filename_limit=255,
        keep_user_top_dir=keep_user_top_dir,
    )


class CollectionTests(unittest.TestCase):
    def test_user_time_limit_stops_before_the_next_logical_file(self) -> None:
        """Count setup and scanning time, finish the current file, and defer the next file."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            logical_files = [str(source / "first.log"), str(source / "second.log")]

            with (
                patch("logsync.sync.time.monotonic", side_effect=(0.0, 0.5, 1.0)),
                patch("logsync.sync._source_files", return_value=logical_files),
                patch("logsync.sync.random.shuffle"),
                patch("logsync.sync._sync_file") as sync_file,
            ):
                stats = sync_user(
                    str(source),
                    ["/destination/"],
                    "alice",
                    owner_uid=1001,
                    owner_gid=100,
                    max_user_seconds=1.0,
                )

            self.assertTrue(stats.deferred)
            self.assertEqual(stats.errors, 0)
            sync_file.assert_called_once()
            self.assertEqual(sync_file.call_args.args[1], logical_files[0])

    def test_user_time_limit_is_not_reported_after_all_files_finish(self) -> None:
        """Leave the deferred status unset when no logical file remains to be skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            logical_file = str(source / "only.log")

            with (
                patch("logsync.sync.time.monotonic", side_effect=(0.0, 0.5)),
                patch("logsync.sync._source_files", return_value=[logical_file]),
                patch("logsync.sync.random.shuffle"),
                patch("logsync.sync._sync_file"),
            ):
                stats = sync_user(
                    str(source),
                    ["/destination/"],
                    "alice",
                    owner_uid=1001,
                    owner_gid=100,
                    max_user_seconds=1.0,
                )

            self.assertFalse(stats.deferred)

    def test_user_files_are_processed_in_shuffled_order(self) -> None:
        """Shuffle logical files once per user pass and process the resulting order."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            first = str(source / "first.log")
            second = str(source / "second.log")

            def reverse(paths: list[str]) -> None:
                paths.reverse()

            with (
                patch("logsync.sync.time.monotonic", side_effect=(0.0, 0.1, 0.2)),
                patch("logsync.sync._source_files", return_value=[first, second]),
                patch("logsync.sync.random.shuffle", side_effect=reverse) as shuffle,
                patch("logsync.sync._sync_file") as sync_file,
            ):
                stats = sync_user(
                    str(source),
                    ["/destination/"],
                    "alice",
                    owner_uid=1001,
                    owner_gid=100,
                    max_user_seconds=1.0,
                )

            self.assertFalse(stats.deferred)
            self.assertEqual(stats.errors, 0)
            shuffle.assert_called_once()
            self.assertEqual(
                [invocation.args[1] for invocation in sync_file.call_args_list],
                [second, first],
            )

    def test_shuffling_eventually_syncs_a_file_after_an_alphabetically_first_failure(self) -> None:
        """Sync a healthy log within ten seeded passes despite an alphabetically earlier log failing every pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            failing = source / "a-failing.log"
            healthy = source / "z-healthy.log"
            failing.write_bytes(b"failing...\n")
            healthy.write_bytes(b"healthy...\n")
            real_open = os.open

            def fail_first_log(path, flags, *args, **kwargs):
                opened_path = Path(path)
                if opened_path.name == failing.name and source in opened_path.parents:
                    raise OSError("injected persistent failure")
                return real_open(path, flags, *args, **kwargs)

            self.assertLess(failing.name, healthy.name)
            with (
                patch("logsync.sync.os.open", side_effect=fail_first_log),
                self.assertLogs("logsync.sync", level="ERROR"),
            ):
                results = [sync_path(source, destination) for _ in range(10)]

            self.assertTrue(all(result.errors == 1 for result in results))
            self.assertEqual((destination / healthy.name).read_bytes(), b"healthy...\n")

    def test_empty_directories_are_removed_one_level_per_pass(self) -> None:
        """Prune empty directories one level per pass while retaining the top-level user directory."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            parent = source / "parent"
            child = parent / "child"
            child.mkdir(parents=True)
            reset_metrics()

            first = SyncStats()
            self.assertEqual(
                _source_files(
                    _context(first),
                    str(source),
                ),
                [],
            )

            self.assertEqual(first.directories_removed, 1)
            self.assertTrue(source.exists())
            self.assertTrue(parent.exists())
            self.assertFalse(child.exists())

            second = SyncStats()
            self.assertEqual(
                _source_files(
                    _context(second),
                    str(source),
                ),
                [],
            )

            self.assertEqual(second.directories_removed, 1)
            self.assertTrue(source.exists())
            self.assertFalse(parent.exists())

    def test_empty_user_top_directory_is_removed_by_default(self) -> None:
        """Remove an empty per-user top-level source directory unless retention is requested."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "alice"
            source.mkdir()
            stats = SyncStats()

            self.assertEqual(_source_files(_context(stats, keep_user_top_dir=False), str(source)), [])

            self.assertFalse(source.exists())
            self.assertEqual(stats.directories_removed, 1)

    def test_empty_directory_removal_races_are_benign(self) -> None:
        """Treat an empty directory disappearing during removal as benign."""
        for error in (
            FileNotFoundError(errno.ENOENT, "injected"),
            OSError(errno.ENOTEMPTY, "injected"),
        ):
            with self.subTest(errno=error.errno):
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp)
                    (source / "empty").mkdir()
                    stats = SyncStats()
                    reset_metrics()

                    with patch("logsync.sync.os.rmdir", side_effect=error):
                        paths = _source_files(
                            _context(stats),
                            str(source),
                        )

                    self.assertEqual(paths, [])
                    self.assertEqual(stats.directories_removed, 0)
                    self.assertEqual(stats.errors, 0)
                    operation = next(
                        sample
                        for sample in metrics.samples()
                        if sample.name == "logsync_operation_count_total"
                        and sample.labels["operation"] == "rmdir"
                    )
                    self.assertEqual(operation.value, 1)
                    self.assertEqual(operation.labels["outcome"], "success")

    def test_empty_directory_removal_failure_does_not_stop_scan(self) -> None:
        """Fail the pass after a pruning error without stopping the file scan."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "empty").mkdir()
            log_file = source / "app.log"
            log_file.write_bytes(b"one...\n")
            stats = SyncStats()
            reset_metrics()

            with (
                patch(
                    "logsync.sync.os.rmdir",
                    side_effect=PermissionError(errno.EACCES, "injected"),
                ),
                self.assertLogs("logsync.sync", level="ERROR"),
            ):
                paths = _source_files(
                    _context(stats),
                    str(source),
                )

            self.assertEqual(paths, [str(log_file)])
            self.assertEqual(stats.directories_removed, 0)
            self.assertEqual(stats.errors, 1)
            operation = next(
                sample
                for sample in metrics.samples()
                if sample.name == "logsync_operation_count_total"
                and sample.labels["operation"] == "rmdir"
            )
            self.assertEqual(operation.labels["outcome"], "error")

    def test_source_scan_returns_unique_logical_paths(self) -> None:
        """Snapshot deduplicated logical paths while retaining physical entry counts."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "z-active.log").write_bytes(b"")
            (source / "logsync-gc-m-gc.log").write_bytes(b"")
            (source / "a-paired.log").write_bytes(b"")
            (source / "logsync-gc-a-paired.log").write_bytes(b"")
            stats = SyncStats()

            paths = _source_files(
                _context(stats),
                str(source),
            )

            self.assertEqual(
                {os.path.basename(path) for path in paths},
                {"a-paired.log", "m-gc.log", "z-active.log"},
            )
            self.assertEqual((stats.files_active, stats.files_gc), (2, 2))

    def test_failed_eager_scan_prevents_file_synchronization(self) -> None:
        """Prevent all file synchronization when the eager source scan fails."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            nested = source / "nested"
            nested.mkdir(parents=True)
            (source / "app.log").write_bytes(b"one...\n")
            real_scandir = os.scandir

            def fail_nested(path):
                if not isinstance(path, int) and Path(path).name == nested.name:
                    raise OSError("injected scan failure")
                return real_scandir(path)

            with (
                patch("logsync.sync.os.scandir", side_effect=fail_nested),
                patch("logsync.sync._sync_file") as sync_file,
            ):
                stats = sync_path(source, destination)

            self.assertEqual(stats.errors, 1)
            sync_file.assert_not_called()

    def test_file_disappearing_before_open_is_benign(self) -> None:
        """Treat a file disappearing between the scan and open as benign."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            active = source / "app.log"
            active.write_bytes(b"one...\n")
            real_open = os.open

            def remove_active(path, flags, *args, **kwargs):
                opened_path = Path(path)
                if opened_path.name == active.name and source in opened_path.parents:
                    opened_path.unlink()
                return real_open(path, flags, *args, **kwargs)

            with patch("logsync.sync.os.open", side_effect=remove_active):
                stats = sync_path(source, destination)

            self.assertEqual(stats.errors, 0)
            self.assertFalse((destination / "app.log").exists())

    def test_gc_and_active_use_one_logical_sync_call(self) -> None:
        """Synchronize paired GC and active generations as one logical file in GC-first order."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            active = source / "app.log"
            active.write_bytes(b"active...\n")
            (source / "logsync-gc-app.log").write_bytes(b"gc...\n")
            reset_metrics()

            with patch("logsync.sync._sync_file", wraps=_sync_file) as sync_file:
                stats = sync_path(source, destination)

            self.assertEqual(sync_file.call_count, 1)
            self.assertEqual(
                os.path.basename(sync_file.call_args.args[1]), active.name
            )
            self.assertEqual(stats.files_scanned, 2)
            self.assertEqual(
                (destination / "app.log").read_bytes(),
                b"gc...\nactive...\n",
            )
            source_opens = next(
                sample
                for sample in metrics.samples()
                if sample.name == "logsync_operation_count_total"
                and sample.labels.get("filesystem") == "source"
                and sample.labels.get("operation") == "open_file"
            )
            self.assertEqual(source_opens.value, 2)

    def test_gc_failure_prevents_active_generation_sync(self) -> None:
        """Stop a logical-file attempt before the active generation when its GC generation fails."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "app.log").write_bytes(b"active...\n")
            quarantine = source / "logsync-gc-app.log"
            quarantine.write_bytes(b"gc...\n")
            real_open = os.open

            def fail_quarantine(path, flags, *args, **kwargs):
                if os.path.basename(path) == quarantine.name:
                    raise OSError("injected GC failure")
                return real_open(path, flags, *args, **kwargs)

            with patch("logsync.sync.os.open", side_effect=fail_quarantine):
                stats = sync_path(source, destination)

            self.assertEqual(stats.errors, 1)
            self.assertFalse((destination / "app.log").exists())

    def test_generations_append_in_order_across_passes(self) -> None:
        """Append initial and later GC and active generations in order across passes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            gc = source / "logsync-gc-app.log"
            active = source / "app.log"
            gc.write_bytes(b"gc-one...\n")
            active.write_bytes(b"active-one...\n")
            gc_writer = gc.open("ab", buffering=0)
            active_writer = active.open("ab", buffering=0)
            try:
                sync_path(source, destination)
                gc_writer.write(b"gc-two...\n")
                active_writer.write(b"active-two...\n")
                sync_path(source, destination)
            finally:
                gc_writer.close()
                active_writer.close()

            self.assertEqual(
                (destination / "app.log").read_bytes(),
                b"gc-one...\nactive-one...\ngc-two...\nactive-two...\n",
            )

    def test_quarantines_maps_logically_and_collects_complete_file(self) -> None:
        """Quarantine a drained source under its logical destination name and later collect it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")

            stats = sync_path(source, destination)

            self.assertEqual((destination / "app.log").read_bytes(), b"one...\n")
            self.assertFalse((source / "app.log").exists())
            self.assertTrue((source / "logsync-gc-app.log").exists())
            self.assertEqual((stats.quarantined, stats.collected), (1, 0))

            collected = sync_path(source, destination)
            self.assertFalse((source / "logsync-gc-app.log").exists())
            self.assertEqual((collected.quarantined, collected.collected), (0, 1))

    def test_open_writer_retains_active_file_until_writer_closes(self) -> None:
        """Retain the active path while a local writer remains open."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            active = source / "app.log"
            active.write_bytes(b"one...\n")
            writer = active.open("ab", buffering=0)
            try:
                first = sync_path(source, destination)
                quarantine = source / "logsync-gc-app.log"
                self.assertTrue(active.exists())
                self.assertFalse(quarantine.exists())
                self.assertEqual(first.collected, 0)
                writer.write(b"two...\n")
                second = sync_path(source, destination)
                self.assertEqual(second.collected, 0)
                self.assertEqual(
                    (destination / "app.log").read_bytes(),
                    b"one...\ntwo...\n",
                )
            finally:
                writer.close()

            third = sync_path(source, destination)
            self.assertEqual(
                (destination / "app.log").read_bytes(), b"one...\ntwo...\n"
            )
            self.assertFalse(active.exists())
            self.assertTrue(quarantine.exists())
            self.assertEqual((third.quarantined, third.collected), (1, 0))

            fourth = sync_path(source, destination)
            self.assertFalse(quarantine.exists())
            self.assertEqual(fourth.collected, 1)

    def test_multiple_producers_do_not_split_the_active_generation(self) -> None:
        """Retain one active generation while multiple local producers remain open."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            active = source / "app.log"
            first = os.open(
                active,
                os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC,
                0o600,
            )
            second = os.open(
                active,
                os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC,
                0o600,
            )
            try:
                os.write(first, b"one...\n")
                sync_path(source, destination)
                self.assertTrue(active.exists())
                self.assertFalse((source / "logsync-gc-app.log").exists())

                os.write(second, b"two...\n")
                sync_path(source, destination)
                self.assertTrue(active.exists())
                self.assertEqual(
                    (destination / "app.log").read_bytes(),
                    b"one...\ntwo...\n",
                )
            finally:
                os.close(first)
                os.close(second)

            stats = sync_path(source, destination)
            self.assertEqual((stats.quarantined, stats.collected), (1, 0))
            self.assertFalse(active.exists())
            self.assertTrue((source / "logsync-gc-app.log").exists())

            collected = sync_path(source, destination)
            self.assertEqual(collected.collected, 1)
            self.assertFalse((source / "logsync-gc-app.log").exists())

    def test_writer_in_another_process_blocks_collection_lease(self) -> None:
        """Honor real cross-process lease contention from an open writable descriptor."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            active = source / "app.log"
            active.write_bytes(b"one...\n")
            commands_read, commands_write = os.pipe()
            events_read, events_write = os.pipe()
            pid = os.fork()

            if pid == 0:
                os.close(commands_write)
                os.close(events_read)
                exit_status = 0
                try:
                    with active.open("ab", buffering=0) as writer:
                        os.write(events_write, b"R")
                        if os.read(commands_read, 1) != b"A":
                            raise RuntimeError("parent did not request append")
                        writer.write(b"two...\n")
                        os.write(events_write, b"W")
                        if os.read(commands_read, 1) != b"C":
                            raise RuntimeError("parent did not request close")
                except BaseException:
                    exit_status = 1
                finally:
                    os.close(commands_read)
                    os.close(events_write)
                os._exit(exit_status)

            os.close(commands_read)
            os.close(events_write)
            try:
                self.assertEqual(os.read(events_read, 1), b"R")
                first = sync_path(source, destination)
                quarantine = source / "logsync-gc-app.log"
                self.assertTrue(active.exists())
                self.assertFalse(quarantine.exists())
                self.assertEqual(first.collected, 0)

                os.write(commands_write, b"A")
                self.assertEqual(os.read(events_read, 1), b"W")
                second = sync_path(source, destination)
                self.assertEqual(second.collected, 0)
                self.assertEqual(
                    (destination / "app.log").read_bytes(),
                    b"one...\ntwo...\n",
                )
                os.write(commands_write, b"C")
            finally:
                os.close(commands_write)
                os.close(events_read)
                _, status = os.waitpid(pid, 0)

            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
            third = sync_path(source, destination)
            self.assertEqual(
                (destination / "app.log").read_bytes(), b"one...\ntwo...\n"
            )
            self.assertTrue(quarantine.exists())
            self.assertEqual((third.quarantined, third.collected), (1, 0))

            fourth = sync_path(source, destination)
            self.assertFalse(quarantine.exists())
            self.assertEqual(fourth.collected, 1)

    def test_gc_and_recreated_active_are_both_drained_in_one_pass(self) -> None:
        """Drain quarantined and recreated active generations in append order during one pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            active = source / "app.log"
            active.write_bytes(b"one...\n")
            with patch(
                "logsync.sync.verify_lease_unbroken",
                side_effect=(True, True, False),
            ):
                first = sync_path(source, destination)
            quarantine = source / "logsync-gc-app.log"
            self.assertEqual((first.quarantined, first.collected), (1, 0))
            quarantine.write_bytes(
                b"\0" * len(b"one...\n") + b"two...\n"
            )
            active.write_bytes(b"three...\n")
            writer = quarantine.open("ab", buffering=0)
            try:
                with patch(
                    "logsync.sync.get_write_lease", wraps=get_write_lease
                ) as lease:
                    sync_path(source, destination)
            finally:
                writer.close()

            self.assertEqual(
                (destination / "app.log").read_bytes(),
                b"one...\ntwo...\nthree...\n",
            )
            self.assertTrue(active.exists())
            self.assertTrue(quarantine.exists())
            self.assertEqual(lease.call_count, 1)

    def test_collection_failure_retains_durably_synced_data(self) -> None:
        """Retain durably synchronized destination data after a subsequent collection failure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            data = b"one...\n"
            (source / "app.log").write_bytes(data)

            with (
                patch("logsync.sync._collect_if_drained", side_effect=OSError("injected")),
                self.assertLogs("logsync.sync", level="ERROR"),
            ):
                result = sync_path(source, destination)

            self.assertEqual(result.errors, 1)
            self.assertEqual((destination / "app.log").read_bytes(), data)

    def test_collected_gc_does_not_collect_already_scanned_active(self) -> None:
        """Retain an already-scanned active generation after its GC generation is collected."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "logsync-gc-app.log").write_bytes(b"\0" * 8)
            active = source / "app.log"
            active.write_bytes(b"active...\n")

            with patch("logsync.sync.get_write_lease", wraps=get_write_lease) as lease:
                first = sync_path(source, destination)

            self.assertEqual(first.collected, 1)
            self.assertEqual(first.quarantined, 0)
            self.assertEqual(lease.call_count, 1)
            self.assertTrue(active.exists())
            self.assertFalse((source / "logsync-gc-app.log").exists())
            self.assertEqual(
                (destination / "app.log").read_bytes(), b"active...\n"
            )

            second = sync_path(source, destination)
            self.assertEqual((second.quarantined, second.collected), (1, 0))
            self.assertTrue((source / "logsync-gc-app.log").exists())

    def test_empty_and_fully_sparse_files_collect_without_destination(self) -> None:
        """Quarantine and collect empty and fully sparse files without opening a destination."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "empty.log").write_bytes(b"")
            (source / "sparse.log").write_bytes(b"\0" * 32)

            with patch("logsync.destination.get_user_anchor_path") as get_user_anchor_path:
                quarantined = sync_path(source, destination)
                collected = sync_path(source, destination)

            get_user_anchor_path.assert_not_called()
            self.assertEqual(quarantined.quarantined, 2)
            self.assertEqual(collected.collected, 2)
            self.assertEqual(list(source.iterdir()), [])

    def test_lease_errors_use_normal_file_error_handling(self) -> None:
        """Retry busy leases and apply normal file-error handling to unsupported leases."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")

            real_fcntl = fcntl.fcntl

            def busy(fd: int, command: int, argument: int = 0) -> int:
                if command == fcntl.F_SETLEASE and argument == fcntl.F_WRLCK:
                    raise OSError(errno.EAGAIN, "busy")
                return real_fcntl(fd, command, argument)

            with patch("logsync.leases.fcntl.fcntl", side_effect=busy):
                stats = sync_path(source, destination)
            self.assertEqual(stats.errors, 0)
            self.assertTrue((source / "app.log").exists())
            self.assertFalse((source / "logsync-gc-app.log").exists())

            def unsupported(fd: int, command: int, argument: int = 0) -> int:
                if command == fcntl.F_SETLEASE and argument == fcntl.F_WRLCK:
                    raise OSError(errno.EINVAL, "unsupported")
                return real_fcntl(fd, command, argument)

            with patch("logsync.leases.fcntl.fcntl", side_effect=unsupported):
                stats = sync_path(source, destination)
            self.assertEqual(stats.errors, 1)

    def test_lease_break_before_rename_retains_active_source(self) -> None:
        """Retain the active pathname when its lease breaks before quarantine rename."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            with patch("logsync.sync.verify_lease_unbroken", return_value=False):
                stats = sync_path(source, destination)

            self.assertEqual(stats.collected, 0)
            self.assertEqual(stats.quarantined, 0)
            self.assertTrue((source / "app.log").exists())
            self.assertFalse((source / "logsync-gc-app.log").exists())

    def test_renamed_active_is_retained_for_a_later_pass(self) -> None:
        """Retain renamed active data and late writes until a later pass collects the quarantine."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "app.log").write_bytes(b"one...\n")
            reset_metrics()
            renamed = sync_path(source, destination)
            quarantine = source / "logsync-gc-app.log"

            self.assertEqual((renamed.quarantined, renamed.collected), (1, 0))
            self.assertTrue(quarantine.exists())
            rename_operations = {
                sample.labels["operation"]
                for sample in metrics.samples()
                if sample.name == "logsync_operation_count_total"
                and sample.labels["filesystem"] == "source"
            }
            self.assertIn("rename", rename_operations)
            self.assertNotIn("unlink", rename_operations)
            with quarantine.open("ab") as writer:
                writer.write(b"late...\n")

            reset_metrics()
            collected = sync_path(source, destination)
            self.assertEqual(
                (destination / "app.log").read_bytes(),
                b"one...\nlate...\n",
            )
            self.assertEqual(collected.collected, 1)
            self.assertFalse(quarantine.exists())
            collection_operations = {
                sample.labels["operation"]
                for sample in metrics.samples()
                if sample.name == "logsync_operation_count_total"
                and sample.labels["filesystem"] == "source"
            }
            self.assertIn("unlink", collection_operations)
            self.assertNotIn("rename", collection_operations)
