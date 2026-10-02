"""Application-specific in-memory telemetry for logsync."""

import time

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from .errors import error_context
from .stats import SyncStats


_GLOBAL_PHASE = "logsync_global_phase"
_USER = "logsync_user"
_PHASE = "logsync_phase"
_OPERATION = "logsync_operation"
_ENTRIES = "logsync_entries"
_USER_ENTRIES = "logsync_user_entries"
_ENTRY_TYPES = ("active", "gc", "dir")


_MEASUREMENTS = {
    _GLOBAL_PHASE: ("phase",),
    _USER: ("user",),
    _PHASE: ("phase",),
    _OPERATION: ("filesystem", "operation"),
}


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One fully named Prometheus sample."""

    name: str
    metric_type: str
    help: str
    labels: Mapping[str, str]
    value: int | float


@dataclass(slots=True)
class _Totals:
    count: int = 0
    duration_ns: int = 0


class _Measurement:

    def __init__(self, record: Callable[[str, tuple[str, ...], str, int], None], name: str, labels: tuple[str, ...]) -> None:
        self._record: Callable[[str, tuple[str, ...], str, int], None] = record
        self._name: str = name
        self._labels: tuple[str, ...] = labels
        self._started_ns: int | None = None
        self._failed: bool = False

    def __enter__(self) -> Self:
        self._started_ns = time.perf_counter_ns()
        return self

    def fail(self) -> None:
        """Mark a handled failure as this measurement's outcome."""
        self._failed = True

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        del exc_value, traceback
        if self._started_ns is None:
            raise RuntimeError("measurement was not entered")
        outcome = "error" if exc_type is not None or self._failed else "success"
        self._record(self._name, self._labels, outcome, time.perf_counter_ns() - self._started_ns)


class _Metrics:

    def __init__(self) -> None:
        self._measurements: dict[tuple[str, tuple[str, ...], str], _Totals] = {}
        self._values: dict[str, tuple[int, int, int]] = {}
        self._staged_values: dict[str, tuple[int, int, int]] | None = None

    def global_phase(self, name: str) -> _Measurement:
        """Measure one global CLI phase."""
        return self._measure(_GLOBAL_PHASE, (name,))

    def user(self, user_name: str) -> _Measurement:
        """Measure one attempt to process a user."""
        return self._measure(_USER, (user_name,))

    @contextmanager
    def phase(self, user_name: str, name: str) -> Iterator[_Measurement]:
        """Measure one ordinary inclusive phase with per-user error context."""
        with error_context(f"{name} for {user_name!r} failed"):
            with self._measure(_PHASE, (name,)) as measurement:
                yield measurement

    @contextmanager
    def operation(self, filesystem: str, name: str) -> Iterator[_Measurement]:
        """Measure one low-level filesystem operation."""
        with error_context(f"{filesystem} {name} failed"):
            with self._measure(_OPERATION, (filesystem, name)) as measurement:
                yield measurement

    def _measure(self, name: str, labels: tuple[str, ...]) -> _Measurement:
        return _Measurement(self._record_measurement, name, labels)

    def record_sync(self, user_name: str, stats: SyncStats) -> None:
        """Record the latest synchronization values for one user."""
        values = self._staged_values if self._staged_values is not None else self._values
        values[user_name] = self._sync_values(stats)

    @contextmanager
    def inventory(self) -> Iterator[None]:
        """Atomically replace gauges with values recorded during one pass."""
        if self._staged_values is not None:
            raise RuntimeError("inventory staging is already active")
        self._staged_values = {}
        try:
            yield
            self._values = self._staged_values
        finally:
            self._staged_values = None

    def samples(self) -> tuple[MetricSample, ...]:
        """Return the current registry as fully named Prometheus samples."""
        result: list[MetricSample] = []
        for (name, label_values, outcome), totals in sorted(self._measurements.items()):
            labels = dict(zip(_MEASUREMENTS[name], label_values, strict=True))
            labels["outcome"] = outcome
            result.extend((
                MetricSample(f"{name}_count_total", "counter", "Completed measurements.", labels, totals.count),
                MetricSample(
                    f"{name}_duration_seconds_total",
                    "counter", "Total duration of completed measurements.",
                    labels, totals.duration_ns / 1_000_000_000
                ),
            ))
        entry_totals = dict.fromkeys(_ENTRY_TYPES, 0)
        for user_name, values in self._values.items():
            result.append(MetricSample(_USER_ENTRIES, "gauge", "Latest observed value.", {"user": user_name}, sum(values)))
            for entry_type, value in zip(_ENTRY_TYPES, values, strict=True):
                entry_totals[entry_type] += value
        for entry_type, value in entry_totals.items():
            result.append(MetricSample(_ENTRIES, "gauge", "Latest observed value.", {"type": entry_type}, value))
        result.sort(key=lambda sample: (sample.name, tuple(sample.labels.items())))
        return tuple(result)

    def _record_measurement(self, name: str, labels: tuple[str, ...], outcome: str, duration_ns: int) -> None:
        """Add one completed measurement to the registry."""
        totals = self._measurements.setdefault((name, labels, outcome), _Totals())
        totals.count += 1
        totals.duration_ns += duration_ns

    @staticmethod
    def _sync_values(stats: SyncStats) -> tuple[int, int, int]:
        return (
            stats.files_active,
            stats.files_gc,
            stats.directories_scanned,
        )

metrics = _Metrics()
"""Process-lifetime telemetry registry."""
