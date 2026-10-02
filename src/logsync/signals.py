"""Deferred process-termination signal handling."""

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class ShutdownRequested(BaseException):
    """Request an orderly exit after any active critical section."""

    def __init__(self, received: signal.Signals) -> None:
        super().__init__(received.name)
        self.received: signal.Signals = received


@dataclass(slots=True)
class _HandlerState:
    installed: bool = False


_DEPTH = {
    signal.SIGINT: 0,
    signal.SIGTERM: 0,
}
_PENDING: set[signal.Signals] = set()
_HANDLER_STATE = _HandlerState()


def install_shutdown_handlers() -> None:
    """Install the process termination handlers once."""
    if _HANDLER_STATE.installed:
        return
    _ = signal.signal(signal.SIGINT, _shutdown_handler)
    _ = signal.signal(signal.SIGTERM, _shutdown_handler)
    _HANDLER_STATE.installed = True


@contextmanager
def critical_section(defer_sigterm: bool) -> Iterator[None]:
    """Defer termination signals until the outermost critical scope exits."""
    selected = [signal.SIGINT]
    if defer_sigterm:
        selected.append(signal.SIGTERM)
    for received in selected:
        _DEPTH[received] += 1
    try:
        yield
    finally:
        for received in selected:
            _DEPTH[received] -= 1

        deliverable = [
            received
            for received in selected
            if _DEPTH[received] == 0 and received in _PENDING
        ]
        _PENDING.difference_update(deliverable)
        if deliverable:
            raise ShutdownRequested(deliverable[0])


def _shutdown_handler(signum: int, _frame: object) -> None:
    received = signal.Signals(signum)
    if _DEPTH[received] == 0:
        raise ShutdownRequested(received)
    _PENDING.add(received)
