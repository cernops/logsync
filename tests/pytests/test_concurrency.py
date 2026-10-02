"""Races between synchronization and external log writers."""

import os
from pathlib import Path
from unittest.mock import patch

from tests.pytests.support import encoded_source_file, sync_path


def test_append_after_copy_boundary_is_copied_on_next_pass(
    tmp_path: Path,
) -> None:
    """Preserve a boundary-crossing append and copy it once on the next pass."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    log_path = encoded_source_file(source, destination / "app.log")
    initial = b"initial...\n"
    appended = b"appended...\n"
    log_path.write_bytes(initial)
    append_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)

    real_write = os.write
    appended_during_sync = False

    def append_before_first_write(fd: int, data: bytes) -> int:
        nonlocal appended_during_sync
        if not appended_during_sync:
            assert real_write(append_fd, appended) == len(appended)
            appended_during_sync = True
        return real_write(fd, data)

    try:
        with patch("logsync.sync.os.write", side_effect=append_before_first_write):
            first_stats = sync_path(source, destination)
    finally:
        os.close(append_fd)

    assert appended_during_sync
    assert first_stats.errors == 0
    assert (destination / "app.log").read_bytes() == initial
    quarantine = log_path.with_name("logsync-gc-app.log")
    assert log_path.read_bytes() == b"\0" * len(initial) + appended
    assert not quarantine.exists()

    second_stats = sync_path(source, destination)

    assert second_stats.errors == 0
    assert (destination / "app.log").read_bytes() == initial + appended
    assert not log_path.exists()
    assert quarantine.exists()

    third_stats = sync_path(source, destination)
    assert third_stats.collected == 1
    assert not quarantine.exists()


def test_partial_record_is_completed_and_copied_on_next_pass(
    tmp_path: Path,
) -> None:
    """Retain a partial record and copy it once after a later append completes it."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    log_path = encoded_source_file(source, destination / "app.log")
    log_path.write_bytes(b"complete...\npartial")
    append_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)

    first_stats = sync_path(source, destination)

    assert first_stats.errors == 0
    assert (destination / "app.log").read_bytes() == b"complete...\n"

    try:
        assert os.write(append_fd, b"...\n") == len(b"...\n")
    finally:
        os.close(append_fd)

    second_stats = sync_path(source, destination)
    third_stats = sync_path(source, destination)

    assert second_stats.errors == 0
    assert third_stats.errors == 0
    assert (destination / "app.log").read_bytes() == b"complete...\npartial...\n"
