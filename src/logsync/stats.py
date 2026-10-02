"""Synchronization result values shared with telemetry."""

from dataclasses import dataclass


@dataclass(slots=True)
class SyncStats:
    """Summarize one completed per-user synchronization call."""

    files_active: int = 0
    files_gc: int = 0
    directories_scanned: int = 0
    directories_removed: int = 0
    quarantined: int = 0
    collected: int = 0
    errors: int = 0
    deferred: bool = False

    @property
    def files_scanned(self) -> int:
        """Return all physical files observed during the source scan."""
        return self.files_active + self.files_gc
