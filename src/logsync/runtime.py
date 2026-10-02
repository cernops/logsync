"""Application runtime independent of command-line parsing."""

import logging
import os
import pwd
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext

from .errors import format_error
from .metrics import metrics
from .afs import HTCondorKeyringCache
from .common import safe_user
from .config import Config
from .destination import get_user_anchor_path
from .identity import IdentityFatalError, as_daemon, as_user
from .metrics_file import MetricsFilePublisher

from .stats import SyncStats
from .sync import destination_path_for_source, sync_user


LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def run(config: Config) -> int:
    """Run synchronization passes according to a complete configuration."""
    configure_logging(config.verbose)
    _log_startup(config)
    keyring_cache = HTCondorKeyringCache() if config.htcondor_keyring else None
    if keyring_cache is not None:
        keyring_cache.use_neutral()
    metrics_publisher = (
        MetricsFilePublisher(config.metrics_file, config.metrics_interval)
        if config.metrics_file is not None
        else None
    )

    while True:
        t0 = time.monotonic()
        failed = False
        deferred = False
        with metrics.inventory():
            try:
                with metrics.global_phase("user_discovery"):
                    users = (
                        [config.user]
                        if config.user is not None
                        else discover_users(config.source_prefix)
                    )
            except OSError as exc:
                logging.error(f"failed to discover source users: {format_error(exc)}")
                logging.info("pass complete users=0 failed=true deferred=false")
                failed = True
                users = []
            with metrics.global_phase("user_processing") as meas:
                if not failed:
                    failed, deferred = _process_users(config, users, keyring_cache)
                    if failed:
                        meas.fail()
        if metrics_publisher is not None:
            try:
                with metrics.global_phase("metrics_publication"):
                    _ = metrics_publisher.publish_if_due()
            except OSError as exc:
                logging.error(f"failed to publish metrics to {config.metrics_file}: {format_error(exc)}")
        with metrics.global_phase("sleep"):
            if config.interval >= 0 and not deferred:
                dt = t0 + config.interval - time.monotonic()
                if dt > 0:
                    time.sleep(dt)
        if config.interval < 0:
            return 1 if failed else 0


def configure_logging(verbose: bool) -> None:
    """Configure process logging for the selected verbosity."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[handler],
        force=True,
    )


def _log_startup(config: Config) -> None:
    """Log the complete configuration and one illustrative path mapping."""
    logging.info(
        " ".join((
            "config",
            f"source_prefix={_log_value(config.source_prefix)}",
            f"dest_prefixes={_log_value(config.dest_prefixes)}",
            f"user_dir_sharding={_log_value(str(config.user_dir_sharding))}",
            f"user={_log_value(config.user)}",
            f"interval={_log_value(config.interval)}",
            f"max_user_seconds={_log_value(config.max_user_seconds)}",
            f"htcondor_keyring={_log_value(config.htcondor_keyring)}",
            f"no_chown={_log_value(config.no_chown)}",
            f"use_effective_identity={_log_value(config.use_effective_identity)}",
            f"keep_user_top_dirs={_log_value(config.keep_user_top_dirs)}",
            f"max_filename_bytes={_log_value(config.max_filename_bytes)}",
            f"chunk_size_bytes={_log_value(config.chunk_size_bytes)}",
            f"metrics_file={_log_value(config.metrics_file)}",
            f"metrics_interval={_log_value(config.metrics_interval)}",
            f"verbose={_log_value(config.verbose)}",
        ))
    )

    example_user = config.user if config.user is not None else "bob"
    source_user_path = config.source_prefix + example_user
    for destination_prefix in config.dest_prefixes:
        example_destination_user_path = get_user_anchor_path(config.user_dir_sharding, destination_prefix, example_user)
        example_destination = example_destination_user_path + "app.log"
        example_source = source_user_path + "/" + example_destination.lstrip("/")
        mapped_destination = destination_path_for_source(source_user_path, example_source)
        logging.info(
            " ".join((
                "path translation example",
                f"source={_log_value(example_source)}",
                f"destination={_log_value(mapped_destination)}",
            ))
        )


def _log_value(value: object) -> str:
    """Render one unambiguous value for a structured log field."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return repr(value)
    return str(value)


