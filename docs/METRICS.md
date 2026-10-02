# Metrics reference

Logsync records work performed by the command-line synchronization loop.
Metrics are kept in memory for the lifetime of the process and can be written in Prometheus text format for collection by a textfile collector.

## Data model

Every measurement records its elapsed time and is assigned an `outcome` label (`success` or `error`),
based on whether or not the measured work failed.

Counters are cumulative from process startup.
Synchronization values describe the latest complete CLI pass: entry types are summed across users, while total entry counts remain per user.
Each completed pass atomically replaces the full gauge set, so users absent from that pass are removed.
The three aggregate entry types remain present and record zero when a pass has no corresponding entries.
Direct `sync_user()` calls replace that user's contribution to the aggregate gauges and its per-user entry gauge.

Synchronization results do not depend on telemetry. Each `sync_user()` call returns its completed statistics directly and records the same values in the process-wide registry.

The public `metrics` singleton uses a monotonic clock and is designed for logsync's single-threaded execution. Restarting the process resets all metrics.

## Metric families

The table lists the exported Prometheus family names. Measurement families add the suffixes described below.

| Metric                 | Labels in addition to `outcome` | Meaning                                          |
| ---------------------- | ------------------------------- | ------------------------------------------------ |
| `logsync_global_phase` | `phase`                         | One measured CLI lifecycle phase.                |
| `logsync_user`         | `user`                          | One attempt to process a user during a CLI pass. |
| `logsync_phase`        | `phase`                         | One measured part of a user attempt.             |
| `logsync_operation`    | `filesystem`, `operation`       | One low-level source or destination operation.   |

The following value metric families do not have an `outcome` label:

| Metric                 | Labels | Meaning                                                                                          |
| ---------------------- | ------ | ------------------------------------------------------------------------------------------------ |
| `logsync_entries`      | `type` | Source filesystem inodes: active files (`active`), GC files (`gc`), scanned directories (`dir`). |
| `logsync_user_entries` | `user` | Sum of `logsync_entries` but by user.                                                            |

### Prometheus series

Each populated metric family produces the following series for every label
combination:

| Suffix                    | Type    | Meaning                                       |
| ------------------------- | ------- | --------------------------------------------- |
| `_count_total`            | counter | Completed measurements since process startup. |
| `_duration_seconds_total` | counter | Total elapsed time of completed measurements. |

Each value metric produces a gauge from the latest recorded user contributions.
`logsync_entries` always has one series for each of its three types, including zero values; `logsync_user_entries` has one series per user in the latest pass.

## Time accounting

CLI timing uses ordinary measurements. The two active phases jointly represent synchronization work.
User processing is nested naturally, while publication and sleep are recorded separately:

```text
CLI synchronization work
├── global phase: user_discovery
└── global phase: user_processing
    └── logsync_user
        └── phases aggregated across users
            └── filesystem operations

Outside active synchronization work:
├── global phase: metrics_publication
└── global phase: sleep
```

The sum of `user_discovery` and `user_processing` durations approximates active pass time; it excludes only the small amount of bookkeeping between and around those measurements.
User phases occur inside a user timer. Publication and scheduled sleep are separate global phases.
Durations are inclusive diagnostic observations: measurements can be nested, repeated, or overlap, and unmeasured bookkeeping can occur between them.
Phase and operation durations therefore do not form exact partitions of user durations.

The global `user_processing` phase contains the user scopes as well as inter-user iteration, aggregate reporting, and error handling.
It therefore does not equal the sum of all user durations.

`logsync_operation` remains an inclusive diagnostic metric. Operations occur inside phases, so adding operation durations to phase durations double-counts time.

Publication and scheduled sleep are recorded separately from active pass work.
A publication includes the completed active pass but cannot include its own duration; that publication measurement is visible in a later snapshot.
When any user's remaining files are deferred by the soft processing limit, the recurring loop skips its scheduled sleep and no `sleep` measurement is recorded for that pass.

When calculating time:

- Use `rate()` or `increase()` with `_duration_seconds_total`. The raw counter
  is cumulative from process startup.
