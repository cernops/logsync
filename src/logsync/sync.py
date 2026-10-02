"""Core log synchronization logic."""

import errno
import logging
import os
import random
import time
from collections.abc import Callable

from dataclasses import dataclass, field

from .metrics import metrics
from .common import DEFAULT_CHUNK_SIZE, RECORD_TERMINATOR, close_fd
from .destination import UserDirSharding, open_dest_file
from .errors import format_error
from .filenames import (
    QUARANTINE_FILE_PREFIX,
    logical_source_path,
    quarantine_file_path,
    source_filename_limit,
)
from .leases import release_write_lease, verify_lease_unbroken, get_write_lease
from .linux import punch_hole, rename_noreplace
from .signals import critical_section
from .stats import SyncStats

log = logging.getLogger(__name__)

@dataclass(slots=True)
class _Context:
    """State shared by one user synchronization pass."""

    progress: SyncStats
    user: str
    chunk_size: int
    filename_limit: int
    destination_prefixes: list[str] = field(default_factory=list)
    owner_uid: int = 0
    owner_gid: int = 0
    user_dir_sharding: UserDirSharding = UserDirSharding.NONE
    set_destination_ownership: bool = True
    keep_user_top_dir: bool = False
    credential_setup: Callable[[], None] | None = None
    credentials_ready: bool = False


def sync_user(
    source_dir: str,
    dest_prefixes: list[str],
    user: str,
    *,
    owner_uid: int,
    owner_gid: int,
    user_dir_sharding: UserDirSharding = UserDirSharding.NONE,
    set_destination_ownership: bool = True,
    keep_user_top_dir: bool = False,
    max_filename_bytes: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_user_seconds: float = 1.0,
    credential_setup: Callable[[], None] | None = None,
) -> SyncStats:
    """Drain complete records into a user's confined destination directory."""
    started = time.monotonic()
    with metrics.phase(user, "source_setup"):
        with metrics.operation("source", "resolve_directory"):
            source_dir = os.path.realpath(source_dir, strict=True)
        filename_limit = source_filename_limit(source_dir, max_filename_bytes)
        if chunk_size <= 0:
            raise ValueError("chunk size must be positive")
        if max_user_seconds <= 0 or not float("-inf") < max_user_seconds < float("inf"):
            raise ValueError("maximum user duration must be a positive finite number")
        if not os.path.isdir(source_dir):
            raise NotADirectoryError(source_dir)
    deadline = started + max_user_seconds

    ctx = _Context(
        progress=SyncStats(),
        user=user,
        chunk_size=chunk_size,
        filename_limit=filename_limit,
        destination_prefixes=dest_prefixes,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        user_dir_sharding=user_dir_sharding,
        set_destination_ownership=set_destination_ownership,
        keep_user_top_dir=keep_user_top_dir,
        credential_setup=credential_setup,
    )

    try:
        with metrics.phase(ctx.user, "source_scan"):
            source_files = _source_files(ctx, source_dir)
    except OSError as exc:
        log.error(f"source_scan {source_dir!r}: {format_error(exc)}")
        ctx.progress.errors += 1
        metrics.record_sync(ctx.user, ctx.progress)
        return ctx.progress

    random.shuffle(source_files) # to avoid one log starving the rest
    for logical_path in source_files:
        if time.monotonic() >= deadline:
            ctx.progress.deferred = True
            break
        destination_path = destination_path_for_source(source_dir, logical_path)
        try:
            _sync_file(
                ctx,
                logical_path,
                destination_path,
            )
        except OSError as exc:
            log.error(f"while syncing {logical_path!r}: {format_error(exc)}")
            ctx.progress.errors += 1
            break

    metrics.record_sync(ctx.user, ctx.progress)
    return ctx.progress


def destination_path_for_source(source_dir: str, source_path: str) -> str:
    """Map a source path to its encoded absolute destination path."""
    relative = os.path.relpath(source_path, source_dir)
    if relative == os.pardir or relative.startswith(f"{os.pardir}/"):
        raise ValueError(f"source path is outside source directory: {source_path}")
    return "/" + relative


