"""Typed synchronization helpers shared by pytest modules."""

import os
from pathlib import Path

from logsync.sync import SyncStats, sync_user as confined_sync_path


def encoded_source_file(source: Path, destination: Path) -> Path:
    """Return a source path that encodes the absolute destination path."""
    path = source.joinpath(*destination.parts[1:])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def sync_path(source: Path, destination: Path) -> SyncStats:
    """Synchronize into a temporary destination per-user directory."""
    destination.mkdir(parents=True, exist_ok=True)
    return confined_sync_path(
        str(source),
        [str(destination.parent) + "/"],
        destination.name,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