- Include both `outcome="success"` and `outcome="error"` to account for all
  completed attempts.
- Compare the same observation interval.
- Do not expect phase durations to add up exactly to user durations.
- Allow for counter resets when the logsync process restarts.

Outcomes also do not have to match throughout the hierarchy. A child operation can record `error` when its exception is caught and the enclosing file continues successfully.
A user or `user_processing` measurement is explicitly marked as an error when its handled aggregate result reports a failure.
Outcomes describe the measured context itself; they are not automatically inherited from child measurements.

## Cardinality

Assume `N` distinct values of the `user` label. The table below gives the
maximum cardinality after every currently instrumented label combination has
occurred with both outcomes.
“Label sets” counts distinct combinations of all labels, including `outcome`;
“Prometheus series” counts the individual suffixed time series exported for
those combinations.

| Metric family          |     Label sets | Prometheus series |
| ---------------------- | -------------: | ----------------: |
| `logsync_global_phase` |              8 |                16 |
| `logsync_user`         |           `2N` |              `4N` |
| `logsync_phase`        |             18 |                36 |
| `logsync_operation`    |             54 |               108 |
| Value metrics          |        `3 + N` |           `3 + N` |
| **Total**              |  **`83 + 3N`** |    **`163 + 5N`** |

The factors are:

- Every label combination has up to two `outcome` values: `success` and `error`.
- There are four global phases, so `logsync_global_phase` has eight label sets.
- There are nine phases, so `logsync_phase` has `9 phases × 2 outcomes = 18` label sets.
- There are 27 valid operation pairs: 15 source and 12 destination.
  Therefore `logsync_operation` has `27 pairs × 2 outcomes = 54` label sets.

Each measurement label set exports two series: measurement count and duration. Thus the operation total is:

```text
54 × 2 = 108
```

The three entry types produce three aggregate gauges. `logsync_user_entries` produces one gauge per user.

Measurement label sets that have never occurred and users absent from the latest pass are omitted. The three aggregate entry label sets are always present.
Cumulative measurement series remain in the process registry.
Latest value series are replaced when a CLI pass completes, and process-lifetime cardinality resets when the process restarts.

## Phase labels

The `logsync_global_phase` family currently uses:

| Phase                 | Meaning                                                         |
| --------------------- | --------------------------------------------------------------- |
| `user_discovery`      | Discover the user directories selected for a CLI pass.          |
| `user_processing`     | Process all selected users and perform aggregate reporting.     |
| `metrics_publication` | Decide whether a snapshot is due and publish it when necessary. |
| `sleep`               | Wait until the next scheduled recurring pass.                   |

The `logsync_phase` family currently uses:

| Phase                 | Meaning                                                                                                           |
| --------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `identity_resolution` | Validate a requested username and resolve its UID and GID.                                                        |
| `credential_setup`    | On first required destination access, select the isolated HTCondor AFS session.                                   |
| `source_setup`        | Resolve and validate the source directory and effective filename limit.                                           |
| `source_scan`         | Build the complete snapshot of unique logical source paths and prune already-empty subdirectories.                |
| `file_analysis`       | Open a source file, locate its data offset and final complete-record boundary.                                    |
| `destination_setup`   | Open or create the confined destination.                                                                          |
| `data_transfer`       | Copy complete records, synchronize storage, and punch the source range.                                           |
| `collection`          | Lease a drained source generation, rename active data to quarantine, or unlink a quarantine from an earlier pass. |
| `reporting`           | Report the completed user's synchronization statistics.                                                           |

`credential_setup` is present only when HTCondor keyring support is requested and the pass requires destination access.
Other phases appear only when the corresponding work is needed.

## Operation labels

The `filesystem` label identifies the role of the object being accessed:

- `source` is the source log tree.
- `destination` is the confined destination tree.

The currently instrumented operations are listed below. Not every operation
appears during every pass.

### Source operations

