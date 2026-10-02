"""Runtime user orchestration and system discovery."""

import io
import logging
import os
import pwd
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from logsync.config import Config
from logsync.identity import IdentityFatalError
from logsync.runtime import _log_startup, configure_logging, discover_users, resolve_supplementary_groups, resolve_user, run
from logsync.sync import SyncStats


class RuntimeTests(unittest.TestCase):
    def test_startup_logs_an_example_for_each_destination_prefix(self) -> None:
        """Log the complete destination list and one path example for every prefix."""
        config = Config(
            source_prefix="/source/",
            dest_prefixes=["/primary/", "/archive/"],
            user="alice",
        )

        with patch("logsync.runtime.logging.info") as info:
            _log_startup(config)

        messages = [call.args[0] for call in info.call_args_list]
        self.assertIn("dest_prefixes=['/primary/', '/archive/']", messages[0])
        examples = [message for message in messages if message.startswith("path translation example ")]
        self.assertEqual(len(examples), 2)
        self.assertIn("destination='/primary/alice/app.log'", examples[0])
        self.assertIn("destination='/archive/alice/app.log'", examples[1])

    def test_keyring_cache_lasts_for_one_run_invocation(self) -> None:
        """Reuse one cache across recurring passes and create a new cache for another run."""
        config = Config(
            source_prefix="/source/",
            dest_prefixes=["/destination/"],
            user="alice",
            interval=0,
            htcondor_keyring=True,
        )
        seen_caches: list[object] = []

        def process_users(_config, _users, cache) -> tuple[bool, bool]:
            seen_caches.append(cache)
            if len(seen_caches) == 2:
                object.__setattr__(config, "interval", -1)
            return False, False

        with (
            patch("logsync.runtime.HTCondorKeyringCache") as cache_type,
            patch("logsync.runtime._process_users", side_effect=process_users),
        ):
            cache_type.side_effect = [MagicMock(), MagicMock()]
            self.assertEqual(run(config), 0)
            first_cache = seen_caches[0]
            object.__setattr__(config, "interval", -1)
            self.assertEqual(run(config), 0)

        self.assertIs(seen_caches[0], first_cache)
        self.assertIs(seen_caches[1], first_cache)
        self.assertIsNot(seen_caches[2], first_cache)
        self.assertEqual(cache_type.call_count, 2)

    def test_keyring_is_neutralized_before_user_discovery(self) -> None:
        """Join the process-unique neutral keyring before performing initial user discovery."""
        events: list[str] = []
        with (
            patch("logsync.runtime.HTCondorKeyringCache") as cache_type,
            patch("logsync.runtime.discover_users", side_effect=lambda _prefix: events.append("discover") or []),
            patch("logsync.runtime._process_users", return_value=(False, False)),
        ):
            cache_type.return_value.use_neutral.side_effect = lambda: events.append("neutral")
            result = run(Config(
                source_prefix="/source/",
                dest_prefixes=["/destination/"],
                interval=-1,
                htcondor_keyring=True,
            ))

        self.assertEqual(result, 0)
        self.assertEqual(events, ["neutral", "discover"])

    def test_logging_starts_with_iso_like_millisecond_timestamp(self) -> None:
        """Use the configured ISO-like millisecond timestamp in runtime logs."""
        stream = io.StringIO()
        with redirect_stderr(stream):
            configure_logging(verbose=False)
            logging.info("scan complete")

        self.assertRegex(
            stream.getvalue().strip(),
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO scan complete$",
        )

    def test_summary_log_level_reflects_source_tree_operations(self) -> None:
        """Log idle user and pass summaries at debug, promoting both when source-tree work occurs."""
        config = Config(
            source_prefix="/source/",
            dest_prefixes=["/destination/"],
            user="alice",
        )
        for stats, expected_logger in (
            (SyncStats(files_active=1, directories_scanned=1), "debug"),
            (SyncStats(files_gc=1, directories_scanned=1, collected=1), "info"),
        ):
            with (
                self.subTest(expected_logger=expected_logger),
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch("logsync.runtime.sync_user", return_value=stats),
                patch("logsync.runtime.logging.info") as info,
                patch("logsync.runtime.logging.debug") as debug,
            ):
                self.assertEqual(run(config), 0)

            if expected_logger == "debug":
                expected = debug
                unexpected = info
            else:
                expected = info
                unexpected = debug
            self.assertTrue(any(call.args[0].startswith("user=alice ") for call in expected.call_args_list))
            self.assertTrue(any(call.args[0].startswith("pass complete ") for call in expected.call_args_list))
            self.assertFalse(any(call.args[0].startswith("user=alice ") for call in unexpected.call_args_list))
            self.assertFalse(any(call.args[0].startswith("pass complete ") for call in unexpected.call_args_list))

    def test_default_identity_does_not_resolve_supplementary_groups(self) -> None:
        """Synchronize normally without consulting supplementary-group NSS data."""
        with (
            patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
            patch("logsync.runtime.resolve_supplementary_groups", side_effect=OSError("injected")) as resolve_groups,
            patch("logsync.runtime.sync_user", return_value=SyncStats()) as run_sync,
        ):
            result = run(Config(
                source_prefix="/source/",
                dest_prefixes=["/destination/"],
                user="alice",
            ))

        self.assertEqual(result, 0)
        resolve_groups.assert_not_called()
        run_sync.assert_called_once()

    def test_multi_user_failure_does_not_stop_later_user(self) -> None:
        """Continue to a later user after credential setup fails."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            for user in ("alice", "bob"):
                (source_root / user).mkdir(parents=True)
            dest_root.mkdir()

            def resolve(user: str) -> tuple[str, int, int]:
                return user, 1001 if user == "alice" else 1002, 100

            def run_with_credentials(*_args, **kwargs) -> SyncStats:
                kwargs["credential_setup"]()
                return SyncStats()

            with (
                patch("logsync.runtime.resolve_user", side_effect=resolve),
                patch(
                    "logsync.runtime.HTCondorKeyringCache"
                ) as cache_type,
                patch(
                    "logsync.runtime.sync_user", side_effect=run_with_credentials
                ) as run_sync,
            ):
                cache_type.return_value.use.side_effect = [OSError("missing token"), None]
                result = run(
                    Config(
                        source_prefix=str(source_root) + "/",
                        dest_prefixes=[str(dest_root) + "/"],
                        htcondor_keyring=True,
                    )
                )

            self.assertEqual(result, 1)
            self.assertEqual(cache_type.return_value.use.call_count, 2)
            self.assertEqual(run_sync.call_count, 2)
            self.assertEqual(run_sync.call_args_list[1].args[2], "bob")

    def test_multi_user_pass_neutralizes_credentials_between_users(self) -> None:
        """Remove one user's selected credentials before source-only work for the next user."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            for user in ("alice", "bob"):
                (source_root / user).mkdir(parents=True)
            dest_root.mkdir()
            events: list[str] = []

            def resolve(user: str) -> tuple[str, int, int]:
                return user, 1001 if user == "alice" else 1002, 100

            def sync(*args, **kwargs) -> SyncStats:
                user = args[2]
                events.append(f"sync:{user}")
                if user == "alice":
                    kwargs["credential_setup"]()
                return SyncStats()

            with (
                patch("logsync.runtime.resolve_user", side_effect=resolve),
                patch("logsync.runtime.HTCondorKeyringCache") as cache_type,
                patch("logsync.runtime.sync_user", side_effect=sync),
            ):
                cache_type.return_value.use_neutral.side_effect = lambda: events.append("neutral")
                cache_type.return_value.use.side_effect = lambda uid: events.append(f"credentials:{uid}")
                result = run(Config(
                    source_prefix=str(source_root) + "/",
                    dest_prefixes=[str(dest_root) + "/"],
                    htcondor_keyring=True,
                ))

            self.assertEqual(result, 0)
            self.assertEqual(events, [
                "neutral",
                "neutral",
                "sync:alice",
                "credentials:1001",
                "neutral",
                "neutral",
                "sync:bob",
                "neutral",
            ])

    def test_deferred_user_continues_to_later_users_and_skips_sleep(self) -> None:
        """Treat a deferred user as successful, process later users, and immediately start the next pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            for user in ("alice", "bob"):
                (source_root / user).mkdir(parents=True)
            dest_root.mkdir()

            def resolve(user: str) -> tuple[str, int, int]:
                return user, 1001 if user == "alice" else 1002, 100

            with (
                patch("logsync.runtime.resolve_user", side_effect=resolve),
                patch(
                    "logsync.runtime.sync_user",
                    side_effect=(SyncStats(deferred=True), SyncStats(), SystemExit()),
                ) as run_sync,
                patch("logsync.runtime.time.monotonic", side_effect=(0.0, 1.0)),
                patch("logsync.runtime.time.sleep") as sleep,
            ):
                with self.assertRaises(SystemExit):
                    run(
                        Config(
                            source_prefix=str(source_root) + "/",
                            dest_prefixes=[str(dest_root) + "/"],
                            interval=5.0,
                        )
                    )

            self.assertEqual([call.args[2] for call in run_sync.call_args_list], ["alice", "bob", "alice"])
            sleep.assert_not_called()

    def test_deferred_one_shot_run_succeeds(self) -> None:
        """Return success when a one-shot user's remaining files are deferred by the time limit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()

            with (
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch("logsync.runtime.sync_user", return_value=SyncStats(deferred=True)),
            ):
                result = run(
                    Config(
                        source_prefix=str(source_root) + "/",
                        dest_prefixes=[str(dest_root) + "/"],
                        user="alice",
                    )
                )

            self.assertEqual(result, 0)

    def test_effective_identity_wraps_complete_user_sync(self) -> None:
        """Run the complete user synchronization under the target EUID and EGID."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()
            events: list[str] = []
            identity = MagicMock()
            identity.__enter__.side_effect = lambda: events.append("identity:enter")
            identity.__exit__.side_effect = lambda *_args: events.append("identity:exit")

            def sync(*_args, **_kwargs) -> SyncStats:
                events.append("sync")
                return SyncStats()

            with (
                patch(
                    "logsync.runtime.resolve_user",
                    return_value=("alice", 1001, 100),
                ),
                patch("logsync.runtime.resolve_supplementary_groups", return_value=(200, 201)),
                patch("logsync.runtime.as_user", return_value=identity) as select_identity,
                patch("logsync.runtime.sync_user", side_effect=sync) as run_sync,
            ):
                result = run(
                    Config(
                        source_prefix=str(source_root) + "/",
                        dest_prefixes=[str(dest_root) + "/"],
                        user="alice",
                        use_effective_identity=True,
                    )
                )

            self.assertEqual(result, 0)
            select_identity.assert_called_once_with(1001, 100, (200, 201))
            self.assertEqual(events, ["identity:enter", "sync", "identity:exit"])
            self.assertIs(
                run_sync.call_args.kwargs["set_destination_ownership"], True
            )
            self.assertEqual(run_sync.call_args.kwargs["max_user_seconds"], 1.0)

    def test_identity_restoration_failure_stops_user_processing(self) -> None:
        """Propagate identity restoration failure instead of attempting a later user."""
        identity = MagicMock()
        identity.__exit__.side_effect = IdentityFatalError("injected")

        with (
            patch("logsync.runtime.discover_users", return_value=["alice", "bob"]),
            patch("logsync.runtime.resolve_user", side_effect=[("alice", 1001, 100), ("bob", 1002, 100)]),
            patch("logsync.runtime.resolve_supplementary_groups", return_value=()),
            patch("logsync.runtime.as_user", return_value=identity),
            patch("logsync.runtime.sync_user", return_value=SyncStats()) as run_sync,
            self.assertRaisesRegex(IdentityFatalError, "injected"),
        ):
            run(Config(
                source_prefix="/source/",
                dest_prefixes=["/destination/"],
                interval=-1,
                use_effective_identity=True,
            ))

        self.assertEqual(run_sync.call_count, 1)

    def test_keyring_cleanup_does_not_mask_identity_restoration_failure(self) -> None:
        """Propagate a fatal identity restoration error even when neutral keyring cleanup also fails."""
        identity = MagicMock()
        identity.__exit__.side_effect = IdentityFatalError("identity restoration failed")

        with (
            patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
            patch("logsync.runtime.resolve_supplementary_groups", return_value=()),
            patch("logsync.runtime.HTCondorKeyringCache") as cache_type,
            patch("logsync.runtime.as_user", return_value=identity),
            patch("logsync.runtime.sync_user", return_value=SyncStats()),
            self.assertRaisesRegex(IdentityFatalError, "identity restoration failed"),
        ):
            cache_type.return_value.use_neutral.side_effect = [None, None, OSError("neutral cleanup failed")]
            run(Config(
                source_prefix="/source/",
                dest_prefixes=["/destination/"],
                user="alice",
                interval=-1,
                htcondor_keyring=True,
                use_effective_identity=True,
            ))

    def test_lazy_keyring_setup_temporarily_restores_daemon_identity(self) -> None:
        """Switch from the user back to the daemon only around lazy keyring activation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            dest_root = root / "home"
            (source_root / "alice").mkdir(parents=True)
            dest_root.mkdir()
            events: list[str] = []
            user_identity = MagicMock()
            daemon_identity = MagicMock()
            user_identity.__enter__.side_effect = lambda: events.append("user:enter")
            user_identity.__exit__.side_effect = lambda *_args: events.append("user:exit")
            daemon_identity.__enter__.side_effect = lambda: events.append("daemon:enter")
            daemon_identity.__exit__.side_effect = lambda *_args: events.append("daemon:exit")

            def sync(*_args, **kwargs) -> SyncStats:
                events.append("sync:start")
                kwargs["credential_setup"]()
                events.append("sync:end")
                return SyncStats()

            with (
                patch("logsync.runtime.os.geteuid", return_value=0),
                patch("logsync.runtime.os.getegid", return_value=0),
                patch("logsync.runtime.os.getgroups", return_value=[10, 20]),
                patch("logsync.runtime.resolve_user", return_value=("alice", 1001, 100)),
                patch("logsync.runtime.resolve_supplementary_groups", return_value=(200,)),
                patch("logsync.runtime.HTCondorKeyringCache") as cache_type,
                patch("logsync.runtime.as_user", return_value=user_identity) as select_user_identity,
                patch("logsync.runtime.as_daemon", return_value=daemon_identity) as select_daemon_identity,
                patch("logsync.runtime.sync_user", side_effect=sync),
            ):
                cache_type.return_value.use.side_effect = lambda uid: events.append(f"credentials:{uid}")
                result = run(
                    Config(
                        source_prefix=str(source_root) + "/",
                        dest_prefixes=[str(dest_root) + "/"],
                        user="alice",
                        htcondor_keyring=True,
                        use_effective_identity=True,
                    )
                )

            self.assertEqual(result, 0)
            select_user_identity.assert_called_once_with(1001, 100, (200,))
            select_daemon_identity.assert_called_once_with(0, 0, (10, 20))
            self.assertEqual(events, [
                "user:enter",
                "sync:start",
                "daemon:enter",
                "credentials:1001",
                "daemon:exit",
                "sync:end",
                "user:exit",
            ])

    def test_user_discovery_logs_bad_entry_and_continues(self) -> None:
        """Report unreadable and non-directory entries while discovering later users."""
        bad_entry = MagicMock(name="bad_entry")
        bad_entry.name = "alice"
        bad_entry.path = "/logsync/alice"
        bad_entry.is_dir.side_effect = OSError("injected inspection failure")
        good_entry = MagicMock(name="good_entry")
        good_entry.name = "bob"
        good_entry.is_dir.return_value = True
        stray_entry = MagicMock(name="stray_entry")
        stray_entry.name = "README"
        stray_entry.path = "/logsync/README"
        stray_entry.is_dir.return_value = False
        entries = MagicMock()
        entries.__enter__.return_value = iter((bad_entry, stray_entry, good_entry))

        with (
            patch("logsync.runtime.os.scandir", return_value=entries),
            self.assertLogs(level="ERROR") as captured,
        ):
            users = discover_users("/logsync/")

        self.assertEqual(users, ["bob"])
        self.assertTrue(
            any(
                "failed to inspect source user entry /logsync/alice" in line
                for line in captured.output
            )
        )
        self.assertTrue(
            any(
                "failed to inspect source user entry /logsync/README" in line
                for line in captured.output
            )
        )

    def test_discovers_users_below_a_prefixed_root(self) -> None:
        """Strip the configured prefix and ignore unrelated entries during discovery."""
        alice = MagicMock(name="alice")
        alice.name = "u-alice"
        alice.is_dir.return_value = True
        unrelated = MagicMock(name="unrelated")
        unrelated.name = "other-bob"
        entries = MagicMock()
        entries.__enter__.return_value = iter((unrelated, alice))

        with patch("logsync.runtime.os.scandir", return_value=entries):
            users = discover_users("/logsync/u-")

        self.assertEqual(users, ["alice"])
        unrelated.is_dir.assert_not_called()

    def test_resolves_exact_system_user(self) -> None:
        """Resolve exact account names and reject unknown users."""
        account = pwd.getpwuid(os.getuid())

        self.assertEqual(
            resolve_user(account.pw_name),
            (account.pw_name, account.pw_uid, account.pw_gid),
        )
        self.assertEqual(
            resolve_supplementary_groups(account.pw_name, account.pw_gid),
            tuple(sorted(set(os.getgrouplist(account.pw_name, account.pw_gid)) - {account.pw_gid})),
        )
        with self.assertRaises(ValueError):
            resolve_user("logsync-user-that-does-not-exist")

    def test_rejects_user_lookup_that_returns_a_different_name(self) -> None:
        """Reject an NSS lookup that canonicalizes the requested username."""
        account = MagicMock()
        account.pw_name = "canonical-alice"

        with (
            patch("logsync.runtime.pwd.getpwnam", return_value=account),
            self.assertRaisesRegex(
                ValueError,
                "user lookup did not return an exact match: alice",
            ),
        ):
            resolve_user("alice")
