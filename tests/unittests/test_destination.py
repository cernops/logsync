"""Destination confinement and object creation."""

# pylint: disable=protected-access

# Test setup is intentionally repeated across thematic modules.
# pylint: disable=duplicate-code

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from logsync.common import close_fd
from logsync.destination import (
    UserDirSharding,
    _open_or_create_dir,
    _open_or_create_file,
    _validate_dest_path,
    get_user_anchor_path,
    open_dest_file,
)
from logsync.metrics import metrics
from logsync.sync import sync_user as confined_sync_path

from tests.unittests.support import encoded_source_file, reset_metrics, sync_path


class DestinationTests(unittest.TestCase):
    def test_directory_creation_defers_termination_signals(self) -> None:
        """Defer SIGINT and SIGTERM until a new directory has final ownership and permissions."""
        deferred = MagicMock()
        with (
            patch("logsync.destination.critical_section", return_value=deferred) as critical_section,
            patch("logsync.destination.os.open", side_effect=[FileNotFoundError(), 42]),
            patch("logsync.destination.os.mkdir"),
            patch("logsync.destination.os.fchown"),
            patch("logsync.destination.os.fchmod"),
        ):
            self.assertEqual(
                _open_or_create_dir(10, "logs", 1001, 100, True),
                42,
            )

        critical_section.assert_called_once_with(True)
        deferred.__enter__.assert_called_once_with()
        deferred.__exit__.assert_called_once()

    def test_file_creation_defers_termination_signals(self) -> None:
        """Defer SIGINT and SIGTERM until a new file has final ownership and permissions."""
        deferred = MagicMock()
        with (
            patch("logsync.destination.critical_section", return_value=deferred) as critical_section,
            patch("logsync.destination.os.open", side_effect=[FileNotFoundError(), 42]),
            patch("logsync.destination.os.fchown"),
            patch("logsync.destination.os.fchmod"),
        ):
            self.assertEqual(
                _open_or_create_file(10, "app.log", 1001, 100, True),
                42,
            )

        critical_section.assert_called_once_with(True)
        deferred.__enter__.assert_called_once_with()
        deferred.__exit__.assert_called_once()

    def test_rejects_invalid_user(self) -> None:
        """Reject usernames that are not safe single path components."""
        with self.assertRaises(ValueError):
            get_user_anchor_path(UserDirSharding.NONE, "/home/", "../alice")

    def test_destination_validation_returns_first_matching_anchor(self) -> None:
        """Return the canonical destination and first matching user anchor."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_anchor = root / "alice"
            requested = user_anchor / "logs" / "app.log"

            self.assertEqual(
                _validate_dest_path(
                    str(requested),
                    [str(root / "bob") + "/", str(root) + "/", str(user_anchor) + "/"],
                ),
                (str(requested), str(root) + "/"),
            )

    def test_skips_symlink_sources(self) -> None:
        """Warn about source symlinks without following them."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (root / "target").write_bytes(b"secret...\n")
            os.symlink(root / "target", source / "linked.log")

            with self.assertLogs("logsync.sync", level="WARNING") as captured:
                stats = sync_path(source, dest)

            self.assertEqual(stats.errors, 0)
            self.assertFalse((dest / "linked.log").exists())
            self.assertIn("skipping non-regular source entry", captured.output[0])

    def test_follows_dangling_file_symlink_within_user_dir(self) -> None:
        """Create a dangling file-symlink target within the destination user directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            destination = homes / "alice"
            source.mkdir()
            destination.mkdir(parents=True)
            (destination / "app.log").symlink_to("logs/main")
            encoded_source_file(source, destination / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
            )

            self.assertEqual(stats.errors, 0)
            self.assertEqual((destination / "logs" / "main").read_bytes(), b"one...\n")

    def test_rejects_destination_symlink_outside_user_dir(self) -> None:
        """Reject a destination symlink escaping the selected user directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            home = homes / "alice"
            outside = root / "outside"
            source.mkdir()
            home.mkdir(parents=True)
            outside.mkdir()
            (home / "app.log").symlink_to(outside / "main")
            encoded_source_file(source, home / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
            )

            self.assertEqual(stats.errors, 1)
            self.assertEqual(list(outside.iterdir()), [])

    def test_created_destination_has_requested_ownership(self) -> None:
        """Assign the requested UID and GID to a created destination file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            source.mkdir()
            home = homes / "alice"
            home.mkdir(parents=True)
            encoded_source_file(source, home / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
            )

            destination = homes / "alice" / "app.log"
            self.assertEqual(stats.errors, 0)
            self.assertEqual(
                (destination.stat().st_uid, destination.stat().st_gid),
                (os.getuid(), os.getgid()),
            )

    def test_first_letter_sharding_uses_existing_user_dir(self) -> None:
        """Write beneath the selected pre-existing first-letter user directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            home = homes / "a" / "alice"
            source.mkdir()
            home.mkdir(parents=True)
            encoded_source_file(source, home / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
                user_dir_sharding=UserDirSharding.FIRST_LETTER,
            )

            self.assertEqual(stats.errors, 0)
            self.assertEqual((home / "app.log").read_bytes(), b"one...\n")
            self.assertFalse((homes / "alice").exists())

    def test_prefixed_user_dir(self) -> None:
        """Write beneath a username-prefixed destination user directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            home = homes / "u-alice"
            source.mkdir()
            home.mkdir(parents=True)
            encoded_source_file(source, home / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/u-"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
            )

            self.assertEqual(stats.errors, 0)
            self.assertEqual((home / "app.log").read_bytes(), b"one...\n")

    def test_prefixed_first_letter_sharding(self) -> None:
        """Apply the destination prefix to the first-letter shard component."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            home = homes / "u-a" / "alice"
            source.mkdir()
            home.mkdir(parents=True)
            encoded_source_file(source, home / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/u-"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
                user_dir_sharding=UserDirSharding.FIRST_LETTER,
            )

            self.assertEqual(stats.errors, 0)
            self.assertEqual((home / "app.log").read_bytes(), b"one...\n")

    def test_missing_user_dir_is_not_created(self) -> None:
        """Require a pre-existing flat destination user directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            source.mkdir()
            homes.mkdir()
            encoded_source_file(source, homes / "a" / "alice" / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
            )

            self.assertEqual(stats.errors, 1)
            self.assertFalse((homes / "alice").exists())

    def test_missing_first_letter_shard_is_not_created(self) -> None:
        """Do not create a missing first-letter shard."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            source.mkdir()
            homes.mkdir()
            encoded_source_file(source, homes / "a" / "alice" / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
                user_dir_sharding=UserDirSharding.FIRST_LETTER,
            )

            self.assertEqual(stats.errors, 1)
            self.assertFalse((homes / "a").exists())

    def test_missing_sharded_user_dir_is_not_created(self) -> None:
        """Do not create a missing user directory beneath an existing shard."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            source.mkdir()
            (homes / "a").mkdir(parents=True)
            encoded_source_file(source, homes / "alice" / "app.log").write_bytes(b"one...\n")

            stats = confined_sync_path(
                str(source),
                [str(homes) + "/"],
                "alice",
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
                user_dir_sharding=UserDirSharding.FIRST_LETTER,
            )

            self.assertEqual(stats.errors, 1)
            self.assertFalse((homes / "a" / "alice").exists())

    def test_disabled_destination_ownership_does_not_chown(self) -> None:
        """Leave destination ownership assignment to the filesystem when disabled."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            homes = root / "homes"
            source.mkdir()
            (homes / "alice").mkdir(parents=True)
            encoded_source_file(source, homes / "alice" / "app.log").write_bytes(b"one...\n")

            with patch("logsync.destination.os.fchown") as fchown:
                stats = confined_sync_path(
                    str(source),
                    [str(homes) + "/"],
                    "alice",
                    owner_uid=os.getuid(),
                    owner_gid=os.getgid(),
                    set_destination_ownership=False,
                )

            self.assertEqual(stats.errors, 0)
            fchown.assert_not_called()

    def test_complete_destination_setup_preserves_chown_policy(self) -> None:
        """Resolve and open the destination while preserving the requested chown policy."""
        with (
            patch("logsync.destination._validate_dest_path", return_value=("/home/alice/app.log", "/home/alice/")) as validate,
            patch("logsync.destination.safe_open_dir", return_value=10),
            patch("logsync.destination._open_or_create_file", return_value=42) as create,
            patch("logsync.destination.close_fd"),
        ):
            fd = open_dest_file(
                "/home/alice/app.log",
                1001,
                100,
                True,
                user_dir_sharding=UserDirSharding.NONE,
                path_prefixes=["/home/"],
                user="alice",
            )

        self.assertEqual(fd, 42)
        validate.assert_called_once()
        self.assertIs(create.call_args.args[4], True)

    def test_concurrent_destination_dir_creation_is_expected(self) -> None:
        """Quietly reopen and successfully measure a concurrently created directory."""
        reset_metrics()
        with (
            patch("logsync.destination.os.open", side_effect=[FileNotFoundError(), 42]) as open_path,
            patch("logsync.destination.os.mkdir", side_effect=FileExistsError()),
            self.assertNoLogs("logsync.destination", level="WARNING"),
        ):
            self.assertEqual(
                _open_or_create_dir(10, "logs", 1001, 100, True),
                42,
            )

        self.assertEqual(open_path.call_count, 2)
        self.assertTrue(
            all(sample.labels["outcome"] == "success" for sample in metrics.samples() if "outcome" in sample.labels)
        )

    def test_concurrent_destination_file_creation_is_expected(self) -> None:
        """Quietly reopen and successfully measure a concurrently created file."""
        reset_metrics()
        with (
            patch("logsync.destination.os.open", side_effect=[FileNotFoundError(), FileExistsError(), 42]) as open_path,
            self.assertNoLogs("logsync.destination", level="WARNING"),
        ):
            self.assertEqual(
                _open_or_create_file(10, "app.log", 1001, 100, True),
                42,
            )

        self.assertEqual(open_path.call_count, 3)
        self.assertTrue(open_path.call_args.args[1] & os.O_APPEND)
        self.assertTrue(
            all(sample.labels["outcome"] == "success" for sample in metrics.samples() if "outcome" in sample.labels)
        )

    def test_close_error_identifies_the_destination_path(self) -> None:
        """Include the destination path in advisory close-error logs."""
        with (
            patch("logsync.common.os.close", side_effect=OSError("injected")),
            self.assertLogs("logsync.common", level="ERROR") as captured,
        ):
            close_fd(42, "destination", "/logs/alice/app.log")

        self.assertIn(
            "failed to close destination path /logs/alice/app.log",
            captured.output[0],
        )
