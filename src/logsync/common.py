"""Shared logsync constants and validation helpers."""

import logging
import os

from .errors import format_error
from .metrics import metrics


log = logging.getLogger(__name__)

RECORD_TERMINATOR = b"...\n"
DEFAULT_CHUNK_SIZE = 16 * 1024


def close_fd(
    fd: int,
    filesystem: str,
    path: str,
) -> None:
    """Close a file descriptor and report advisory failures."""
    try:
        with metrics.operation(filesystem, "close"):
            os.close(fd)
    except OSError as exc:
        log.error(f"failed to close {filesystem} path {path}: {format_error(exc)}")


def safe_user(value: str) -> str:
    """Return a username that is safe to use as a single path component."""
    if not value or "/" in value or value in {".", ".."}:
        raise ValueError("user must be a single path component")
    return value
