"""Multi-user layout and failure isolation."""

import os
from pathlib import Path
from unittest.mock import Mock

from logsync.cli import main
from logsync.sync import SyncStats
from logsync.sync import _Context, _sync_file
from tests.pytests.support import encoded_source_file


def test_source_tree_encodes_destination_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Map a source log to the absolute destination encoded below its source user directory."""
    source_dir = tmp_path / "src-u-alice"
    destination_user_dir = tmp_path / "destination-u-alice"
    source_dir.mkdir(parents=True)
    destination_user_dir.mkdir()
    encoded_source_file(source_dir, destination_user_dir / "app.log").write_bytes(b"alice...\n")
    monkeypatch.setattr(
        "logsync.runtime.resolve_user",
        lambda user: (user, os.getuid(), os.getgid()),
    )

    assert main(
        [
            "--source-prefix",
            f"{tmp_path}/src-u-",
            "--dest-prefix",
            f"{tmp_path}/destination-u-",
            "--user",
            "alice",
        ]
    ) == 0
    assert (destination_user_dir / "app.log").read_bytes() == b"alice...\n"


def test_source_tree_routes_to_multiple_destination_prefixes(tmp_path: Path, monkeypatch) -> None:
    """Route source-encoded logs into either configured destination prefix."""
    source_dir = tmp_path / "source" / "alice"
    primary_user_dir = tmp_path / "primary" / "alice"
    archive_user_dir = tmp_path / "archive" / "alice"
    source_dir.mkdir(parents=True)
    primary_user_dir.mkdir(parents=True)
    archive_user_dir.mkdir(parents=True)
    encoded_source_file(source_dir, primary_user_dir / "app.log").write_bytes(b"primary...\n")
    encoded_source_file(source_dir, archive_user_dir / "app.log").write_bytes(b"archive...\n")
    monkeypatch.setattr(
        "logsync.runtime.resolve_user",
        lambda user: (user, os.getuid(), os.getgid()),
    )

    assert main(
        [
            "--source-prefix",
            f"{tmp_path}/source/",
            "--dest-prefix",
            f"{tmp_path}/primary/",
            "--dest-prefix",
            f"{tmp_path}/archive/",
            "--user",
            "alice",
        ]
    ) == 0
    assert (primary_user_dir / "app.log").read_bytes() == b"primary...\n"
    assert (archive_user_dir / "app.log").read_bytes() == b"archive...\n"


def test_first_letter_sharding_routes_each_user(tmp_path: Path, monkeypatch) -> None:
    """Route users through their pre-existing first-letter destination directories."""
    source_root = tmp_path / "source"
    destination_root = tmp_path / "home"
    for user, contents in (("alice", b"alice...\n"), ("bob", b"bob...\n")):
        source_dir = source_root / user
        source_dir.mkdir(parents=True)
        destination_user_dir = destination_root / user[0] / user
        destination_user_dir.mkdir(parents=True)
        encoded_source_file(source_dir, destination_user_dir / "app.log").write_bytes(contents)

    monkeypatch.setattr(
        "logsync.runtime.resolve_user",
        lambda user: (user, os.getuid(), os.getgid()),
    )

    assert main(
        [
            "--source-prefix",
            f"{source_root}/",
            "--dest-prefix",
            f"{destination_root}/",
            "--user-dir-sharding",
            "first-letter",
        ]
    ) == 0
    assert (destination_root / "a" / "alice" / "app.log").read_bytes() == b"alice...\n"
    assert (destination_root / "b" / "bob" / "app.log").read_bytes() == b"bob...\n"
    assert not (destination_root / "alice").exists()
    assert not (destination_root / "bob").exists()


def test_user_setup_failure_does_not_stop_later_user(tmp_path: Path, monkeypatch) -> None:
    """Continue to later users after credential setup fails while retaining destination chown."""
    source_root = tmp_path / "source"
    destination_root = tmp_path / "home"
    for user in ("alice", "bob"):
        (source_root / user).mkdir(parents=True)
        (destination_root / user).mkdir(parents=True)

    monkeypatch.setattr(
        "logsync.runtime.resolve_user",
        lambda user: (user, 1001 if user == "alice" else 1002, 100),
    )
    keyring_cache = Mock()
    keyring_cache.use.side_effect = [OSError("missing token"), None]
    def run_with_credentials(*_args, **kwargs) -> SyncStats:
        kwargs["credential_setup"]()
        return SyncStats()

    run_sync = Mock(side_effect=run_with_credentials)
    cache_type = Mock(return_value=keyring_cache)
    monkeypatch.setattr("logsync.runtime.HTCondorKeyringCache", cache_type)
    monkeypatch.setattr("logsync.runtime.sync_user", run_sync)

    assert main(
        [
            "--source-prefix",
            f"{source_root}/",
            "--dest-prefix",
            f"{destination_root}/",
            "--htcondor-keyring",
        ]
    ) == 1
    assert keyring_cache.use.call_count == 2
    assert run_sync.call_count == 2
    assert run_sync.call_args_list[1].args[2] == "bob"
    assert run_sync.call_args_list[1].kwargs["set_destination_ownership"] is True


def test_file_failure_stops_only_the_current_user(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Stop scanning the failed user while fully processing a later user."""
    source_root = tmp_path / "source"
    destination_root = tmp_path / "home"
    for user in ("alice", "bob"):
        source_dir = source_root / user
        source_dir.mkdir(parents=True)
        (destination_root / user).mkdir(parents=True)
        destination_user_dir = destination_root / user
        encoded_source_file(source_dir, destination_user_dir / "a.log").write_bytes(b"one...\n")
        encoded_source_file(source_dir, destination_user_dir / "b.log").write_bytes(b"two...\n")

    monkeypatch.setattr(
        "logsync.runtime.resolve_user",
        lambda user: (user, os.getuid(), os.getgid()),
    )
    attempted: list[tuple[str, str]] = []

    def fail_alice(ctx: _Context, source_path: str, *args, **kwargs) -> None:
        user = next(user for user in ("alice", "bob") if user in source_path.split("/"))
        attempted.append((user, os.path.basename(source_path)))
        if user == "alice":
            raise OSError("injected destination failure")
        _sync_file(ctx, source_path, *args, **kwargs)

    monkeypatch.setattr("logsync.sync._sync_file", fail_alice)

    assert main(
        [
            "--source-prefix",
            f"{source_root}/",
            "--dest-prefix",
            f"{destination_root}/",
        ]
    ) == 1
    assert len([entry for entry in attempted if entry[0] == "alice"]) == 1
    assert {entry for entry in attempted if entry[0] == "bob"} == {
        ("bob", "a.log"),
        ("bob", "b.log"),
    }
    assert (destination_root / "bob" / "a.log").read_bytes() == b"one...\n"
    assert (destination_root / "bob" / "b.log").read_bytes() == b"two...\n"
