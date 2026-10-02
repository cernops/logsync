"""Use AFS credentials anchored by HTCondor in a Linux keyring."""

import ctypes
import ctypes.util
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from .errors import format_error


KEY_SPEC_SESSION_KEYRING = -3
KEY_SPEC_USER_KEYRING = -4
KEYRING_CACHE_TTL = 900.0


class _Keyutils(Protocol):
    """Operations used from libkeyutils."""

    def keyctl_join_session_keyring(self, name: bytes | None) -> int:
        """Join or create a session keyring."""
        ...

    def keyctl_search(self, keyring: int, key_type: bytes, description: bytes, destination: int) -> int:
        """Search a keyring for a key."""
        ...

    def keyctl_link(self, key: int, keyring: int) -> int:
        """Link a key into a keyring."""
        ...


_keyutils_library: _Keyutils | None = None


def _get_keyutils() -> _Keyutils:
    """Load and configure libkeyutils when keyring support is first used."""
    global _keyutils_library  # pylint: disable=global-statement
    if _keyutils_library is not None:
        return _keyutils_library

    library_name = ctypes.util.find_library("keyutils") or "libkeyutils.so.1"
    try:
        keyutils = ctypes.CDLL(library_name, use_errno=True)
    except OSError as exc:
        raise OSError(f"HTCondor keyring support requires libkeyutils: {exc}") from exc
    keyutils.keyctl_join_session_keyring.argtypes = [ctypes.c_char_p]
    keyutils.keyctl_join_session_keyring.restype = ctypes.c_int32
    keyutils.keyctl_search.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32]
    keyutils.keyctl_search.restype = ctypes.c_int32
    keyutils.keyctl_link.argtypes = [ctypes.c_int32, ctypes.c_int32]
    keyutils.keyctl_link.restype = ctypes.c_long
    _keyutils_library = cast(_Keyutils, cast(object, keyutils))
    return _keyutils_library


@dataclass(frozen=True)
class _CachedKeyring:
    session_name: str
    session_serial: int
    anchor_serial: int
    created_at: float


class HTCondorKeyringCache:
    """Select isolated named HTCondor sessions with a soft refresh interval."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        _ = _get_keyutils()
        self._clock: Callable[[], float] = clock
        self._namespace: str = os.urandom(16).hex()
        self._entries: dict[int, _CachedKeyring] = {}
        self._generation: int = 0

    def use(self, uid: int) -> None:
        """Join uid's cached session, refreshing it after the soft TTL."""
        cached = self._entries.get(uid)
        now = self._clock()
        if cached is None:
            self._entries[uid] = self._create(uid, now)
            return
        if now - cached.created_at < KEYRING_CACHE_TTL:
            self._restore(uid, cached)
            return
        try:
            refreshed = self._create(uid, now)
        except OSError as exc:
            logging.warning(f"failed to refresh HTCondor keyring session for uid {uid}; using stale session: {format_error(exc)}")
            self._restore(uid, cached)
            return
        self._entries[uid] = refreshed

    def use_neutral(self) -> None:
        """Join the process-unique session that contains no user credentials."""
        _ = _join_session(f"logsync-{self._namespace}-neutral")

    def _create(self, uid: int, now: float) -> _CachedKeyring:
        self._generation += 1
        session_name = f"logsync-{self._namespace}-{uid}-{self._generation}"
        session_serial = _join_session(session_name)
        anchor_serial = _find_anchor(uid)
        _link_anchor(uid, anchor_serial)
        return _CachedKeyring(session_name, session_serial, anchor_serial, now)

    def _restore(self, uid: int, cached: _CachedKeyring) -> None:
        session_serial = _join_session(cached.session_name)
        if session_serial != cached.session_serial:
            _link_anchor_from_description(cached.anchor_serial)
            self._entries[uid] = _CachedKeyring(cached.session_name, session_serial, cached.anchor_serial, cached.created_at)

def _join_session(name: str) -> int:
    keyutils = _get_keyutils()
    return _check_key_serial(
        keyutils.keyctl_join_session_keyring(name.encode("ascii")),
        f"could not join keyring session {name!r}",
    )


def _find_anchor(uid: int) -> int:
    keyutils = _get_keyutils()
    name = f"htcondor_uid{uid}"
    return _check_key_serial(
        keyutils.keyctl_search(KEY_SPEC_USER_KEYRING, b"keyring", name.encode("ascii"), 0),
        f"could not find HTCondor keyring {name!r}",
    )


def _link_anchor(uid: int, anchor_serial: int) -> None:
    keyutils = _get_keyutils()
    _check_result(
        keyutils.keyctl_link(anchor_serial, KEY_SPEC_SESSION_KEYRING),
        f"could not link HTCondor keyring 'htcondor_uid{uid}'",
    )


def _link_anchor_from_description(anchor_serial: int) -> None:
    keyutils = _get_keyutils()
    _check_result(
        keyutils.keyctl_link(anchor_serial, KEY_SPEC_SESSION_KEYRING),
        f"could not relink cached HTCondor keyring {anchor_serial}",
    )


def _check_key_serial(result: int, message: str) -> int:
    _check_result(result, message)
    return result


def _check_result(result: int, message: str) -> None:
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{message}: {os.strerror(error)}")