| Operation           | Meaning                                                             |
| ------------------- | ------------------------------------------------------------------- |
| `resolve_directory` | Resolve the source directory to a canonical path.                   |
| `scandir`           | Open a source-user discovery or source-tree directory scan.         |
| `inspect_entry`     | Inspect the type and metadata of a directory entry.                 |
| `rmdir`             | Remove an already-empty source subdirectory.                        |
| `rename`            | Rename a drained active source file for collection by a later pass. |
| `open_file`         | Open a source file.                                                 |
| `close`             | Close a source file descriptor.                                     |
| `seek_data`         | Find the next data extent. End-of-data is treated as success.       |
| `pread_nonzero`     | Read past allocated NUL padding while locating the first live byte. |
| `pread_records`     | Read data while finding complete record boundaries.                 |
| `pread_copy`        | Read data for transfer to the destination.                          |
| `lease`             | Acquire, verify, or release a source-file lease.                     |
| `punch_hole`        | Remove transferred data from the sparse source file.                |
| `fsync`             | Synchronize source-file changes to storage.                         |
| `unlink`            | Remove a collected source file.                                     |

### Destination operations

| Operation               | Meaning                                                 |
| ----------------------- | ------------------------------------------------------- |
| `realpath`              | Canonicalize and validate a requested destination path. |
| `open_directory`        | Open the filesystem root or a destination directory component. |
| `close`                 | Close a destination file or directory descriptor.       |
| `mkdir`                 | Create a missing destination directory.                 |
| `chown_directory`       | Set ownership on a destination directory.               |
| `chmod_directory`       | Set permissions on a destination directory.             |
| `open_file`             | Open an existing destination file.                      |
| `create_file`           | Create a missing destination file.                      |
| `chown_file`            | Set ownership on a destination file.                    |
| `chmod_file`            | Set permissions on a destination file.                  |
| `write`                 | Append transferred data.                                |
| `fsync`                 | Synchronize destination-file changes to storage.        |

## Publishing

Metrics publication is intended for recurring runs (non-negative `--interval`).
Use `--metrics-file` and optionally `--metrics-interval` to publish snapshots:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --interval 5 \
  --metrics-file /run/logsync/metrics.prom --metrics-interval 15
```

The output file is cleared when the publisher starts, preventing metrics from a previous process from being mistaken for current values and validating that the destination is writable.
After each active pass, a new snapshot is written when the configured minimum interval has elapsed.
The snapshot includes the completed pass; the publication phase itself is first visible in a later snapshot.
Writes use a temporary file in the same directory followed by an atomic replacement, so the parent directory must already exist.

A startup publication failure is fatal. A later publication failure is logged
without changing the synchronization result; recurring execution retries
publication after a later pass.

The output includes Prometheus `HELP` and `TYPE` metadata. Cumulative series are declared as counters, while latest values are gauges.

## Example queries

Passes that ended in an error (the two failure paths are mutually exclusive for handled failures):

```promql
sum(increase(logsync_global_phase_count_total{
  phase=~"user_discovery|user_processing",
  outcome="error"
}[15m]))
```

Measured share of active synchronization time by global phase:

```promql
sum by (phase) (
  rate(logsync_global_phase_duration_seconds_total{
    phase=~"user_discovery|user_processing"
  }[5m])
)
/
scalar(sum(rate(logsync_global_phase_duration_seconds_total{
  phase=~"user_discovery|user_processing"
}[5m])))
```

Phase share of completed processing time:

```promql
sum by (phase) (logsync_phase_duration_seconds_total)
/
scalar(sum(logsync_global_phase_duration_seconds_total{phase="user_processing"}))
```

Operation error fraction over five minutes:

```promql
(
  sum(increase(logsync_operation_count_total{outcome="error"}[5m]))
  or
  0 * sum(increase(logsync_operation_count_total[5m]))
)
/
sum(increase(logsync_operation_count_total[5m]))
```

Average successful operation duration over five minutes:

```promql
sum by (filesystem, operation) (
  rate(logsync_operation_duration_seconds_total{outcome="success"}[5m])
)
/
sum by (filesystem, operation) (
  rate(logsync_operation_count_total{outcome="success"}[5m])
)
```

Latest active files across all users:

```promql
logsync_entries{type="active"}
```

Latest source inode usage by user:

```promql
logsync_user_entries
```
