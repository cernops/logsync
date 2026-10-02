"""Command-line parsing and configuration construction."""

import argparse
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logsync.cli import main
from logsync.config import Config
from logsync.destination import UserDirSharding
from logsync.cli import absolute_path_prefix
from logsync.signals import ShutdownRequested


class CliTests(unittest.TestCase):
    def test_logs_clean_signal_exit(self) -> None:
        """Log a handled termination request and return a successful status."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("logsync.cli.install_shutdown_handlers") as install_handlers,
                patch(
                    "logsync.cli.run",
                    side_effect=ShutdownRequested(signal.SIGTERM),
                ),
                patch("logsync.cli.logging.info") as log_info,
            ):
                result = main(
                    [
                        "--source-prefix",
                        f"{root}/source/",
                        "--dest-prefix",
                        f"{root}/home/",
                    ]
                )

        self.assertEqual(result, 0)
        install_handlers.assert_called_once_with()
        log_info.assert_called_once_with("received SIGTERM, exiting")

    def test_builds_complete_config(self) -> None:
        """Map every command-line setting into the application configuration."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("logsync.cli.run", return_value=7) as run:
                result = main(
                    [
                        "--source-prefix",
                        f"{root}/source/",
                        "--dest-prefix",
                        f"{root}/home/",
                        "--dest-prefix",
                        f"{root}/archive/",
                        "--user-dir-sharding",
                        "first-letter",
                        "--user",
                        "alice",
                        "--interval",
                        "2.5",
                        "--max-user-seconds",
                        "4.5",
                        "--htcondor-keyring",
                        "--no-chown",
                        "--use-effective-identity",
                        "--keep-user-top-dirs",
                        "--max-filename-bytes",
                        "123",
                        "--chunk-size-bytes",
                        "456",
                        "--metrics-file",
                        str(root / "metrics.prom"),
                        "--metrics-interval",
                        "3.5",
                        "--verbose",
                    ]
                )

            self.assertEqual(result, 7)
            run.assert_called_once_with(
                Config(
                    source_prefix=str(root / "source") + "/",
                    dest_prefixes=[
                        str(root / "home") + "/",
                        str(root / "archive") + "/",
                    ],
                    user_dir_sharding=UserDirSharding.FIRST_LETTER,
                    user="alice",
                    interval=2.5,
                    max_user_seconds=4.5,
                    htcondor_keyring=True,
                    no_chown=True,
                    use_effective_identity=True,
                    keep_user_top_dirs=True,
                    max_filename_bytes=123,
                    chunk_size_bytes=456,
                    metrics_file=str(root / "metrics.prom"),
                    metrics_interval=3.5,
                    verbose=True,
                )
            )

    def test_builds_config_defaults(self) -> None:
        """Map omitted optional arguments to configuration defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("logsync.cli.run", return_value=0) as run:
                result = main(
                    [
                        "--source-prefix",
                        f"{root}/source/",
                        "--dest-prefix",
                        f"{root}/home/",
                    ]
                )

            self.assertEqual(result, 0)
            run.assert_called_once_with(
                Config(
                    source_prefix=str(root / "source") + "/",
                    dest_prefixes=[str(root / "home") + "/"],
                )
            )

    def test_rejects_invalid_user_before_running(self) -> None:
        """Report an unsafe command-line username without entering the runtime."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("logsync.cli.run") as run,
                self.assertRaises(SystemExit),
            ):
                main(
                    [
                        "--source-prefix",
                        f"{root}/source/",
                        "--dest-prefix",
                        f"{root}/home/",
                        "--user",
                        "../alice",
                    ]
                )

            run.assert_not_called()

    def test_rejects_invalid_max_user_seconds(self) -> None:
        """Reject non-positive and non-finite per-user time limits before running."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for invalid in ("0", "-1", "inf", "nan"):
                with (
                    self.subTest(invalid=invalid),
                    patch("logsync.cli.run") as run,
                    self.assertRaises(SystemExit),
                ):
                    main(
                        [
                            "--source-prefix",
                            f"{root}/source/",
                            "--dest-prefix",
                            f"{root}/home/",
                            "--max-user-seconds",
                            invalid,
                        ]
                    )
                run.assert_not_called()

    def test_absolute_path_prefix_validation(self) -> None:
        """Accept safe absolute prefixes and reject unsafe directory and username prefix syntax."""
        self.assertEqual(absolute_path_prefix("/home/"), "/home/")
        self.assertEqual(
            absolute_path_prefix("/home/u-"), "/home/u-"
        )
        self.assertEqual(
            absolute_path_prefix("/home/u_"), "/home/u_"
        )
        for invalid in ("home/", "/home", ""):
            with self.subTest(invalid=invalid), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                absolute_path_prefix(invalid)
