"""Configuration and lifecycle support for real-filesystem tests."""

import os
import pwd
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from logsync.destination import UserDirSharding
from logsync.linux import punch_hole
from tests.integration.profiles import PROFILES, WorkloadProfile
from tests.integration.workload import filename


@dataclass(frozen=True)
class IntegrationConfig:
    """Validated integration-test configuration."""

    source_root: Path
    destination_root: Path
    users: tuple[str, ...]
    profile: WorkloadProfile
    profile_name: str
    keep_failed: bool
    user_dir_sharding: UserDirSharding


def load_config() -> IntegrationConfig:
    """Load and validate the integration environment."""
    if os.geteuid() != 0:
        raise RuntimeError("large-scale integration tests must run as root")

    source_root = _required_dir_prefix("LOGSYNC_INTEGRATION_SOURCE_PREFIX")
    destination_root = _required_dir_prefix("LOGSYNC_INTEGRATION_DEST_PREFIX")
    if source_root == destination_root or source_root in destination_root.parents or destination_root in source_root.parents:
        raise RuntimeError("integration source and destination roots must not overlap")

    raw_users = _required_env("LOGSYNC_INTEGRATION_USERS")
    users = tuple(part.strip() for part in raw_users.split(",") if part.strip())
    if not users or len(users) != len(set(users)):
        raise RuntimeError("LOGSYNC_INTEGRATION_USERS must contain unique usernames")
    for user in users:
        if "/" in user or user in {".", ".."}:
            raise RuntimeError(f"invalid integration username: {user}")
        user_ids(user)
    profile_name = os.environ.get("LOGSYNC_INTEGRATION_PROFILE", "standard")
    try:
        profile = PROFILES[profile_name]
    except KeyError as exc:
        names = ", ".join(sorted(PROFILES))
        raise RuntimeError(f"unknown integration profile {profile_name!r}; choose from {names}") from exc

    keep_failed = os.environ.get("LOGSYNC_INTEGRATION_KEEP_FAILED", "0") == "1"
    user_dir_sharding_value = os.environ.get("LOGSYNC_INTEGRATION_USER_DIR_SHARDING", "none")
    try:
        user_dir_sharding = UserDirSharding(user_dir_sharding_value)
    except ValueError as exc:
        raise RuntimeError(
            "LOGSYNC_INTEGRATION_USER_DIR_SHARDING must be 'none' or 'first-letter'"
        ) from exc
    return IntegrationConfig(
        source_root,
        destination_root,
        users,
        profile,
        profile_name,
        keep_failed,
        user_dir_sharding,
    )


def new_run_name() -> str:
    """Return the recognizable integration-test directory name."""
    return "tmp-logsync-integration"


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a project Python module with the current source tree."""
    environment = os.environ.copy()
    source_path = str(Path(__file__).resolve().parents[2] / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source_path if not existing else f"{source_path}:{existing}"
    command = [sys.executable, *args]
    print(f"+ {shlex.join(command)}", file=sys.stderr, flush=True)
    return subprocess.run(
        command,
        check=False,
        text=True,
        env=environment,
    )


def run_logsync(config: IntegrationConfig, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the logsync CLI against the integration filesystem configuration."""
    return run_command(
        "-m",
        "logsync.cli",
        "--source-prefix",     f"{config.source_root}/",
        "--dest-prefix",       f"{config.destination_root}/",
        "--user-dir-sharding", config.user_dir_sharding,
        *args,
    )


def require_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    """Raise an informative assertion for a failed subprocess."""
    assert result.returncode == 0, f"{operation} failed with exit code {result.returncode}"


def user_ids(user: str) -> tuple[int, int]:
    """Return exact system UID and GID for an integration user."""
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RuntimeError(f"unknown integration user: {user}") from exc
    if account.pw_name != user:
        raise RuntimeError(f"user lookup did not return an exact match: {user}")
    return account.pw_uid, account.pw_gid


def prepare_source_user_dirs(config: IntegrationConfig) -> None:
    """Create source user directories and assign them to their target users."""
    for user in config.users:
        uid, gid = user_ids(user)
        source_user = config.source_root / user
        try:
            source_user.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = source_user.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"source user path is not a directory: {source_user}")
        os.chown(source_user, uid, gid, follow_symlinks=False)


def probe_user_keyring(config: IntegrationConfig, user: str) -> None:
    """Require a usable keyring and access to the destination user directory."""
    result = run_command(
        "-m",
        "tests.integration.keyring_helper",
        "probe",
        user,
        str(destination_user_dir(config, user)),
    )
    require_success(result, f"keyring probe for {user}")


def require_hole_punch(directory: Path) -> None:
    """Require successful source hole punching inside the generated run."""
    probe = directory / "logsync-integration-hole-probe"
    fd = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        data = b"nonzero probe...\n"
        if os.write(fd, data) != len(data):
            raise OSError("short write while preparing hole-punch probe")
        punch_hole(fd, 0, len(data))
        if os.pread(fd, len(data), 0) != b"\0" * len(data):
            raise OSError("hole-punch probe did not read back as zeros")
    finally:
        os.close(fd)
        probe.unlink(missing_ok=True)


def remove_run(config: IntegrationConfig, run_name: str) -> None:
    """Remove only files generated for this run."""
    for user in config.users:
        source_path = source_log_dir(config, user)
        for file_number in range(config.profile.file_count):
            path = source_path / filename(run_name, file_number)
            path.unlink(missing_ok=True)
        try:
            (source_path / run_name).rmdir()
        except FileNotFoundError:
            pass

        result = run_command(
            "-m",
            "tests.integration.keyring_helper",
            "remove",
            user,
            str(destination_user_dir(config, user)),
            "--profile",
            config.profile_name,
            "--run-name",
            run_name,
        )
        require_success(result, f"destination cleanup for {user}")


def verify_destination(
    config: IntegrationConfig,
    user: str,
    run_name: str,
    *,
    include_append: bool,
) -> None:
    """Verify destination bytes while using the user's AFS credentials."""
    arguments = [
        "-m",
        "tests.integration.keyring_helper",
        "verify",
        user,
        str(destination_user_dir(config, user)),
        "--profile",
        config.profile_name,
        "--run-name",
        run_name,
    ]
    if include_append:
        arguments.append("--include-append")
    result = run_command(*arguments)
    require_success(result, f"destination verification for {user}")


def destination_user_dir(config: IntegrationConfig, user: str) -> Path:
    """Return the configured pre-existing destination directory for a user."""
    if config.user_dir_sharding is UserDirSharding.FIRST_LETTER:
        return config.destination_root / user[0] / user
    return config.destination_root / user


def source_log_dir(config: IntegrationConfig, user: str) -> Path:
    """Return the source directory encoding the user's destination directory."""
    return (config.source_root / user).joinpath(*destination_user_dir(config, user).parts[1:])


def _required_dir_prefix(variable: str) -> Path:
    raw_value = _required_env(variable)
    if not raw_value.endswith("/"):
        raise RuntimeError(f"{variable} must end with '/'")
    value = Path(raw_value)
    if not value.is_absolute():
        raise RuntimeError(f"{variable} must be an absolute path")
    resolved = value.resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError(f"{variable} must name a directory")
    return resolved


def _required_env(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise RuntimeError(f"{variable} is required")
    return value
