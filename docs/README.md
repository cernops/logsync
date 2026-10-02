# logsync

`logsync` drains append-only log files whose path below the source user directory encodes their absolute destination:

```text
/logsync/<user>/shared/users/<user>/test.log
```

maps to:

```text
/shared/users/<user>/test.log
```

Source user dirs are always unsharded.
With `--user-dir-sharding first-letter`, the source must encode the sharded destination instead:

```text
/logsync/<user>/shared/users/<first-letter-of-user>/<user>/test.log
```

Logsync drains active files under their normal names while producers may
continue to open and append to them. After complete records are durably copied,
logsync punches their source byte ranges. Only when an active file is fully
drained and a Linux write lease proves that no producer has it open does
logsync atomically rename it, for example from `app.log` to
`logsync-gc-app.log`. The quarantine remains until a later synchronization
pass leases and unlinks it. Parent directories are retained.

## Durability model

Logsync copies only complete records ending in `...\n`.
It leaves an incomplete tail in place for a later pass.

For each active or quarantined source file with complete records, it:

1. appends all complete ranges to the destination;
2. `fsync()`s the destination file once;
3. punches the source prefix through the final copied boundary with `FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE`;
4. `fsync()`s the source file once.

`SIGINT` is deferred while this sequence is in progress and is handled after
the source sync completes. `SIGTERM` may still stop an in-progress transfer.

Destination paths are opened only when a source file has at least one complete
record. Empty and fully drained files can therefore be leased and collected
without destination access. Incomplete-only files remain for a later pass.

Physical quarantine names never affect mapping: `logsync-gc-app.log` maps to
destination `app.log`. Destination files are always opened with `O_APPEND`.
The punched source prefix is the only record of consumed bytes.

Only one quarantine generation exists per logical pathname. If both `app.log`
and `logsync-gc-app.log` exist, the quarantine is processed first and the
active generation is then appended after it.
Successfully opening the quarantine suppresses collection of the active
generation for that logical-file attempt, even when the quarantine is removed;
a later outer pass may quarantine the active file. Missing physical
counterparts and files that disappear between scanning and opening are ignored
as normal races.

Collection requires a granted `F_WRLCK` Linux file lease. `EAGAIN` means another descriptor remains open, so collection is retried normally.
Other lease errors are handled like ordinary per-user synchronization errors.
While holding the lease, logsync re-scans the inode through EOF and verifies that the lease remained unbroken.
Collection uses one pathname operation per outer pass.
After verifying the lease remained unbroken, logsync renames a drained active file to quarantine or unlinks a drained quarantine.
During source scanning, logsync also removes subdirectories that are already empty.
Removal is not recursive, so a nested empty hierarchy is pruned by one level per pass.
Empty per-user top-level source directories are also removed unless `--keep-user-top-dirs` is set.

The normal no-error path is lossless and duplicate-free. Failure recovery is
at least once because destination append and source punching are separate
durable operations. A partial write can leave a fragment before the complete
range appended by a retry. A destination `fsync()` that commits but reports
failure, a punch failure, or a crash after destination durability but before
durable punching can duplicate the complete range. Logsync never truncates,
overwrites, compares, or repairs destination contents, and it never punches
source bytes until destination `fsync()` succeeds.

## Requirements

- Modern Linux with Python 3.11 or newer
- A single source mount containing only regular files and directories and supporting atomic `RENAME_NOREPLACE`, sparse punching, and Linux file leases
- Source-file ownership matching logsync's filesystem identity, or `CAP_LEASE`
- Only one instance of Logsync is invoked and through its CLI only
- Producers append complete atomic records ending with `...\n` through the normal pathname opened with `O_CREAT | O_WRONLY | O_APPEND`
- Producers do not rename, unlink, move, or replace source files
- Source records do not contain NUL bytes
- Source filenames leave room for the `logsync-gc-` prefix and do not use the reserved `logsync-` prefix
- Each destination (top-level, per-user) directory, already exists and is trusted
- Without `--no-chown`: privilege to assign newly created destination objects to the user's UID and primary GID
- With `--use-effective-identity`: root or EUID/EGID-changing capability and full access by users to their source trees,
  including permission to delete their top-level source dir if `--keep-user-top-dirs` is not specified
- With `--htcondor-keyring`: access to root's keyring and operation alongside the htcondor schedd

