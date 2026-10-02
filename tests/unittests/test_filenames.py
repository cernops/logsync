"""Source filename limits."""

# Test setup is intentionally repeated across thematic modules.
# pylint: disable=duplicate-code

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logsync.cli import main
from logsync.filenames import source_filename_limit
from logsync.sync import SyncStats

from tests.unittests.support import sync_path


class FilenameTests(unittest.TestCase):
    def test_rejects_source_filenames_too_long_for_quarantine_file(self) -> None:
        """Reserve enough filename space for the quarantine prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            name_max = os.pathconf(source, "PC_NAME_MAX")
            invalid_length = name_max - len(os.fsencode("logsync-gc-")) + 1
            (source / ("a" * invalid_length)).write_bytes(b"one...\n")

            self.assertEqual(sync_path(source, dest).errors, 1)
            self.assertEqual(list(dest.iterdir()), [])

    def test_configured_source_filename_limit(self) -> None:
        """Validate configured filename limits against the source filesystem."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            filesystem_limit = source_filename_limit(str(source))

            self.assertEqual(source_filename_limit(str(source), 32), 32)
            with self.assertRaises(ValueError):
                source_filename_limit(str(source), 0)
            with self.assertRaises(ValueError):
                source_filename_limit(str(source), filesystem_limit)

    def test_cli_calculates_default_filename_limit_only_in_sync_user(self) -> None:
        """Calculate and validate the default filesystem limit only in the synchronization path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()

            with (
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch("logsync.runtime.sync_user", return_value=SyncStats()) as run_sync,
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

            self.assertEqual(result, 0)
            self.assertIsNone(run_sync.call_args.kwargs["max_filename_bytes"])

    def test_rejects_source_filename_over_configured_limit(self) -> None:
        """Reject names exceeding a configured lower filename limit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            dest = root / "dest"
            source.mkdir()
            (source / "long.log").write_bytes(b"one...\n")

            stats = sync_path(source, dest, max_filename_bytes=4)

            self.assertEqual(stats.errors, 1)
            self.assertEqual(list(dest.iterdir()), [])
