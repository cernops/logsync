"""Confined synchronization helpers shared by unittest modules."""

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from logsync.metrics import metrics
from logsync.sync import DEFAULT_CHUNK_SIZE, SyncStats, sync_user as confined_sync_path


def encoded_source_file(source: Path, destination: Path) -> Path:
    """Return a source path that encodes the absolute destination path."""
    path = source.joinpath(*destination.parts[1:])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def reset_metrics() -> None:
    """Clear the process telemetry singleton between tests."""
    metrics._measurements.clear()  # pylint: disable=protected-access
    metrics._values.clear()  # pylint: disable=protected-access
    metrics._staged_values = None  # pylint: disable=protected-access


def sync_path(
    source: Path,
    destination: Path,
    max_filename_bytes: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    *,
    credential_setup: Callable[[], None] | None = None,
) -> SyncStats:
    """Use a temporary destination as the confined per-user directory."""
    destination.mkdir(parents=True, exist_ok=True)
    encoded_dir = source.joinpath(*destination.parts[1:])
    encoded_dir.mkdir(parents=True)
    top = source / destination.parts[1]
    staged = [entry for entry in source.iterdir() if entry != top]
    for entry in staged:
        entry.rename(encoded_dir / entry.name)
    try:
        return confined_sync_path(
            str(source),
            [str(destination.parent) + "/"],
            destination.name,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            max_filename_bytes=max_filename_bytes,
            chunk_size=chunk_size,
            credential_setup=credential_setup,
        )
    finally:
        for entry in encoded_dir.iterdir():
            entry.rename(source / entry.name)
        shutil.rmtree(top)
