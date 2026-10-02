# Tests

`unittests/` contains the established unittest-style suite; `pytests/` contains newer pytest-style tests.
Pytest collects both suites so they share the setup in `tests/conftest.py`; `make test` runs them as separate pytest invocations.
Every test starts with the same pseudorandom seed, and the Makefile fixes Python's hash seed for reproducible collection and iteration order without allowing one test's random consumption to affect another.
Critical coverage will move gradually to pytest-style tests without broad duplication.

`root/` contains focused Linux behavior tests that require effective UID 0 but do not require HTCondor, AFS, services, dedicated filesystems, or other production-like infrastructure.
`make test-root` runs the suite directly and therefore requires root.
`make test-podman-root` runs it in Podman's rootless user namespace.
Rootless Podman must be installed and configured with subordinate UID and GID ranges that cover the identities exercised by the tests.
The root-suite pytest setup runs every test function in an isolated forked child process, so tests contain ordinary Python code while a failure cannot leave the pytest runner under a changed identity.
Identity-changing operations belong in test functions because pytest fixture setup and teardown run in the parent process.

Production path interfaces use strings. Tests may use `pathlib` to arrange
temporary files, but convert paths to strings when calling production APIs.

The test filesystem must support every exercised feature, including sparse files and hole punching.
Lack of support is a test failure.
Test fallbacks for unsupported features separately and explicitly.

## Large-Scale Integration Test

Numbered integration tests run in explicit order against the real CLI, HTCondor keyrings, and production-type filesystems.

The suite is excluded from `make`, `make all`, and `make check`.
It requires root, real HTCondor-managed user keyrings, an XFS source filesystem with sparse-file and hole-punch support, and a production-type destination filesystem.
Run it with `make test-integration`.
Helpers print subprocess commands to stderr;
default pytest capture hides successful output,
while a direct invocation with `-s` shows commands and child output.

Set these environment variables before invoking it:

- `LOGSYNC_INTEGRATION_SOURCE_PREFIX`: absolute, dedicated source directory prefix ending in `/`.
  The test creates missing `<prefix><user>` directories and assigns them to the corresponding user before workload generation.
- `LOGSYNC_INTEGRATION_DEST_PREFIX`: absolute destination directory prefix ending in `/`,
  with existing destination per-user directories accessible using the corresponding AFS tokens.
- `LOGSYNC_INTEGRATION_USERS`: comma-separated exact local usernames.
  Every user must have an HTCondor-managed keyring and an existing destination directory under the destination prefix.
- `LOGSYNC_INTEGRATION_PROFILE`: optional profile name; defaults to `standard`.
- `LOGSYNC_INTEGRATION_KEEP_FAILED`: set to `1` to preserve a failed run for diagnosis.
  Successful runs are always removed.
- `LOGSYNC_INTEGRATION_USER_DIR_SHARDING`: optional `none` (the default) or `first-letter`.
  Destination per-user directories must exist as `<prefix><user>` or `<prefix><first-letter>/<user>`, respectively.
  Source user directories remain unsharded at `<source-prefix><user>`; generated logs encode the complete destination path, including any shard.

Example setup and invocation for users `alice` and `bob`:

```bash
export LOGSYNC_INTEGRATION_SOURCE_PREFIX=/logsync/
export LOGSYNC_INTEGRATION_DEST_PREFIX=/shared/user-
export LOGSYNC_INTEGRATION_USERS=alice,bob
export LOGSYNC_INTEGRATION_PROFILE=standard
#export LOGSYNC_INTEGRATION_KEEP_FAILED=1 # optional, to retain generated files upon failure
export LOGSYNC_INTEGRATION_USER_DIR_SHARDING=first-letter

sudo --preserve-env make test-integration
```

The prefix directories must be distinct and non-overlapping below their filesystem roots.
The test creates only `tmp-logsync-integration/<eight-digit-number>.log` files and confines cleanup to that directory.
It rejects incomplete setup, including unavailable user keyrings, before creating workload data.
The configured roots must not be used by concurrent integration test runs.
Generated source workload files and their containing integration directory are owned by the target user.

Workload profiles are defined in `integration/profiles.py`.

All generated records are fixed-size, deterministic, contain no zero bytes, and end in `...\n`.
Destination content mismatches report the affected path and actual and expected byte counts.
The integration support and keyring helper modules contain only test configuration, generation, credential-context verification, and cleanup logic.
Future failure and race scenarios should remain separate from the normal-operation test.

## Test descriptions

Each test function or method has a docstring describing the behavior it verifies.
Test modules also carry docstrings describing their broader theme.
