"""Application configuration independent of its input source."""

from dataclasses import dataclass

from .common import DEFAULT_CHUNK_SIZE
from .destination import UserDirSharding


@dataclass(frozen=True, slots=True)
class Config:
    """Complete validated configuration for one logsync process."""

    source_prefix: str
    dest_prefixes: list[str]
    user_dir_sharding: UserDirSharding = UserDirSharding.NONE
    user: str | None = None
    interval: float = -1.0
    max_user_seconds: float = 1.0
    htcondor_keyring: bool = False
    no_chown: bool = False
    use_effective_identity: bool = False
    keep_user_top_dirs: bool = False
    max_filename_bytes: int | None = None
    chunk_size_bytes: int = DEFAULT_CHUNK_SIZE
    metrics_file: str | None = None
    metrics_interval: float = 15.0
    verbose: bool = False
