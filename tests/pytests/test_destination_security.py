"""Destination confinement and replacement races."""

import os
from pathlib import Path

from logsync.sync import sync_user
from tests.pytests.support import encoded_source_file


def test_syncs_files_to_different_destination_prefix_styles(tmp_path: Path) -> None:
    """Sync separate source files to directory and username-prefixed destinations."""
    source = tmp_path / "source"
    primary_user_dir = tmp_path / "primary" / "alice"
    archive_user_dir = tmp_path / "archive" / "u-alice"
    source.mkdir()
    primary_user_dir.mkdir(parents=True)
    archive_user_dir.mkdir(parents=True)
    encoded_source_file(source, primary_user_dir / "current.log").write_bytes(b"current...\n")
    encoded_source_file(source, archive_user_dir / "history.log").write_bytes(b"history...\n")

    stats = sync_user(
        str(source),
        [str(tmp_path / "primary") + "/", str(tmp_path / "archive") + "/u-"],
        "alice",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert stats.errors == 0
    assert (primary_user_dir / "current.log").read_bytes() == b"current...\n"
    assert (archive_user_dir / "history.log").read_bytes() == b"history...\n"


def test_rejects_path_outside_every_destination_prefix(tmp_path: Path) -> None:
    """Reject a source-encoded destination that matches none of the configured user directories."""
    source = tmp_path / "source"
    first_prefix = tmp_path / "first"
    second_prefix = tmp_path / "second"
    outside = tmp_path / "outside" / "alice"
    (first_prefix / "alice").mkdir(parents=True)
    (second_prefix / "alice").mkdir(parents=True)
    encoded_source_file(source, outside / "app.log").write_bytes(b"one...\n")

    stats = sync_user(
        str(source),
        [str(first_prefix) + "/", str(second_prefix) + "/"],
        "alice",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert stats.errors == 1
    assert not outside.exists()


def test_symlink_race_cannot_escape_user_dir(tmp_path: Path, monkeypatch) -> None:
    """Prevent a post-canonicalization symlink replacement from escaping the user directory."""
    source = tmp_path / "source"
    homes = tmp_path / "homes"
    service = homes / "alice" / "service"
    outside = tmp_path / "outside"
    source.mkdir()
    service.mkdir(parents=True)
    outside.mkdir()
    (homes / "alice" / "app.log").symlink_to("service/main")
    encoded_source_file(source, homes / "alice" / "app.log").write_bytes(b"one...\n")
    real_realpath = os.path.realpath
    replaced = False

    def replace_after_realpath(path: os.PathLike[str], *, strict: bool = False) -> str:
        nonlocal replaced
        result = real_realpath(path, strict=strict)
        if not replaced and Path(path).name == "app.log":
            replaced = True
            service.rmdir()
            service.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr("logsync.destination.os.path.realpath", replace_after_realpath)

    stats = sync_user(
        str(source),
        [str(homes) + "/"],
        "alice",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert replaced
    assert stats.errors == 1
    assert list(outside.iterdir()) == []


def test_user_dir_is_opened_after_destination_validation(tmp_path: Path, monkeypatch) -> None:
    """Open the user directory after validation so replacement cannot redirect writes."""
    source = tmp_path / "source"
    homes = tmp_path / "homes"
    user_dir = homes / "alice"
    relocated = tmp_path / "relocated-alice"
    source.mkdir()
    user_dir.mkdir(parents=True)
    encoded_source_file(source, user_dir / "app.log").write_bytes(b"one...\n")
    real_realpath = os.path.realpath
    replaced = False

    def replace_after_realpath(path: os.PathLike[str], *, strict: bool = False) -> str:
        nonlocal replaced
        result = real_realpath(path, strict=strict)
        if not replaced and Path(path).name == "app.log":
            replaced = True
            user_dir.rename(relocated)
            user_dir.mkdir()
        return result

    monkeypatch.setattr("logsync.destination.os.path.realpath", replace_after_realpath)

    stats = sync_user(
        str(source),
        [str(homes) + "/"],
        "alice",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert replaced
    assert stats.errors == 0
    assert (user_dir / "app.log").read_bytes() == b"one...\n"
    assert list(relocated.iterdir()) == []


def test_rejects_source_encoded_path_outside_user_dir(tmp_path: Path) -> None:
    """Reject a source path encoding a destination outside the selected user directory."""
    source = tmp_path / "source"
    destination_prefix = tmp_path / "shared" / "users"
    (destination_prefix / "alice").mkdir(parents=True)
    encoded_source_file(source, destination_prefix / "bob" / "test.log").write_bytes(
        b"one...\n"
    )

    stats = sync_user(
        str(source),
        [str(destination_prefix) + "/"],
        "alice",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert stats.errors == 1
    assert not (destination_prefix / "bob").exists()