def _source_files(
    ctx: _Context,
    source_dir: str,
) -> list[str]:
    """Return unique logical paths for physical source files."""
    pending = [source_dir]
    logical_files: set[str] = set()
    while pending:
        directory = pending.pop()
        directory_is_empty = True
        with metrics.operation("source", "scandir"):
            entries_context = os.scandir(directory)
        with entries_context as entries:
            for entry in entries:
                directory_is_empty = False
                with metrics.operation("source", "inspect_entry"):
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.startswith(QUARANTINE_FILE_PREFIX):
                            logical = logical_source_path(entry.path)
                            ctx.progress.files_gc += 1
                        else:
                            logical = entry.path
                            ctx.progress.files_active += 1
                        logical_files.add(logical)
                    else:
                        log.warning(f"skipping non-regular source entry {entry.path!r}")
        ctx.progress.directories_scanned += 1
        if directory_is_empty and (directory != source_dir or not ctx.keep_user_top_dir):
            try:
                with metrics.operation("source", "rmdir"):
                    try:
                        os.rmdir(directory)
                    except OSError as exc:
                        if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                            raise
                    else:
                        ctx.progress.directories_removed += 1
            except OSError as exc:
                log.error(f"rmdir {directory!r}: {format_error(exc)}")
                ctx.progress.errors += 1
    return list(logical_files)


def _sync_file(
    ctx: _Context,
    logical_path: str,
    destination_path: str,
) -> None:
    """Synchronize both physical generations of one logical source."""
    quarantine_path = quarantine_file_path(logical_path, ctx.filename_limit)
    quarantine_exists = _sync_generation(
        ctx,
        quarantine_path,
        destination_path,
        collect_if_drained=True,
        quarantine_path=None,
    )
    _ = _sync_generation(
        ctx,
        logical_path,
        destination_path,
        collect_if_drained=not quarantine_exists,
        quarantine_path=quarantine_path,
    )


def _sync_generation(
    ctx: _Context,
    source_path: str,
    destination_path: str,
    *,
    collect_if_drained: bool,
    quarantine_path: str | None,
) -> bool:
    """Synchronize one generation and optionally quarantine or collect it."""
    result = _sync_real_file(
        ctx,
        source_path,
        destination_path,
    )
    if result is None:
        return False

    src_fd = result
    try:
        with metrics.phase(ctx.user, "collection"):
            if collect_if_drained:
                collected = int(
                    _collect_if_drained(
                        ctx,
                        source_path,
                        src_fd,
                        quarantine_path=quarantine_path,
                    )
                )
                if quarantine_path is None:
                    ctx.progress.collected += collected
                else:
                    ctx.progress.quarantined += collected
    finally:
        close_fd(src_fd, "source", source_path)
    return True


