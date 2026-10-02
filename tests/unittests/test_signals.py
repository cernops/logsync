"""Shutdown signal handling and critical sections."""

import signal
import unittest
from unittest.mock import call, patch

from logsync.signals import (
    ShutdownRequested,
    _shutdown_handler,
    critical_section,
    install_shutdown_handlers,
)


class SignalTests(unittest.TestCase):
    def test_handler_raises_immediately_outside_deferred_scope(self) -> None:
        """Turn an undeferred termination signal into the shutdown exception."""
        with self.assertRaises(ShutdownRequested) as raised:
            _shutdown_handler(signal.SIGTERM, None)

        self.assertIs(raised.exception.received, signal.SIGTERM)

    def test_pending_signal_is_raised_after_outermost_scope(self) -> None:
        """Delay a pending signal until nested critical sections have exited."""
        reached_inner_exit = False
        with self.assertRaises(ShutdownRequested) as raised:
            with critical_section(False):
                with critical_section(False):
                    _shutdown_handler(signal.SIGINT, None)
                reached_inner_exit = True

        self.assertTrue(reached_inner_exit)
        self.assertIs(raised.exception.received, signal.SIGINT)

    def test_sigterm_is_not_deferred_by_sigint_only_scope(self) -> None:
        """Deliver SIGTERM immediately from a critical section that defers only SIGINT."""
        with critical_section(False):
            with self.assertRaises(ShutdownRequested) as raised:
                _shutdown_handler(signal.SIGTERM, None)

        self.assertIs(raised.exception.received, signal.SIGTERM)

    def test_sigterm_is_deferred_by_strong_critical_scope(self) -> None:
        """Delay SIGTERM until a strong critical section exits."""
        reached_scope_exit = False
        with self.assertRaises(ShutdownRequested) as raised:
            with critical_section(True):
                _shutdown_handler(signal.SIGTERM, None)
                reached_scope_exit = True

        self.assertTrue(reached_scope_exit)
        self.assertIs(raised.exception.received, signal.SIGTERM)

    def test_installs_each_handler_only_once(self) -> None:
        """Install one handler for each supported termination signal exactly once."""
        with (
            patch("logsync.signals._HANDLER_STATE.installed", False),
            patch("logsync.signals.signal.signal") as set_handler,
        ):
            install_shutdown_handlers()
            install_shutdown_handlers()

        self.assertEqual(
            set_handler.call_args_list,
            [
                call(signal.SIGINT, _shutdown_handler),
                call(signal.SIGTERM, _shutdown_handler),
            ],
        )


if __name__ == "__main__":
    unittest.main()
