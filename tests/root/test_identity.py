"""Real Linux effective-identity transitions that require root privileges."""

import os
import tempfile

from pathlib import Path
from typing import cast
from unittest.mock import patch

from logsync.config import Config
from logsync.identity import as_daemon, as_user
from logsync.afs import HTCondorKeyringCache
from logsync.runtime import _make_credential_setup, run
from logsync.stats import SyncStats


if os.geteuid() != 0:
    raise RuntimeError("tests/root requires effective UID 0; run it with `sudo make test-root`")


_COLLECTION_PID = os.getpid()


def test_runs_in_an_isolated_process() -> None:
    """Run each root test in a child process rather than in the pytest process that collected it."""
    assert os.getpid() != _COLLECTION_PID


def test_selects_and_restores_an_unprivileged_effective_identity() -> None:
    """Select an unprivileged EUID and EGID and restore root after leaving the context."""
    original = (os.geteuid(), os.getegid())
    assert original == (0, 0)
    with as_user(65534, 65534):
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
    assert (os.geteuid(), os.getegid()) == original


def test_nested_daemon_identity_returns_to_the_unprivileged_identity() -> None:
    """Temporarily regain the complete root identity and return to the user before restoring root."""
    daemon_groups = tuple(os.getgroups())
    with as_user(65534, 65534):
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
        assert os.getgroups() == []
        with as_daemon(0, 0, daemon_groups):
            assert (os.geteuid(), os.getegid()) == (0, 0)
            assert sorted(os.getgroups()) == sorted(daemon_groups)
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
        assert os.getgroups() == []
    assert (os.geteuid(), os.getegid()) == (0, 0)


def test_lazy_credential_setup_returns_to_the_unprivileged_identity() -> None:
    """Regain daemon identity for lazy credential setup and return to the user without requiring AFS."""
    class FakeKeyringCache:
        def use(self, uid: int) -> None:
            assert uid == 65534
            assert (os.geteuid(), os.getegid()) == (0, 0)

    daemon_groups = tuple(os.getgroups())
    credential_setup = _make_credential_setup(cast(HTCondorKeyringCache, FakeKeyringCache()), 65534, (0, 0, daemon_groups))
    assert credential_setup is not None
    with as_user(65534, 65534):
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
        credential_setup()
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
    assert (os.geteuid(), os.getegid()) == (0, 0)


def test_runtime_lazy_credential_setup_returns_to_user_before_sync_continues() -> None:
    """Restore complete daemon credentials lazily, then resume and finish without daemon supplementary groups."""
    class FakeKeyringCache:
        def __init__(self) -> None:
            pass

        def use_neutral(self) -> None:
            assert (os.geteuid(), os.getegid()) == (0, 0)
            assert os.getgroups() == [65532]

        def use(self, uid: int) -> None:
            assert uid == 65534
            assert (os.geteuid(), os.getegid()) == (0, 0)
            assert os.getgroups() == [65532]

    def sync_user(*_args, **kwargs) -> SyncStats:
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
        assert os.getgroups() == [65531]
        kwargs["credential_setup"]()
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
        assert os.getgroups() == [65531]
        return SyncStats()

    original_groups = os.getgroups()
    try:
        os.setgroups([65532])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            with (
                patch("logsync.runtime.resolve_user", return_value=("nobody", 65534, 65534)),
                patch("logsync.runtime.resolve_supplementary_groups", return_value=(65531,)),
                patch("logsync.runtime.HTCondorKeyringCache", FakeKeyringCache),
                patch("logsync.runtime.sync_user", side_effect=sync_user),
            ):
                result_code = run(Config(
                    source_prefix=str(source) + "/",
                    dest_prefixes=[str(destination) + "/"],
                    user="nobody",
                    htcondor_keyring=True,
                    use_effective_identity=True,
                ))

        assert result_code == 0
        assert (os.geteuid(), os.getegid()) == (0, 0)
        assert os.getgroups() == [65532]
    finally:
        os.setgroups(original_groups)


def test_runtime_restores_daemon_groups_after_user_sync_failure() -> None:
    """Restore daemon supplementary groups when synchronization fails under the target identity."""
    def sync_user(*_args, **_kwargs):
        assert (os.geteuid(), os.getegid()) == (65534, 65534)
        assert os.getgroups() == []
        raise OSError("injected")

    original_groups = os.getgroups()
    try:
        os.setgroups([65532])
        with (
            patch("logsync.runtime.resolve_user", return_value=("nobody", 65534, 65534)),
            patch("logsync.runtime.resolve_supplementary_groups", return_value=()),
            patch("logsync.runtime.sync_user", side_effect=sync_user),
        ):
            result_code = run(Config(
                source_prefix="/source/",
                dest_prefixes=["/destination/"],
                user="nobody",
                use_effective_identity=True,
            ))

        assert result_code == 1
        assert (os.geteuid(), os.getegid()) == (0, 0)
        assert os.getgroups() == [65532]
    finally:
        os.setgroups(original_groups)


