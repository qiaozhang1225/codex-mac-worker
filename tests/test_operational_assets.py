from pathlib import Path
import plistlib
import subprocess
import tomllib

from codex_mac_worker.cli import worker_main
from codex_mac_worker.config import load_worker_config


ROOT = Path(__file__).parents[1]


def test_worker_check_config_does_not_contact_github(tmp_path: Path, capsys) -> None:
    config = tmp_path / "worker.toml"
    key = tmp_path / "github-app.pem"
    key.write_text("placeholder", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.write_text("placeholder", encoding="utf-8")
    config.write_text(
        f'''schema_version = 1
worker_id = "test"
poll_seconds = 60
heartbeat_seconds = 120
database_path = "{tmp_path / 'state.sqlite3'}"
cache_root = "{tmp_path / 'cache'}"
worktree_root = "{tmp_path / 'worktrees'}"
output_root = "{tmp_path / 'outputs'}"
codex_path = "{codex}"
codex_home = "{tmp_path / 'codex-home'}"
github_app_id = "123"
github_installation_id = "456"
github_private_key_path = "{key}"
authorized_users = ["owner"]
[[repositories]]
name = "owner/repo"
clone_url = "https://github.com/owner/repo.git"
''',
        encoding="utf-8",
    )

    assert worker_main(["--config", str(config), "--check-config"]) == 0
    output = capsys.readouterr().out
    assert '"worker_id": "test"' in output
    assert '"discover_installation_repositories": false' in output


def test_templates_are_valid_and_example_config_loads(tmp_path: Path) -> None:
    example = (ROOT / "templates" / "worker.toml.example").read_text(encoding="utf-8")
    rendered = (
        example.replace("__HOME__", str(tmp_path))
        .replace("__OWNER__", "owner")
        .replace("REPLACE_WITH_APP_ID", "123")
        .replace("REPLACE_WITH_INSTALLATION_ID", "456")
    )
    config_path = tmp_path / "worker.toml"
    config_path.write_text(rendered, encoding="utf-8")
    config = load_worker_config(config_path)
    assert config.worker_id == "mac-mini-01"
    assert config.repositories[0].name == "owner/EaseWise"
    assert config.codex_home == tmp_path / "Library/Application Support/CodexWorker/codex-home"
    assert config.discover_installation_repositories is True

    permissions = tomllib.loads(
        (ROOT / "templates" / "codex-worker.config.toml").read_text(encoding="utf-8")
    )
    assert permissions["features"]["goals"] is False
    assert permissions["permissions"]["codex-worker"]["filesystem"]["~"] == "deny"
    assert permissions["permissions"]["codex-worker"]["filesystem"]["/opt/homebrew"] == "read"
    assert permissions["permissions"]["codex-worker"]["network"]["enabled"] is False

    for name in (
        "com.easewise.codex-worker.plist",
        "com.easewise.codex-worker-backup.plist",
    ):
        data = (ROOT / "templates" / name).read_bytes()
        plistlib.loads(data)


def test_shell_scripts_parse_and_docs_cover_manual_boundaries() -> None:
    scripts = (
        ROOT / "scripts" / "install_macos.sh",
        ROOT / "scripts" / "doctor_macos.sh",
        ROOT / "scripts" / "uninstall_macos.sh",
        ROOT / "scripts" / "bootstrap_repository.sh",
        ROOT / "scripts" / "install_macbook.sh",
    )
    for script in scripts:
        subprocess.run(["bash", "-n", script], check=True)

    setup = (ROOT / "docs" / "MAC_MINI_SETUP.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    macbook = (ROOT / "docs" / "MACBOOK_SETUP.md").read_text(encoding="utf-8")
    for required in ("FileVault", "pmset", "GitHub App", "Ruleset", "向日葵", "重启"):
        assert required in setup
    for required in ("pause", "resume", "retry", "revise", "cancel", "Draft PR"):
        assert required in operations
    assert "dispatch-codex-task" in macbook
    assert "codexctl" in macbook
    combined = "\n".join((setup, operations, macbook, (ROOT / "docs" / "SECURITY.md").read_text()))
    for required in (
        "discover_installation_repositories",
        "codexctl repo status",
        "codexctl repo onboard",
        "codexctl repo finalize",
        "awaiting-worker",
        "codexctl task review",
        "codexctl task merge",
        "--expected-head",
        "explicit",
        "future PR",
        "Goal",
    ):
        assert required in combined

    bootstrap = (ROOT / "scripts" / "bootstrap_repository.sh").read_text(encoding="utf-8")
    assert "codexctl repo status" in bootstrap
    assert "gh label create" not in bootstrap