## Usage

`--source-prefix` and `--dest-prefix` are required absolute prefixes ending in `/`, `-`, or `_`.
The source prefix identifies per-user source trees.
Removing `<source-prefix><user>/` from a source file gives its absolute destination path.
For example, `/logsync/alice/shared/users/alice/test.log` maps to `/shared/users/alice/test.log`.

Destination prefixes do not construct or modify that path.
Each defines a per-user directory within which the resolved destination may resolve.
Pass `--dest-prefix` multiple times to allow multiple destination trees; prefixes are checked in command-line order and the first match is used.
A trailing slash places the username below that directory; `-` or `_` makes the final component a username prefix.
For example, `/shared/users/` permits Alice only below `/shared/users/alice`, while `/shared/u-` permits her only below `/shared/u-alice`.

Run once:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice
```

Allow both primary and archive destination trees:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --dest-prefix /archive/users/ --user alice
```

Use the target user's effective UID and GID for the complete user synchronization pass, and rely on creation identity instead of post-creation chown:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice --use-effective-identity --no-chown
```

Use pre-provisioned first-letter-sharded destination user directories:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice --user-dir-sharding first-letter
```

`--user-dir-sharding` accepts `none` (the default) or `first-letter`.
It changes only the expected destination layout; logsync never inserts the shard.
Source paths remain below `<source-prefix><user>` in both modes.
With `--dest-prefix /shared/users/`, first-letter mode requires source-encoded paths to resolve below `/shared/users/a/alice` for Alice.

Omit `--user` to discover users matching the source prefix.
For prefixes ending in `-` or `_`, only matching entries are considered, with the prefix stripped:

```sh
logsync --source-prefix /logsync/u- --dest-prefix /shared/u-
```

A failure while setting up or syncing one user is logged and stops work for that user during the current pass.
Other users are still processed.

By default, each user's synchronization has a soft one-second limit. Set a different positive finite duration with `--max-user-seconds`:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --max-user-seconds 5
```

Source setup and the complete source scan count toward this limit.
Logical files are shuffled before each user pass so that one failing or continuously active file does not consistently starve the others.
Logsync checks the deadline before each logical file, so it finishes a file already in progress and then stops before starting another.
Deferring remaining files is reported as `deferred=true`, is not an error, and does not prevent later users from being processed.

Run repeatedly:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice --interval 5
```

## systemd deployment

[`examples/logsync.service`](../examples/logsync.service) is a starting point for a long-running installation.
Adjust its paths and install it as `/etc/systemd/system/logsync.service`.
`After=` orders logsync after `condor.service` when both units are started, but does not start Condor or tie the services' lifecycles together.

The RPM does not install, enable, or start `logsync.service`.
However, if a unit named `logsync.service` is running, updating the RPM restarts it so that it uses the updated logsync code.
An absent or inactive service is not started, and removing the RPM does not restart it.

The service runs as root to share `condor_master`'s Unix identity, link HTCondor-managed user keyrings, and assign destination ownership.
Check the `logsync` path on the target host.

With `--interval 5`, logsync aims to start a pass every five seconds in the same process.
A user-level failure marks that pass failed, but the interval loop continues with the next pass.
If any user's remaining files are deferred, logsync publishes due metrics but skips that pass's scheduled sleep so the next pass can continue the remaining work immediately.
Without `--interval`, it performs one pass and exits with status 0 or 1.

The example service restarts logsync after an error exit or unclean signal, but not after a clean exit or clean signal such as `SIGTERM`.
Logsync handles `SIGINT` and `SIGTERM`, logs the requested shutdown, and exits
successfully. A signal is briefly deferred while a new destination object is
assigned its final ownership and permissions or while the metrics file is
atomically published.

## AFS / HTCondor and authentication

For a destination on AFS, use the credentials already managed by HTCondor:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice \
  --htcondor-keyring --use-effective-identity --no-chown
