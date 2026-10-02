"""Cached selection of isolated HTCondor keyring sessions."""

from unittest.mock import Mock, call

from logsync.afs import HTCondorKeyringCache


def test_missing_libkeyutils_only_fails_when_keyring_support_is_used(monkeypatch) -> None:
    """Fail when constructing the optional keyring cache if libkeyutils is unavailable."""
    monkeypatch.setattr("logsync.afs._keyutils_library", None)
    monkeypatch.setattr("logsync.afs.ctypes.util.find_library", lambda _name: None)
    monkeypatch.setattr("logsync.afs.ctypes.CDLL", Mock(side_effect=OSError("library unavailable")))

    try:
        HTCondorKeyringCache()
    except OSError as exc:
        assert "HTCondor keyring support requires libkeyutils" in str(exc)
    else:
        raise AssertionError("missing libkeyutils unexpectedly succeeded")


def install_operations(monkeypatch, *, joins, anchors=()) -> tuple[Mock, Mock, Mock]:
    """Install controllable keyring operations for cache tests."""
    join = Mock(side_effect=joins)
    find = Mock(side_effect=anchors)
    link = Mock()
    monkeypatch.setattr("logsync.afs._join_session", join)
    monkeypatch.setattr("logsync.afs._find_anchor", find)
    monkeypatch.setattr("logsync.afs._link_anchor", link)
    monkeypatch.setattr("logsync.afs._link_anchor_from_description", link)
    return join, find, link


def test_first_use_builds_session_and_unexpired_hit_only_rejoins(monkeypatch) -> None:
    """Build and link on first use, then only rejoin the named session before expiry."""
    now = [10.0]
    join, find, link = install_operations(monkeypatch, joins=[101, 101], anchors=[201])
    cache = HTCondorKeyringCache(clock=lambda: now[0])

    cache.use(1001)
    now[0] = 909.0
    cache.use(1001)

    assert join.call_count == 2
    assert join.call_args_list[0] == join.call_args_list[1]
    find.assert_called_once_with(1001)
    link.assert_called_once_with(1001, 201)


def test_different_uids_use_distinct_sessions_and_anchors(monkeypatch) -> None:
    """Keep session names and linked anchors distinct while switching users."""
    join, find, link = install_operations(monkeypatch, joins=[101, 102, 101], anchors=[201, 202])
    cache = HTCondorKeyringCache(clock=lambda: 0.0)

    cache.use(1001)
    cache.use(1002)
    cache.use(1001)

    assert join.call_args_list[0] != join.call_args_list[1]
    assert join.call_args_list[0] == join.call_args_list[2]
    assert find.call_args_list == [call(1001), call(1002)]
    assert link.call_args_list == [call(1001, 201), call(1002, 202)]


def test_expiry_replaces_session_with_current_anchor(monkeypatch) -> None:
    """Create a new session with the current anchor when the soft TTL expires."""
    now = [0.0]
    join, find, link = install_operations(monkeypatch, joins=[101, 102, 102], anchors=[201, 202])
    cache = HTCondorKeyringCache(clock=lambda: now[0])

    cache.use(1001)
    now[0] = 900.0
    cache.use(1001)
    cache.use(1001)

    assert join.call_args_list[0] != join.call_args_list[1]
    assert join.call_args_list[1] == join.call_args_list[2]
    assert find.call_args_list == [call(1001), call(1001)]
    assert link.call_args_list == [call(1001, 201), call(1001, 202)]


def test_failed_refresh_restores_stale_session_and_retries(monkeypatch, caplog) -> None:
    """Warn, restore stale credentials, and retry refresh on every expired use."""
    now = [0.0]
    join, find, link = install_operations(monkeypatch, joins=[101, 102, 101, 103, 101], anchors=[201, OSError("missing"), OSError("missing")])
    cache = HTCondorKeyringCache(clock=lambda: now[0])

    cache.use(1001)
    now[0] = 900.0
    cache.use(1001)
    cache.use(1001)

    assert find.call_count == 3
    assert join.call_args_list[0] == join.call_args_list[2] == join.call_args_list[4]
    assert "using stale session" in caplog.text
    link.assert_called_once_with(1001, 201)


def test_failed_refresh_and_stale_restoration_propagates(monkeypatch) -> None:
    """Propagate a credential error when refresh and stale restoration both fail."""
    now = [0.0]
    install_operations(monkeypatch, joins=[101, 102, OSError("stale gone")], anchors=[201, OSError("missing")])
    cache = HTCondorKeyringCache(clock=lambda: now[0])
    cache.use(1001)
    now[0] = 900.0

    try:
        cache.use(1001)
    except OSError as exc:
        assert "stale gone" in str(exc)
    else:
        raise AssertionError("stale restoration unexpectedly succeeded")


def test_recreated_named_session_receives_cached_anchor(monkeypatch) -> None:
    """Relink the cached anchor when a named session is recreated with a new serial."""
    join, find, link = install_operations(monkeypatch, joins=[101, 999, 999], anchors=[201])
    cache = HTCondorKeyringCache(clock=lambda: 0.0)

    cache.use(1001)
    cache.use(1001)
    cache.use(1001)

    find.assert_called_once_with(1001)
    assert link.call_args_list == [call(1001, 201), call(201)]
