"""Deterministic workload generation shared across credential contexts."""

from enum import StrEnum
from pathlib import Path
from typing import assert_never

from tests.integration.profiles import WorkloadProfile


class WorkloadPhase(StrEnum):
    """Phases in the two-pass integration workload."""

    INITIAL = "initial"
    APPEND = "append"


def filename(run_name: str, file_number: int) -> Path:
    """Return the deterministic relative path for a workload file."""
    return Path(run_name) / f"{file_number:08d}.log"


def phase_data(profile: WorkloadProfile, phase: WorkloadPhase, file_number: int) -> bytes:
    """Build one file's records for a workload phase."""
    if phase is WorkloadPhase.INITIAL:
        count = profile.initial_records_per_file
    elif phase is WorkloadPhase.APPEND:
        count = profile.appended_records_per_file
    else:
        assert_never(phase)
    return b"".join(record(profile, phase, file_number, number) for number in range(count))


def record(
    profile: WorkloadProfile,
    phase: WorkloadPhase,
    file_number: int,
    record_number: int,
) -> bytes:
    """Build one fixed-size, complete, nonzero log record."""
    prefix = f"{phase}:{file_number:08d}:{record_number:08d}:".encode("ascii")
    suffix = b"...\n"
    padding = profile.record_bytes - len(prefix) - len(suffix)
    if padding < 0:
        raise ValueError("profile record size is too small for its record header")
    return prefix + b"x" * padding + suffix