def _process_users(
    config: Config,
    users: list[str],
    keyring_cache: HTCondorKeyringCache | None,
) -> tuple[bool, bool]:
    """Process every requested user and report whether any failed or was deferred."""
    failed = False
    deferred = False
    active = False
    daemon_identity = (os.geteuid(), os.getegid(), tuple(os.getgroups())) if config.use_effective_identity else None
    for requested_user in users:
        try:
            with _neutral_keyring(keyring_cache), metrics.user(requested_user) as meas:
                with metrics.phase(requested_user, "identity_resolution"):
                    user, uid, gid = resolve_user(requested_user)
                    supplementary_groups = resolve_supplementary_groups(user, gid) if config.use_effective_identity else ()
                if config.htcondor_keyring:
                    assert keyring_cache is not None
                credential_setup = _make_credential_setup(keyring_cache, uid, daemon_identity)
                identity = as_user(uid, gid, supplementary_groups) if config.use_effective_identity else nullcontext()
                with identity:
                    stats = sync_user(
                        config.source_prefix + user,
                        config.dest_prefixes,
                        user,
                        owner_uid=uid,
                        owner_gid=gid,
                        user_dir_sharding=config.user_dir_sharding,
                        set_destination_ownership=not config.no_chown,
                        keep_user_top_dir=config.keep_user_top_dirs,
                        max_filename_bytes=config.max_filename_bytes,
                        chunk_size=config.chunk_size_bytes,
                        max_user_seconds=config.max_user_seconds,
                        credential_setup=credential_setup,
                    )
                with metrics.phase(user, "reporting"):
                    user_active = _stats_have_operations(stats)
                    if user_active or stats.errors or stats.deferred:
                        log_summary = logging.info
                    else:
                        log_summary = logging.debug
                    log_summary(" ".join((
                        f"user={user}",
                        f"files_active={stats.files_active}",
                        f"files_gc={stats.files_gc}",
                        f"dirs_scanned={stats.directories_scanned}",
                        f"quarantined={stats.quarantined}",
                        f"collected={stats.collected}",
                        f"dirs_removed={stats.directories_removed}",
                        f"errors={stats.errors}",
                        f"deferred={str(stats.deferred).lower()}",
                    )))
                if user_active:
                    active = True
                if stats.deferred:
                    deferred = True
                if stats.errors:
                    meas.fail()
                    failed = True
        except IdentityFatalError:
            raise
        except Exception as exc:
            logging.error(f"failed to sync user {requested_user!r}: {format_error(exc)}")
            failed = True
    if active or failed or deferred:
        log_summary = logging.info
    else:
        log_summary = logging.debug
    log_summary(f"pass complete users={len(users)} failed={str(failed).lower()} deferred={str(deferred).lower()}")
    return failed, deferred


@contextmanager
def _neutral_keyring(keyring_cache: HTCondorKeyringCache | None) -> Iterator[None]:
    """Keep user credentials out of source-only work and inter-user process state."""
    if keyring_cache is None:
        yield
        return
    keyring_cache.use_neutral()
    restoration_error: IdentityFatalError | None = None
    try:
        yield
    except IdentityFatalError as exc:
        restoration_error = exc
        try:
            keyring_cache.use_neutral()
        except Exception as cleanup_exc:
            exc.add_note(f"neutral keyring cleanup also failed: {format_error(cleanup_exc)}")
        raise
    finally:
        if restoration_error is None:
            keyring_cache.use_neutral()


def _make_credential_setup(
    keyring_cache: HTCondorKeyringCache | None,
    uid: int,
    daemon_identity: tuple[int, int, tuple[int, ...]] | None,
) -> Callable[[], None] | None:
    """Return lazy keyring setup that restores the daemon identity when needed."""
    if keyring_cache is None:
        return None

    def setup() -> None:
        identity = as_daemon(*daemon_identity) if daemon_identity is not None else nullcontext()
        with identity:
            keyring_cache.use(uid)

    return setup


def _stats_have_operations(stats: SyncStats) -> bool:
    """Return whether a user pass changed the source tree."""
    return bool(stats.quarantined or stats.collected or stats.directories_removed)


def discover_users(source_prefix: str) -> list[str]:
    """Return source user directory names in stable order."""
    users: list[str] = []
    anchor, _, dir_prefix = source_prefix.rpartition("/")
    with metrics.operation("source", "scandir"):
        entries_context = os.scandir(anchor)
    with entries_context as entries:
        for entry in entries:
            if not entry.name.startswith(dir_prefix):
                continue
            user = entry.name[len(dir_prefix):]
            try:
                if not entry.is_dir(follow_symlinks=False):
                    raise ValueError(f"source entry {entry.path!r} is not a regular directory")
                users.append(safe_user(user))
            except (OSError, ValueError) as exc:
                logging.error(f"failed to inspect source user entry {entry.path}: {format_error(exc)}")
    return sorted(users)


def resolve_user(value: str) -> tuple[str, int, int]:
    """Resolve an exact account name to its ownership identifiers."""
    user = safe_user(value)
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise ValueError(f"unknown user: {user}") from exc
    if account.pw_name != user:
        raise ValueError(f"user lookup did not return an exact match: {user}")
    return user, account.pw_uid, account.pw_gid


def resolve_supplementary_groups(user: str, primary_gid: int) -> tuple[int, ...]:
    """Resolve supplementary groups without duplicating the primary GID."""
    return tuple(sorted(set(os.getgrouplist(user, primary_gid)) - {primary_gid}))
