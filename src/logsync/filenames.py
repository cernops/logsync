"""Source filename policy and quarantine naming."""

import errno
import os


QUARANTINE_FILE_PREFIX = "logsync-gc-"


def source_filename_limit(source_dir: str, configured_limit: int | None = None) -> int:
    """Return a valid source filename limit for this filesystem."""
    filesystem_limit = os.pathconf(source_dir, "PC_NAME_MAX") - len(os.fsencode(QUARANTINE_FILE_PREFIX))
    if configured_limit is None:
        return filesystem_limit
    if configured_limit <= 0:
        raise ValueError("maximum filename length must be positive")
    if configured_limit >= filesystem_limit:
        raise ValueError(f"configured filename limit {configured_limit} must be less than filesystem limit {filesystem_limit}")
    return configured_limit


def quarantine_file_path(source_path: str, filename_limit: int) -> str:
    """Return the reserved quarantine path for an active source file."""
    filename = os.path.basename(source_path)
    if len(os.fsencode(filename)) > filename_limit:
        raise OSError(
            errno.ENAMETOOLONG,
            f"source filename exceeds configured limit of {filename_limit} bytes",
            source_path,
        )
    return os.path.dirname(source_path) + f"/{QUARANTINE_FILE_PREFIX}{filename}"


def logical_source_path(quarantine_path: str) -> str:
    """Return the active/logical path represented by a quarantine path."""
    filename = os.path.basename(quarantine_path)
    if not filename.startswith(QUARANTINE_FILE_PREFIX):
        raise ValueError(f"not a quarantine path: {quarantine_path}")
    return os.path.dirname(quarantine_path) + "/" + filename[len(QUARANTINE_FILE_PREFIX):]
