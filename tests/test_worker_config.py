from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.config import ConfigError, load_worker_config


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