def _sync_real_file(
    ctx: _Context,
    source_path: str,
    destination_path: str,
) -> int | None:
    """Open and synchronize one physical source-file generation."""
    src_fd = -1
    try:
        with metrics.phase(ctx.user, "file_analysis"):
            try:
                with metrics.operation("source", "open_file"):
                    src_fd = os.open(source_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None

            start_of_data = _find_start_of_data(ctx, src_fd)
            if start_of_data is None:
                end_of_data = None
            else:
                end_of_data = _last_complete_record_boundary(
                    ctx,
                    src_fd,
                    start_of_data,
                )

        if end_of_data is not None:
            assert start_of_data is not None
            if ctx.credential_setup is not None and not ctx.credentials_ready:
                with metrics.phase(ctx.user, "credential_setup"):
                    ctx.credential_setup()
                ctx.credentials_ready = True
            with metrics.phase(ctx.user, "destination_setup"):
                dst_fd = open_dest_file(
                    destination_path,
                    ctx.owner_uid,
                    ctx.owner_gid,
                    ctx.set_destination_ownership,
                    user_dir_sharding=ctx.user_dir_sharding,
                    path_prefixes=ctx.destination_prefixes,
                    user=ctx.user,
                )
            try:
                _sync_range(
                    ctx,
                    src_fd,
                    dst_fd,
                    start_of_data=start_of_data,
                    end_of_data=end_of_data,
                )
            finally:
                close_fd(dst_fd, "destination", destination_path)
    except BaseException:
        if src_fd >= 0:
            close_fd(src_fd, "source", source_path)
            src_fd = -1
        raise
    return src_fd


def _collect_if_drained(
    ctx: _Context,
    source_path: str,
    src_fd: int,
    *,
    quarantine_path: str | None,
) -> bool:
    """Rename a drained `source_path` to `quarantine_path`, or unlink it `quarantine_path=None` (i.e. already quarantined)."""
    with metrics.operation("source", "lease"):
        acquired = get_write_lease(src_fd)
    if acquired is None:
        return False
    try:
        if _find_start_of_data(ctx, src_fd) is not None:
            return False
        with metrics.operation("source", "lease"):
            if not verify_lease_unbroken(src_fd, acquired):
                return False

        if quarantine_path is None:
            try:
                with metrics.operation("source", "unlink"):
                    os.unlink(source_path)
            except OSError as exc:
                log.error(f"unlink {source_path!r}: {format_error(exc)}")
                return False
        else:
            with metrics.operation("source", "rename"):
                rename_noreplace(source_path, quarantine_path)
        return True
    finally:
        with metrics.operation("source", "lease"):
            release_write_lease(src_fd)


def _sync_range(
    ctx: _Context,
    src_fd: int,
    dst_fd: int,
    *,
    start_of_data: int,
    end_of_data: int,
) -> None:
    with critical_section(False):
        with metrics.phase(ctx.user, "data_transfer"):
            _ = _copy_range(
                ctx,
                src_fd,
                dst_fd,
                start=start_of_data,
                end=end_of_data,
            )
            with metrics.operation("destination", "fsync"):
                os.fsync(dst_fd)
            with metrics.operation("source", "punch_hole"):
                punch_hole(src_fd, 0, end_of_data)
            with metrics.operation("source", "fsync"):
                os.fsync(src_fd)


def _find_start_of_data(
    ctx: _Context,
    src_fd: int,
) -> int | None:
    with metrics.operation("source", "seek_data"):
        try:
            pos = os.lseek(src_fd, 0, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                return None
            raise

    while True:
        with metrics.operation("source", "pread_nonzero"):
            chunk = os.pread(src_fd, ctx.chunk_size, pos)
        if not chunk:
            return None
        for index, byte in enumerate(chunk):
            if byte:
                return pos + index
        pos += len(chunk)


def _last_complete_record_boundary(
    ctx: _Context,
    fd: int,
    start: int,
) -> int | None:
    pos = start
    suffix = b""
    last_boundary: int | None = None

    while True:
        with metrics.operation("source", "pread_records"):
            chunk = os.pread(fd, ctx.chunk_size, pos)
        if not chunk:
            break

        data = suffix + chunk
        data_base = pos - len(suffix)
        found = data.rfind(RECORD_TERMINATOR)
        if found != -1:
            last_boundary = data_base + found + len(RECORD_TERMINATOR)

        suffix = data[-(len(RECORD_TERMINATOR) - 1) :]
        pos += len(chunk)

    if last_boundary is None or last_boundary <= start:
        return None
    return last_boundary


def _copy_range(
    ctx: _Context,
    src_fd: int,
    dst_fd: int,
    *,
    start: int,
    end: int,
) -> int:
    pos = start
    while pos < end:
        with metrics.operation("source", "pread_copy"):
            chunk = os.pread(src_fd, min(ctx.chunk_size, end - pos), pos)
        if not chunk:
            raise OSError(errno.EIO, f"Could only read {pos-start} out of {end-start} bytes")
        _write_all(dst_fd, chunk)
        pos += len(chunk)
    return end - start


def _write_all(
    fd: int,
    data: bytes,
) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        with metrics.operation("destination", "write"):
            count = os.write(fd, view[written:])
        if count == 0:
            raise OSError(errno.EIO, "write returned zero bytes")
        written += count
