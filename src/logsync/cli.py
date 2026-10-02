"""Command-line interface for logsync."""

import argparse
import logging
import os
import sys
from typing import Protocol, cast

from .common import DEFAULT_CHUNK_SIZE, safe_user
from .config import Config
from .destination import UserDirSharding
from .runtime import run
from .signals import ShutdownRequested, install_shutdown_handlers


class _Args(Protocol):
    source_prefix: str
    dest_prefixes: list[str]
    user: str | None
    interval: float
    max_user_seconds: float
    max_filename_bytes: int | None
    chunk_size_bytes: int
    verbose: bool
    htcondor_keyring: bool
    no_chown: bool
    use_effective_identity: bool
    keep_user_top_dirs: bool
    user_dir_sharding: UserDirSharding
    metrics_file: str | None
    metrics_interval: float


def absolute_path_prefix(value: str) -> str:
    """Parse an absolute path prefix ending in '/', '-' or '_'."""
    if not value or value[-1] not in "/-_":
        raise argparse.ArgumentTypeError("must end with '/', '-' or '_'")
    if not os.path.isabs(value):
        raise argparse.ArgumentTypeError("must be an absolute path")
    return value


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface and return its exit status."""
    parser = argparse.ArgumentParser(
        prog="logsync",
        description=(
            "Copy complete log records from recursive source user trees to "
            "source-encoded absolute destinations, validate each destination "
            "against its per-user directory, and punch copied source ranges."
        ),
    )
    _ = parser.add_argument(
        "--source-prefix",
        type=absolute_path_prefix,
        required=True,
        help="absolute source path prefix, for example '/logsync/' or '/logsync/u-'",
    )
    _ = parser.add_argument(
        "--dest-prefix",
        dest="dest_prefixes",
        type=absolute_path_prefix,
        action="append",
        required=True,
        help=(
            "absolute prefix used to validate destination paths, "
            "for example '/shared/users/' or '/shared/u-'; "
            "(repeat for multiple destinations)"
        ),
    )
    _ = parser.add_argument(
        "--user-dir-sharding",
        type=UserDirSharding,
        choices=tuple(UserDirSharding),
        default=UserDirSharding.NONE,
        help="layout used to validate destination per-user directories; default is none",
    )
    _ = parser.add_argument(
        "--user",
        help="process only this user; default processes every source user",
    )
    _ = parser.add_argument(
        "--interval",
        type=float,
        default=-1.0,
        help="start a pass every N seconds; default runs once",
    )
    _ = parser.add_argument(
        "--max-user-seconds",
        type=positive_float,
        default=1.0,
        help="soft limit on seconds spent synchronizing one user; default is 1",
    )
    _ = parser.add_argument(
        "--htcondor-keyring",
        action="store_true",
        help="use the user's HTCondor-managed AFS credentials",
    )
    _ = parser.add_argument(
        "--no-chown",
        action="store_true",
        help="do not assign newly created destination files and directories to the user",
    )
    _ = parser.add_argument(
        "--use-effective-identity",
        action="store_true",
        help="use the target user's effective UID, GID, and groups for the complete user synchronization pass",
    )
    _ = parser.add_argument(
        "--keep-user-top-dirs",
        action="store_true",
        help="retain empty per-user top-level source directories",
    )
    _ = parser.add_argument(
        "--max-filename-bytes",
        type=int,
        help="reject source filenames longer than this many bytes",
    )
    _ = parser.add_argument(
        "--chunk-size-bytes",
        type=positive_int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"source read and destination write size; default is {DEFAULT_CHUNK_SIZE}",
    )
    _ = parser.add_argument(
        "--metrics-file",
        help="atomically publish Prometheus metrics to this file",
    )
    _ = parser.add_argument(
        "--metrics-interval",
        type=positive_float,
        default=15.0,
        help="minimum seconds between metrics file updates; default is 15",
    )
    _ = parser.add_argument("-v", "--verbose", action="store_true")
    args = cast(_Args, cast(object, parser.parse_args(argv)))

    try:
        if args.user is not None:
            _ = safe_user(args.user)
    except ValueError as exc:
        parser.error(str(exc))

    config = Config(
        source_prefix=args.source_prefix,
        dest_prefixes=args.dest_prefixes,
        user_dir_sharding=args.user_dir_sharding,
        user=args.user,
        interval=args.interval,
        max_user_seconds=args.max_user_seconds,
        htcondor_keyring=args.htcondor_keyring,
        no_chown=args.no_chown,
        use_effective_identity=args.use_effective_identity,
        keep_user_top_dirs=args.keep_user_top_dirs,
        max_filename_bytes=args.max_filename_bytes,
        chunk_size_bytes=args.chunk_size_bytes,
        metrics_file=args.metrics_file,
        metrics_interval=args.metrics_interval,
        verbose=args.verbose,
    )
    try:
        install_shutdown_handlers()
        return run(config)
    except ShutdownRequested as exc:
        logging.info(f"received {exc.received.name}, exiting")
        return 0


def positive_int(value: str) -> int:
    """Parse a positive command-line integer."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_float(value: str) -> float:
    """Parse a positive finite command-line number."""
    parsed = float(value)
    if parsed <= 0 or not float("-inf") < parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


if __name__ == "__main__":
    sys.exit(main())
