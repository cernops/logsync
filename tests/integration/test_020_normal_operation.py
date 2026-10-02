"""Large-scale normal operation on real source and destination filesystems."""

import os
from pathlib import Path

from tests.integration.profiles import WorkloadProfile
from tests.integration.support import (
    IntegrationConfig,
    load_config,
    new_run_name,
    prepare_source_user_dirs,
    probe_user_keyring,
    require_hole_punch,
    remove_run,
    require_success,
    run_logsync,
    source_log_dir,
    user_ids,
    verify_destination,
)
from tests.integration.workload import WorkloadPhase, filename, phase_data


def test_two_pass_multi_user_sync() -> None:
    """Drain and collect two verified multi-user batches with ownership and credentials intact."""
    config = load_config()
    run_name = new_run_name()
    passed = False
    try:
        prepare_source_user_dirs(config)

        for user in config.users:
            probe_user_keyring(config, user)

        for user in config.users:
            uid, gid = user_ids(user)
            require_hole_punch(config.source_root / user)
            source_dir = source_log_dir(config, user)
            source_dir.mkdir(parents=True, exist_ok=True)
            _append_phase(
                source_dir,
                run_name,
                config.profile,
                WorkloadPhase.INITIAL,
                owner_uid=uid,
                owner_gid=gid,
            )

        _run_sync_pass(config)
        _run_sync_pass(config)
        _verify_pass(config, run_name, include_append=False)

        for user in config.users:
            uid, gid = user_ids(user)
            _append_phase(
                source_log_dir(config, user),
                run_name,
                config.profile,
                WorkloadPhase.APPEND,
                owner_uid=uid,
                owner_gid=gid,
            )

        _run_sync_pass(config)
        _run_sync_pass(config)
        _verify_pass(config, run_name, include_append=True)
        passed = True
    finally:
        if passed or not config.keep_failed:
            remove_run(config, run_name)
        else:
            print(f"preserved failed integration run: {run_name}")


def _run_sync_pass(config: IntegrationConfig) -> None:
    for user in config.users:
        result = run_logsync(
            config,
            "--user",              user,
            "--htcondor-keyring",
            "--use-effective-identity",
            "--no-chown",
        )
        require_success(result, f"logsync pass for {user}")


def _append_phase(
    source_dir: Path,
    run_name: str,
    profile: WorkloadProfile,
    phase: WorkloadPhase,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
    run_dir = source_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chown(run_dir, owner_uid, owner_gid, follow_symlinks=False)
    for file_number in range(profile.file_count):
        path = source_dir / filename(run_name, file_number)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchown(fd, owner_uid, owner_gid)
            _write_all(fd, phase_data(profile, phase, file_number))
            os.fsync(fd)
        finally:
            os.close(fd)


def _verify_pass(config: IntegrationConfig, run_name: str, *, include_append: bool) -> None:
    for user in config.users:
        source_dir = source_log_dir(config, user)
        verify_destination(config, user, run_name, include_append=include_append)
        for file_number in range(config.profile.file_count):
            path = source_dir / filename(run_name, file_number)
            assert not path.exists()
            assert not path.with_name(f"logsync-gc-{path.name}").exists()


def _write_all(fd: int, data: bytes) -> None:
    position = 0
    while position < len(data):
        position += os.write(fd, data[position:])
