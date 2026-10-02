"""Temporary effective process identity."""

import os
from collections.abc import Iterator
from contextlib import contextmanager


_Identity = tuple[int, int, tuple[int, ...]]


class IdentityFatalError(RuntimeError):
    """The process could not restore its previous effective identity."""


def _current_identity() -> _Identity:
    return os.geteuid(), os.getegid(), tuple(os.getgroups())


def _rollback(target: _Identity, *, euid: bool = False, egid: bool = False, groups: bool = False) -> None:
    target_uid, target_gid, target_groups = target
    # Restore the identity components in reverse order.
    # Keep the privileged EUID until the EGID and supplementary groups have been restored.
    if egid:
        try:
            os.setegid(target_gid)
        except BaseException as exc:
            raise IdentityFatalError(f"failed to restore EGID {target_gid}") from exc
    if groups:
        try:
            os.setgroups(target_groups)
        except BaseException as exc:
            raise IdentityFatalError(f"failed to restore supplementary groups {target_groups}") from exc
    if euid:
        try:
            os.seteuid(target_uid)
        except BaseException as exc:
            raise IdentityFatalError(f"failed to restore EUID {target_uid}") from exc


def _drop_to_user(target: _Identity) -> _Identity:
    previous = _current_identity()
    uid, gid, supplementary_groups = target
    os.setgroups(supplementary_groups)
    try:
        os.setegid(gid)
    except BaseException:
        _rollback(previous, groups=True)
        raise
    try:
        os.seteuid(uid)
    except BaseException:
        _rollback(previous, egid=True, groups=True)
        raise
    return previous


def _regain_daemon(target: _Identity) -> _Identity:
    previous = _current_identity()
    uid, gid, supplementary_groups = target
    os.seteuid(uid)
    try:
        os.setegid(gid)
    except BaseException:
        _rollback(previous, euid=True)
        raise
    try:
        os.setgroups(supplementary_groups)
    except BaseException:
        _rollback(previous, egid=True, euid=True)
        raise
    return previous


@contextmanager
def as_user(uid: int, gid: int, groups: tuple[int, ...] = ()) -> Iterator[None]:
    """Temporarily drop from the daemon identity to a user's effective identity."""
    target = uid, gid, groups
    previous = _drop_to_user(target)
    try:
        yield
    finally:
        try:
            _ = _regain_daemon(previous)
        except Exception as exc:
            raise IdentityFatalError(f"failed to restore {previous} from {target}") from exc


@contextmanager
def as_daemon(uid: int, gid: int, groups: tuple[int, ...] = ()) -> Iterator[None]:
    """Temporarily regain the daemon's effective identity from a user identity."""
    target = uid, gid, groups
    previous = _regain_daemon(target)
    try:
        yield
    finally:
        try:
            _ = _drop_to_user(previous)
        except Exception as exc:
            raise IdentityFatalError(f"failed to restore {previous} from {target}") from exc
