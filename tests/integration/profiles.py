"""Named workloads for the large-scale integration tests.

Keep profiles here so their size is visible and can be adjusted independently
of the test mechanics.
"""


from dataclasses import dataclass


@dataclass(frozen=True)
class WorkloadProfile:
    """Dimensions of a deterministic, per-user log workload."""

    file_count: int
    initial_records_per_file: int
    appended_records_per_file: int
    record_bytes: int


PROFILES = {
    "smoke": WorkloadProfile(10, 20, 5, 256),
    "standard": WorkloadProfile(1_000, 100, 25, 1_024),
    "large": WorkloadProfile(5_000, 200, 50, 2_048),
}
