"""Confined destination path handling."""

import errno
import os
from enum import StrEnum
from typing import assert_never

from .metrics import metrics
from .common import close_fd, safe_user

from .signals import critical_section

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW

def safe_open_dir(path: str) -> int:
    """Open an absolute directory path component-by-component to avoid symlink traversal."""
    parts = path.split("/")
    assert parts[0] == "", path
    assert parts[-1] == "", path
    with metrics.operation("destination", "open_directory"):
        fd = os.open("/", _DIRECTORY_FLAGS)
    next_fd = fd
    current_path = "/"
    next_path = "/"
    try:
        for p in parts[1:-1]:
            with metrics.operation("destination", "open_directory"):
                next_path = current_path + p + "/"
                next_fd = os.open(p, _DIRECTORY_FLAGS, dir_fd=fd)
            close_fd(fd, "destination", current_path)
            current_path = next_path
            fd = next_fd
        return fd
    except:
        close_fd(fd, "destination", current_path)
        if fd != next_fd:
            close_fd(next_fd, "destination", next_path)
        raise


class UserDirSharding(StrEnum):
    """Supported destination user-directory layouts."""

    NONE = "none"
    FIRST_LETTER = "first-letter"


def _validate_dest_path(file_path: str, user_anchor_paths: list[str]) -> tuple[str, str]:
    """Return the canonical destination path and first matching user anchor."""
    assert file_path.startswith("/"), file_path

    with metrics.operation("destination", "realpath"):
        canonical = os.path.realpath(file_path, strict=False)

    for p in user_anchor_paths:
        assert p.endswith("/"), p
        if canonical.startswith(p):
            return canonical, p

    raise PermissionError(errno.EPERM, f"destination path {file_path!r} resolves to {canonical!r} which is outside per-user directories")


def open_dest_file(
    file_path: str,
    uid: int,
    gid: int,
    set_ownership: bool,
    *,
    user_dir_sharding: UserDirSharding,
    path_prefixes: list[str],
    user: str,
) -> int:
    """Resolve `file_path`, validate it against `path_prefixes` for the given `user` and open it."""
    user_anchor_paths = [get_user_anchor_path(user_dir_sharding, path_prefix, user) for path_prefix in path_prefixes]
    canonical, matched_anchor_path = _validate_dest_path(file_path, user_anchor_paths)
    relative = canonical[len(matched_anchor_path):]
    current_path = matched_anchor_path
    current_fd = safe_open_dir(matched_anchor_path)
    next_path = current_path
    next_fd = current_fd
    try:
        parts = relative.split("/")
        for component in parts[:-1]:
            next_path = current_path + component + "/"
            next_fd = _open_or_create_dir(
                current_fd,
                component,
                uid,
                gid,
                set_ownership,
            )
            close_fd(current_fd, "destination", current_path)
            current_path = next_path
            current_fd   = next_fd
        return _open_or_create_file(
            current_fd,
            parts[-1],
            uid,
            gid,
            set_ownership,
        )
    finally:
        close_fd(current_fd, "destination", current_path)
        if current_fd != next_fd:
            close_fd(next_fd, "destination", next_path)


def get_user_anchor_path(
    user_dir_sharding: UserDirSharding,
    path_prefix: str,
    user: str,
) -> str:
    """Return the destination per-user directory path without accessing it."""
    user = safe_user(user)
    if user_dir_sharding is UserDirSharding.NONE:
        return path_prefix + user + "/"
    if user_dir_sharding is UserDirSharding.FIRST_LETTER:
        return path_prefix + user[0] + "/" + user + "/"
    assert_never(user_dir_sharding)


def _open_or_create_dir(
    parent_fd: int,
    name: str,
    uid: int,
    gid: int,
    set_ownership: bool,
) -> int:
    with metrics.operation("destination", "open_directory"):
        try:
            return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    with critical_section(True):
        with metrics.operation("destination", "mkdir"):
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                created = False
            else:
                created = True
        with metrics.operation("destination", "open_directory"):
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if not created:
            return fd
        try:
            if set_ownership:
                with metrics.operation("destination", "chown_directory"):
                    os.fchown(fd, uid, gid)
            with metrics.operation("destination", "chmod_directory"):
                os.fchmod(fd, 0o755)
        except BaseException:
            close_fd(fd, "destination", name)
            raise
        return fd


def _open_or_create_file(
    parent_fd: int,
    name: str,
    uid: int,
    gid: int,
    set_ownership: bool,
) -> int:
    with metrics.operation("destination", "open_file"):
        try:
            return os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    with critical_section(True):
        with metrics.operation("destination", "create_file"):
            try:
                fd = os.open(name, _FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                fd = None

        if fd is None:
            with metrics.operation("destination", "open_file"):
                return os.open(name, _FILE_FLAGS, dir_fd=parent_fd)

        try:
            if set_ownership:
                with metrics.operation("destination", "chown_file"):
                    os.fchown(fd, uid, gid)
            with metrics.operation("destination", "chmod_file"):
                os.fchmod(fd, 0o644)
        except BaseException:
            close_fd(fd, "destination", name)
            raise
        return fd
