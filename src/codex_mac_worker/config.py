from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any
from urllib.parse import urlsplit

from .merge_policy import MANUAL, MERGE_MODES


class ConfigError(ValueError):
    """Raised when worker or project configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    default_base_branch: str
    worker_github_app_id: int
    allowed_risk_levels: tuple[str, ...]
    protected_paths: tuple[str, ...]
    max_changed_files: int
    max_diff_lines: int
    codex_attempt_timeout_minutes: int
    task_hard_timeout_minutes: int
    max_automatic_attempts: int
    verification: dict[str, tuple[str, ...]]
    preparation: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    name: str
    clone_url: str


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    worker_id: str
    poll_seconds: int
    heartbeat_seconds: int
    database_path: Path
    cache_root: Path
    worktree_root: Path
    output_root: Path
    codex_path: Path
    github_app_id: str
    github_installation_id: str
    github_private_key_path: Path
    authorized_users: tuple[str, ...]
    repositories: tuple[RepositoryConfig, ...]
    codex_home: Path | None = None
    discover_installation_repositories: bool = False
    git_proxy_url: str | None = None
    merge_mode: str = MANUAL


def _positive_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return value


def _capped_positive_int(raw: dict[str, Any], key: str, maximum: int) -> int:
    value = _positive_int(raw, key)
    if value > maximum:
        raise ConfigError(f"{key} cannot exceed {maximum}")
    return value


def _strings(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty string list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{key} must be a non-empty string list")
    return tuple(item.strip() for item in value)


def load_project_config(path: Path) -> ProjectConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"unable to read project config: {exc}") from exc
    return parse_project_config(text)


def parse_project_config(text: str) -> ProjectConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"unable to read project config: {exc}") from exc
    schema_version = raw.get("schema_version")
    if schema_version == 1:
        raise ConfigError(
            "project schema_version 1 must be migrated to 2 and define "
            "worker_github_app_id; re-dispatch retained v1 tasks after migration"
        )
    if schema_version != 2:
        raise ConfigError("project schema_version must be 2")

    verification_raw = raw.get("verification")
    if not isinstance(verification_raw, dict) or not verification_raw:
        raise ConfigError("verification must define at least one profile")
    verification: dict[str, tuple[str, ...]] = {}
    for name, profile in verification_raw.items():
        if not isinstance(profile, dict):
            raise ConfigError(f"verification.{name} must be a table")
        verification[name] = _strings(profile, "commands")

    preparation: tuple[str, ...] = ()
    preparation_raw = raw.get("preparation")
    if preparation_raw is not None:
        if not isinstance(preparation_raw, dict):
            raise ConfigError("preparation must be a table")
        preparation = _strings(preparation_raw, "commands")

    base = raw.get("default_base_branch")
    if not isinstance(base, str) or not base.strip():
        raise ConfigError("default_base_branch must be a non-empty string")
    return ProjectConfig(
        default_base_branch=base.strip(),
        worker_github_app_id=_positive_int(raw, "worker_github_app_id"),
        allowed_risk_levels=_strings(raw, "allowed_risk_levels"),
        protected_paths=_strings(raw, "protected_paths"),
        max_changed_files=_positive_int(raw, "max_changed_files"),
        max_diff_lines=_positive_int(raw, "max_diff_lines"),
        codex_attempt_timeout_minutes=_capped_positive_int(
            raw, "codex_attempt_timeout_minutes", 45
        ),
        task_hard_timeout_minutes=_capped_positive_int(raw, "task_hard_timeout_minutes", 120),
        max_automatic_attempts=_capped_positive_int(raw, "max_automatic_attempts", 2),
        verification=verification,
        preparation=preparation,
    )


def _worker_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _worker_numeric_string(raw: dict[str, Any], key: str) -> str:
    value = _worker_string(raw, key)
    if not value.isdigit() or int(value) <= 0:
        raise ConfigError(f"{key} must be a positive numeric identifier")
    return value


def _worker_proxy_url(raw: dict[str, Any]) -> str | None:
    value = raw.get("git_proxy_url")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("git_proxy_url must be an HTTP(S) URL without credentials")
    value = value.strip()
    if not value:
        return None
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError("git_proxy_url must contain a valid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("git_proxy_url must be an HTTP(S) URL without credentials")
    return value.rstrip("/")


def load_worker_config(path: Path) -> WorkerConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"unable to read worker config: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    discover_installation_repositories = raw.get(
        "discover_installation_repositories",
        False,
    )
    if not isinstance(discover_installation_repositories, bool):
        raise ConfigError("discover_installation_repositories must be a boolean")
    merge_mode = raw.get("merge_mode", MANUAL)
    if merge_mode not in MERGE_MODES:
        raise ConfigError("merge_mode must be 'manual' or 'automatic'")
    repositories_raw = raw.get("repositories", [])
    if not isinstance(repositories_raw, list):
        raise ConfigError("repositories must be an array of tables")
    if not repositories_raw and not discover_installation_repositories:
        raise ConfigError(
            "at least one repository source is required: static repositories or installation discovery"
        )
    repositories: list[RepositoryConfig] = []
    seen: set[str] = set()
    for item in repositories_raw:
        if not isinstance(item, dict):
            raise ConfigError("repository entries must be tables")
        name = _worker_string(item, "name")
        if name in seen:
            raise ConfigError(f"duplicate repository: {name}")
        seen.add(name)
        repositories.append(RepositoryConfig(name, _worker_string(item, "clone_url")))

    def worker_path(key: str) -> Path:
        return Path(_worker_string(raw, key)).expanduser()

    return WorkerConfig(
        worker_id=_worker_string(raw, "worker_id"),
        poll_seconds=_positive_int(raw, "poll_seconds"),
        heartbeat_seconds=_positive_int(raw, "heartbeat_seconds"),
        database_path=worker_path("database_path"),
        cache_root=worker_path("cache_root"),
        worktree_root=worker_path("worktree_root"),
        output_root=worker_path("output_root"),
        codex_path=worker_path("codex_path"),
        github_app_id=_worker_numeric_string(raw, "github_app_id"),
        github_installation_id=_worker_numeric_string(raw, "github_installation_id"),
        github_private_key_path=worker_path("github_private_key_path"),
        authorized_users=_strings(raw, "authorized_users"),
        repositories=tuple(repositories),
        codex_home=worker_path("codex_home"),
        discover_installation_repositories=discover_installation_repositories,
        git_proxy_url=_worker_proxy_url(raw),
        merge_mode=merge_mode,
    )
