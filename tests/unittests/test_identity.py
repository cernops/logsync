"""Temporary effective process identity."""

import unittest
from unittest.mock import Mock, call, patch

from logsync.identity import IdentityFatalError, as_user


class IdentityTests(unittest.TestCase):
    def test_effective_identity_is_selected_and_restored_in_privilege_safe_order(self) -> None:
        """Clear groups before dropping privilege and restore privilege before daemon groups."""
        calls = Mock()
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[20, 30]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid") as set_euid,
            patch("logsync.identity.os.setegid") as set_egid,
            patch("logsync.identity.os.setgroups") as set_groups,
        ):
            calls.attach_mock(set_euid, "seteuid")
            calls.attach_mock(set_egid, "setegid")
            calls.attach_mock(set_groups, "setgroups")
            with as_user(1001, 100):
                pass

        self.assertEqual(calls.mock_calls, [
            call.setgroups(()),
            call.setegid(100),
            call.seteuid(1001),
            call.seteuid(0),
            call.setegid(10),
            call.setgroups((20, 30)),
        ])

    def test_effective_identity_is_restored_after_operation_failure(self) -> None:
        """Restore the previous effective identity when the protected operation fails."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid") as set_euid,
            patch("logsync.identity.os.setegid") as set_egid,
            patch("logsync.identity.os.setgroups"),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            with as_user(1001, 100):
                raise OSError("injected")

        self.assertEqual(set_euid.call_args_list, [call(1001), call(0)])
        self.assertEqual(set_egid.call_args_list, [call(100), call(10)])

    def test_effective_uid_switch_failure_restores_gid(self) -> None:
        """Restore EGID when selecting the target EUID fails after EGID changed."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid", side_effect=PermissionError("injected")),
            patch("logsync.identity.os.setegid") as set_egid,
            patch("logsync.identity.os.setgroups"),
            self.assertRaisesRegex(PermissionError, "injected"),
        ):
            with as_user(1001, 100):
                pass

        self.assertEqual(set_egid.call_args_list, [call(100), call(10)])

    def test_effective_gid_switch_failure_does_not_change_uid(self) -> None:
        """Leave EUID unchanged when selecting the target EGID fails without changing EGID."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid") as set_euid,
            patch("logsync.identity.os.setegid", side_effect=PermissionError("injected")) as set_egid,
            patch("logsync.identity.os.setgroups") as set_groups,
            self.assertRaisesRegex(PermissionError, "injected"),
        ):
            with as_user(1001, 100):
                pass

        set_euid.assert_not_called()
        self.assertEqual(set_egid.call_args_list, [call(100)])
        self.assertEqual(set_groups.call_args_list, [call(()), call(())])

    def test_failed_identity_selection_rollback_is_fatal(self) -> None:
        """Report a fatal identity error when target selection fails and the previous groups cannot be restored."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[20]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid"),
            patch("logsync.identity.os.setegid", side_effect=PermissionError("target GID rejected")),
            patch("logsync.identity.os.setgroups", side_effect=[None, PermissionError("group rollback failed")]),
            self.assertRaises(IdentityFatalError) as raised,
        ):
            with as_user(1001, 100):
                pass

        self.assertIsInstance(raised.exception.__cause__, PermissionError)
        self.assertEqual(str(raised.exception.__cause__), "group rollback failed")

    def test_effective_uid_restore_failure_is_fatal(self) -> None:
        """Report a fatal identity error without attempting privileged restoration after EUID restoration fails."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid", side_effect=[None, PermissionError("injected")]),
            patch("logsync.identity.os.setegid") as set_egid,
            patch("logsync.identity.os.setgroups"),
            self.assertRaises(IdentityFatalError) as raised,
        ):
            with as_user(1001, 100):
                pass

        self.assertEqual(set_egid.call_args_list, [call(100)])
        self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_supplementary_group_switch_failure_leaves_identity_unchanged(self) -> None:
        """Leave EUID and EGID unchanged when clearing supplementary groups fails."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[20]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid") as set_euid,
            patch("logsync.identity.os.setegid") as set_egid,
            patch("logsync.identity.os.setgroups", side_effect=PermissionError("injected")),
            self.assertRaisesRegex(PermissionError, "injected"),
        ):
            with as_user(1001, 100):
                pass

        set_euid.assert_not_called()
        set_egid.assert_not_called()

    def test_supplementary_group_restore_failure_is_fatal(self) -> None:
        """Report a fatal identity error when restoring daemon supplementary groups fails."""
        with (
            patch("logsync.identity.os.geteuid", return_value=0),
            patch("logsync.identity.os.getegid", return_value=10),
            patch("logsync.identity.os.getgroups", return_value=[20]),
            patch("logsync.identity.os.getresuid", return_value=(0, 0, 0)),
            patch("logsync.identity.os.seteuid") as set_euid,
            patch("logsync.identity.os.setegid") as set_egid,
            patch("logsync.identity.os.setgroups", side_effect=[None, PermissionError("injected")]) as set_groups,
            self.assertRaises(IdentityFatalError) as raised,
        ):
            with as_user(1001, 100):
                pass

        self.assertEqual(set_euid.call_args_list, [call(1001), call(0), call(0)])
        self.assertEqual(set_egid.call_args_list, [call(100), call(10), call(10)])
        self.assertEqual(set_groups.call_args_list, [call(()), call((20,))])
        self.assertIsInstance(raised.exception.__cause__, PermissionError)
