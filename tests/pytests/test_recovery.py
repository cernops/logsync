"""Retry behavior and source and destination lifecycle recovery."""

import os
from pathlib import Path
from unittest.mock import patch

from tests.pytests.support import encoded_source_file, sync_path


def test_partial_write_fragment_precedes_complete_retry(
    tmp_path: Path,
) -> None:
    """Leave a partial fragment and append the complete range on retry without punching."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    data = b"one...\ntwo...\n"
    encoded_source_file(source, destination / "app.log").write_bytes(data)
    real_write = os.write
    calls = 0

    def fail_after_partial_write(fd: int, buffer: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, buffer[:4])
        raise OSError("injected write failure")

    with patch("logsync.sync.os.write", side_effect=fail_after_partial_write):
        failed_stats = sync_path(source, destination)

    assert failed_stats.errors == 1
    assert (destination / "app.log").read_bytes() == data[:4]
    assert encoded_source_file(source, destination / "app.log").read_bytes() == data

    assert sync_path(source, destination).errors == 0
    assert (destination / "app.log").read_bytes() == data[:4] + data


def test_retry_after_committed_fsync_failure_duplicates_range(
    tmp_path: Path,
) -> None:
    """Duplicate the range after a committed destination sync reports failure."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    data = b"one...\ntwo...\n"
    encoded_source_file(source, destination / "app.log").write_bytes(data)
    real_fsync = os.fsync
    failed = False

    def sync_then_fail(fd: int) -> None:
        nonlocal failed
        real_fsync(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path == destination / "app.log" and not failed:
            failed = True
            raise OSError("injected fsync failure")

    with patch("logsync.sync.os.fsync", side_effect=sync_then_fail):
        failed_stats = sync_path(source, destination)

    assert failed_stats.errors == 1
    assert (destination / "app.log").read_bytes() == data
    assert encoded_source_file(source, destination / "app.log").read_bytes() == data

    assert sync_path(source, destination).errors == 0
    assert (destination / "app.log").read_bytes() == data + data


def test_retry_after_punch_failure_duplicates_range(tmp_path: Path) -> None:
    """Duplicate the range after destination durability followed by a punch failure."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    data = b"one...\ntwo...\n"
    source_log = encoded_source_file(source, destination / "app.log")
    source_log.write_bytes(data)

    with patch("logsync.sync.punch_hole", side_effect=OSError("injected punch failure")):
        failed_stats = sync_path(source, destination)

    assert failed_stats.errors == 1
    assert (destination / "app.log").read_bytes() == data
    assert source_log.read_bytes() == data

    assert sync_path(source, destination).errors == 0
    assert (destination / "app.log").read_bytes() == data + data


def test_recreated_source_appends_as_new_generation(
    tmp_path: Path,
) -> None:
    """Append a recreated source file as a new generation."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    log_path = encoded_source_file(source, destination / "app.log")
    first = b"first...\n"
    second = b"second...\n"
    log_path.write_bytes(first)
    assert sync_path(source, destination).errors == 0

    log_path.write_bytes(second)
    assert sync_path(source, destination).errors == 0
    assert (destination / "app.log").read_bytes() == first + second


def _assert_destination_rotation_starts_replacement_at_zero(
    tmp_path: Path, rotation: str
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    log_path = encoded_source_file(source, destination / "app.log")
    first = b"first...\n"
    second = b"second...\n"
    log_path.write_bytes(first)
    assert sync_path(source, destination).errors == 0

    destination_log = destination / "app.log"
    if rotation == "rename":
        destination_log.rename(destination / "app.old.log")
    else:
        destination_log.unlink()
    with log_path.open("ab") as file:
        assert file.write(second) == len(second)

    assert sync_path(source, destination).errors == 0
    assert destination_log.read_bytes() == second
    if rotation == "rename":
        assert (destination / "app.old.log").read_bytes() == first


def test_renamed_destination_starts_replacement_at_zero(tmp_path: Path) -> None:
    """Write replacement data at offset zero after destination rotation by rename."""
    _assert_destination_rotation_starts_replacement_at_zero(tmp_path, "rename")


def test_removed_destination_starts_replacement_at_zero(tmp_path: Path) -> None:
    """Write replacement data at offset zero after destination removal."""
    _assert_destination_rotation_starts_replacement_at_zero(tmp_path, "remove")
