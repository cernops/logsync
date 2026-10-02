"""Perform destination operations under one user's HTCondor keyring."""

import argparse
import os
import pwd
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from logsync.afs import HTCondorKeyringCache
from tests.integration.profiles import PROFILES
from tests.integration.workload import WorkloadPhase, filename, phase_data


class Action(StrEnum):
    """Destination operations performed under a user's keyring."""

    PROBE = "probe"
    REMOVE = "remove"
    VERIFY = "verify"


def main() -> None:
    """Perform one requested operation under a user's keyring."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", type=Action, choices=tuple(Action))
    parser.add_argument("user")
    parser.add_argument("path", type=Path)
    parser.add_argument("--run-name")
    parser.add_argument("--profile", choices=sorted(PROFILES))
    parser.add_argument("--include-append", action="store_true")
    args = parser.parse_args()

    account = pwd.getpwnam(args.user)
    if account.pw_name != args.user:
        raise ValueError(f"user lookup did not return an exact match: {args.user}")
    HTCondorKeyringCache().use(account.pw_uid)

    if args.action is Action.PROBE:
        fd = os.open(args.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        os.close(fd)
    elif args.action is Action.REMOVE:
        if args.profile is None or args.run_name is None:
            parser.error("remove requires --profile and --run-name")
        for file_number in range(PROFILES[args.profile].file_count):
            (args.path / filename(args.run_name, file_number)).unlink(missing_ok=True)
        try:
            (args.path / args.run_name).rmdir()
        except FileNotFoundError:
            pass
    elif args.action is Action.VERIFY:
        if args.profile is None or args.run_name is None:
            parser.error("verify requires --profile and --run-name")
        profile = PROFILES[args.profile]
        _assert_ownership(args.path / args.run_name, account.pw_uid, account.pw_gid)
        for file_number in range(profile.file_count):
            expected = phase_data(profile, WorkloadPhase.INITIAL, file_number)
            if args.include_append:
                expected += phase_data(profile, WorkloadPhase.APPEND, file_number)
            path = args.path / filename(args.run_name, file_number)
            actual = path.read_bytes()
            assert actual == expected, (
                f"unexpected contents for {path}: "
                f"read {len(actual)} bytes, expected {len(expected)}"
            )
            _assert_ownership(path, account.pw_uid, account.pw_gid)
    else:
        assert_never(args.action)


def _assert_ownership(path: Path, uid: int, gid: int) -> None:
    metadata = path.stat()
    assert (metadata.st_uid, metadata.st_gid) == (uid, gid), (
        f"unexpected ownership for {path}: "
        f"{metadata.st_uid}:{metadata.st_gid}, expected {uid}:{gid}"
    )


if __name__ == "__main__":
    main()