```

This requires `libkeyutils` and access to the process user's keyring.
The process must have the same real UID and user namespace as `condor_master`, normally root.
When a user pass first needs destination access, logsync creates or rejoins an isolated named session for that UID and links the `htcondor_uid<uid>` anchor.
Passes that only scan or collect source files do not select credentials.
With `--use-effective-identity`, the pass performs source access, lease handling, destination access, and collection with the target effective UID, primary GID, and supplementary groups;
keyring setup briefly restores the complete daemon identity so it can access root's user keyring, then returns to the target identity before destination or lease operations continue.
Logsync caches the selection within one `run()` invocation and refreshes it after a 15-minute soft TTL.
A failed refresh falls back to the stale session and is retried on the next pass that needs destination access.
Logsync neither renews nor removes HTCondor's anchor.

A missing anchor or AFS access failure stops that user for the current pass; other users continue.
Destination access is never retried without the selected credentials.

By default, newly created destination directories and files are assigned with `fchown()`.
This requires local ownership privileges and, on AFS, AFS administrator rights that local root does not imply.

For AFS, combine `--use-effective-identity --no-chown` when the complete synchronization pass should use the user's effective UID and primary GID and creation ownership should be left to the filesystem.
The selected AFS token remains the AFS identity, and logsync does not detect the filesystem.
Source files must be owned by the target UID for that user to acquire Linux file leases without `CAP_LEASE`.

Impose a lower source filename limit:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice --max-filename-bytes 128
```

The configured byte limit must be positive and below the maximum that leaves room for `logsync-gc-` on the source filesystem.
It is validated at startup.

Change the source read and destination write chunk size from its 1 MiB default:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --user alice --chunk-size-bytes 4194304
```

The chunk size must be positive. Larger values trade bigger temporary buffers for fewer syscalls.

## Performance metrics

Logsync always keeps in-memory metrics for global phases, users, source inventory, phases, and source and destination operations.
User attempts and per-user source-entry gauges include a username, while phase, operation, and entry-type metrics are aggregated across users. No series includes a path or filename.
Measurements record cumulative counts and elapsed time.
Successes and failures use separate series.

Measurement counters and durations are cumulative from process startup.
The sum of the `user_discovery` and `user_processing` phase durations approximates active pass time.
User timers and aggregate phase timers are inclusive diagnostic observations and do not form exact partitions of user durations.
Metrics publication and scheduled sleep are recorded separately from active pass work.

Inventory gauges describe the latest complete CLI pass.
`logsync_entries` reports active files, GC files, and scanned directories summed across users, while `logsync_user_entries` reports their combined total for each user.
Completing a pass atomically replaces the gauge set and removes users that were not present in that pass.
The three aggregate `logsync_entries` series remain present with zero values when a pass has no corresponding entries.
Exact per-pass quarantined, collected, and removed-directory values remain in completion logs and returned synchronization statistics.
The related filesystem operation counters are cumulative diagnostics rather than exact replacements for those values.
The registry uses a monotonic clock, is reset when the process restarts, and is single-threaded.

See [METRICS.md](METRICS.md) for the complete metric, label, operation, and Prometheus series reference.

For a recurring run, publish Prometheus text to a file:

```sh
logsync --source-prefix /logsync/ --dest-prefix /shared/users/ --interval 5 \
  --metrics-file /run/logsync/metrics.prom --metrics-interval 15
```

At startup, logsync atomically replaces the metrics file with an empty file.
This clears stale metrics and validates that the destination can be published before user discovery or synchronization begins; a failure is fatal.
After each active pass, logsync publishes when the configured minimum interval has elapsed.
The snapshot includes the completed pass but cannot include its own completed publication phase, which appears in a later publication.
A later publication failure is logged without changing the synchronization result and is retried during the next recurring pass.

One-shot runs publish their completed synchronization pass and user values. Their publication phase is not observable because there is no later snapshot.

The systemd example creates `/run/logsync` with `RuntimeDirectory=`.
This is normally memory-backed and is removed when the service stops, so the metrics file does not outlive the process.
It can be read directly or collected with node exporter's textfile collector.

For local testing with explicit roots:

```sh
python -m logsync.cli --source-prefix /tmp/logsync/ --dest-prefix /tmp/destination/ --user alice
```

This requires the pre-existing `/tmp/destination/alice` dir and source logs below `/tmp/logsync/alice/tmp/destination/alice/`.
The username must exactly match a system account.
With `--user-dir-sharding first-letter`, the permitted destination becomes `/tmp/destination/a/alice`, which must also be encoded in the source path.

## Development

For code changes, run the full local validation:

```sh
make check
```

This runs the unit tests, `pylint`, and `pyright`.
For documentation-only changes, `git diff --check` is enough.
