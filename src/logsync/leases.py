"""Linux file-lease operations used for lossless source collection."""

import errno
import fcntl
import signal
from dataclasses import dataclass


@dataclass(slots=True)
class _LeaseState:
    break_epoch: int = 0
    handler_installed: bool = False


_lease_state = _LeaseState()


def _lease_break(_signum: int, _frame: object) -> None:
    _lease_state.break_epoch += 1


def install_lease_break_handler() -> None:
    """Ensure lease-break SIGIO notifications are harmless and observable."""
    if not _lease_state.handler_installed:
        _ = signal.signal(signal.SIGIO, _lease_break)
        _lease_state.handler_installed = True


def get_write_lease(fd: int) -> int | None:
    """Acquire an exclusive lease and return its break epoch, or None if busy."""
    install_lease_break_handler()
    epoch = _lease_state.break_epoch
    try:
        _ = fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
    except OSError as exc:
        if exc.errno == errno.EAGAIN:
            return None
        raise
    return epoch


def release_write_lease(fd: int) -> None:
    """Release an exclusive lease."""
    _ = fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)


def verify_lease_unbroken(fd: int, epoch: int) -> bool:
    """Return whether the descriptor still owns an unbroken write lease."""
    return (
        _lease_state.break_epoch == epoch
        and fcntl.fcntl(fd, fcntl.F_GETLEASE) == fcntl.F_WRLCK
    )