def test_restores_effective_identity_after_operation_failure() -> None:
    """Restore root EUID and EGID when the protected operation raises an exception."""
    try:
        with as_user(65534, 65534):
            assert (os.geteuid(), os.getegid()) == (65534, 65534)
            raise RuntimeError("injected")
    except RuntimeError as exc:
        assert str(exc) == "injected"
    else:
        raise AssertionError("operation failure was not propagated")
    assert (os.geteuid(), os.getegid()) == (0, 0)


def test_preserves_real_and_saved_identities_and_restores_supplementary_groups() -> None:
    """Clear supplementary groups as the user, then restore them with real and saved identities."""
    original_resuid = os.getresuid()
    original_resgid = os.getresgid()
    original_groups = os.getgroups()
    with as_user(65534, 65534):
        selected_resuid = os.getresuid()
        selected_resgid = os.getresgid()
        assert selected_resuid == (original_resuid[0], 65534, original_resuid[2])
        assert selected_resgid == (original_resgid[0], 65534, original_resgid[2])
        assert os.getgroups() == []
    assert os.getresuid() == original_resuid
    assert os.getresgid() == original_resgid
    assert sorted(os.getgroups()) == sorted(original_groups)


def test_nested_transition_restores_a_nonzero_daemon_gid() -> None:
    """Restore the exact daemon identity across a nested root transition when its EGID is nonzero."""
    os.setegid(123)
    try:
        with as_user(65534, 65533):
            assert (os.geteuid(), os.getegid()) == (65534, 65533)
            with as_daemon(0, 123):
                assert (os.geteuid(), os.getegid()) == (0, 123)
            assert (os.geteuid(), os.getegid()) == (65534, 65533)
        assert (os.geteuid(), os.getegid()) == (0, 123)
    finally:
        os.setegid(0)
    assert (os.geteuid(), os.getegid()) == (0, 0)


def test_failed_uid_selection_rolls_back_gid() -> None:
    """Restore EGID when the kernel rejects an out-of-range target EUID after EGID changed."""
    try:
        with as_user(2**32, 65534):
            raise AssertionError("invalid EUID was selected")
    except OverflowError:
        pass
    else:
        raise AssertionError("invalid EUID was accepted")
    assert (os.geteuid(), os.getegid()) == (0, 0)


def test_filesystem_access_tracks_nested_effective_identity() -> None:
    """Deny root-owned file access as the user, allow it as nested root, then deny it again as the user."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o711)
        protected = root / "protected"
        protected.write_bytes(b"secret")
        protected.chmod(0o600)
        with as_user(65534, 65534):
            try:
                protected.read_bytes()
            except PermissionError:
                pass
            else:
                raise AssertionError("user read a root-only file")
            with as_daemon(0, 0):
                assert protected.read_bytes() == b"secret"
            try:
                protected.read_bytes()
            except PermissionError:
                pass
            else:
                raise AssertionError("user retained root file access")


def test_creation_ownership_uses_effective_identity() -> None:
    """Assign newly created files and directories to the selected effective UID and GID without chown."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o777)
        with as_user(65534, 65533):
            directory = root / "created-directory"
            directory.mkdir()
            created_file = root / "created-file"
            created_file.write_bytes(b"data")
        assert (directory.stat().st_uid, directory.stat().st_gid) == (65534, 65533)
        assert (created_file.stat().st_uid, created_file.stat().st_gid) == (65534, 65533)


def test_effective_gid_grants_group_only_directory_access() -> None:
    """Use the selected EGID to create a file in a group-only directory without supplementary-group access."""
    assert 65533 not in os.getgroups()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o711)
        group_directory = root / "group-directory"
        group_directory.mkdir()
        os.chown(group_directory, 0, 65533)
        group_directory.chmod(0o070)
        with as_user(65534, 65533):
            created = group_directory / "created"
            created.write_bytes(b"data")
        assert (created.stat().st_uid, created.stat().st_gid) == (65534, 65533)


def test_daemon_supplementary_group_does_not_grant_user_access() -> None:
    """Deny user access that would otherwise be inherited from a daemon supplementary group."""
    original_groups = os.getgroups()
    try:
        os.setgroups([65532])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o711)
            group_directory = root / "daemon-group-directory"
            group_directory.mkdir()
            os.chown(group_directory, 0, 65532)
            group_directory.chmod(0o070)
            with as_user(65534, 65534):
                assert os.getgroups() == []
                try:
                    (group_directory / "unexpected").write_bytes(b"data")
                except PermissionError:
                    pass
                else:
                    raise AssertionError("daemon supplementary group granted user access")
        assert os.getgroups() == [65532]
    finally:
        os.setgroups(original_groups)


def test_user_supplementary_group_grants_directory_traversal() -> None:
    """Allow traversal through a directory using a selected user supplementary group."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o711)
        group_directory = root / "user-group-directory"
        group_directory.mkdir()
        os.chown(group_directory, 0, 65531)
        group_directory.chmod(0o050)
        readable = group_directory / "readable"
        readable.write_bytes(b"data")
        readable.chmod(0o004)
        with as_user(65534, 65534, (65531,)):
            assert os.getgroups() == [65531]
            assert readable.read_bytes() == b"data"
