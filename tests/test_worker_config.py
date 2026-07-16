from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.config import ConfigError, load_worker_config


def write_worker_config(
    path: Path,
    tmp_path: Path,
    *,
    discovery: str = "",
    proxy: str = "",
    repositories: str = "",
) -> None:
    path.write_text(
        f"""
schema_version = 1
worker_id = "mac-mini"
poll_seconds = 60
heartbeat_seconds = 120
database_path = "{tmp_path / 'state.sqlite3'}"
cache_root = "{tmp_path / 'cache'}"
worktree_root = "{tmp_path / 'worktrees'}"
output_root = "{tmp_path / 'output'}"
codex_path = "/tmp/codex"
codex_home = "{tmp_path / 'codex-home'}"
github_app_id = "123"
github_installation_id = "456"
github_private_key_path = "{tmp_path / 'app.pem'}"
authorized_users = ["owner"]
{discovery}
{proxy}
{repositories}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_load_worker_config_parses_repositories_and_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    config_path.write_text(
        f"""
schema_version = 1
worker_id = "mac-mini"
poll_seconds = 60
heartbeat_seconds = 120
database_path = "{tmp_path / 'state.sqlite3'}"
cache_root = "{tmp_path / 'cache'}"
worktree_root = "{tmp_path / 'worktrees'}"
output_root = "{tmp_path / 'output'}"
codex_path = "/Applications/ChatGPT.app/Contents/Resources/codex"
codex_home = "{tmp_path / 'codex-home'}"
github_app_id = "123"
github_installation_id = "456"
github_private_key_path = "{tmp_path / 'app.pem'}"
authorized_users = ["owner"]

[[repositories]]
name = "owner/repo"
clone_url = "https://github.com/owner/repo.git"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_worker_config(config_path)

    assert config.worker_id == "mac-mini"
    assert config.repositories[0].name == "owner/repo"
    assert config.codex_home == tmp_path / "codex-home"
    assert config.poll_seconds == 60


def test_worker_config_rejects_duplicate_repositories(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    config_path.write_text(
        """
schema_version = 1
worker_id = "mac-mini"
poll_seconds = 60
heartbeat_seconds = 120
database_path = "/tmp/state.sqlite3"
cache_root = "/tmp/cache"
worktree_root = "/tmp/worktrees"
output_root = "/tmp/output"
codex_path = "/tmp/codex"
codex_home = "/tmp/codex-home"
github_app_id = "123"
github_installation_id = "456"
github_private_key_path = "/tmp/app.pem"
authorized_users = ["owner"]
[[repositories]]
name = "owner/repo"
clone_url = "url1"
[[repositories]]
name = "owner/repo"
clone_url = "url2"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate"):
        load_worker_config(config_path)


def test_worker_config_requires_numeric_github_identifiers(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'github_app_id = "123"', 'github_app_id = "worker-app"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="github_app_id"):
        load_worker_config(config_path)


def test_worker_config_allows_installation_discovery_without_static_repositories(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
    )

    config = load_worker_config(config_path)

    assert config.discover_installation_repositories is True
    assert config.repositories == ()


def test_worker_config_requires_one_repository_source(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(config_path, tmp_path)

    with pytest.raises(ConfigError, match="repository source"):
        load_worker_config(config_path)


def test_worker_config_rejects_non_boolean_discovery_flag(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery='discover_installation_repositories = "yes"',
    )

    with pytest.raises(ConfigError, match="discover_installation_repositories"):
        load_worker_config(config_path)


def test_worker_config_parses_safe_git_proxy(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
        proxy='git_proxy_url = "http://127.0.0.1:7897"',
    )

    config = load_worker_config(config_path)

    assert config.git_proxy_url == "http://127.0.0.1:7897"


def test_worker_config_treats_empty_git_proxy_as_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
        proxy='git_proxy_url = ""',
    )

    assert load_worker_config(config_path).git_proxy_url is None


@pytest.mark.parametrize(
    "proxy",
    [
        "socks5://127.0.0.1:7897",
        "http://user:secret@127.0.0.1:7897",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_worker_config_rejects_unsafe_git_proxy(tmp_path: Path, proxy: str) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
        proxy=f'git_proxy_url = "{proxy}"',
    )

    with pytest.raises(ConfigError, match="git_proxy_url"):
        load_worker_config(config_path)
