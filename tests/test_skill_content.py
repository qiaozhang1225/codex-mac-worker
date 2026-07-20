from __future__ import annotations

from pathlib import Path
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "dual-mac-collaboration"
REQUIRED_REFERENCES = (
    "roles-and-delegation.md",
    "issue-protocol.md",
    "checkpoints.md",
    "git-delivery.md",
    "scheduled-execution.md",
)
REQUIRED_SCRIPTS = (
    "config_validate.py",
    "issue_validate.py",
    "issue_create.py",
    "issue_checkpoint.py",
    "issue_complete.py",
    "git_preflight.py",
    "git_deliver.py",
    "scheduled_pick.py",
)
FORBIDDEN_ACTIVE = (
    "worker_github_app" + "_id",
    "readiness attestation",
    "merge_" + "mode",
    "approval fingerprint",
)


def skill_parts() -> tuple[dict[str, object], str]:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match is not None
    metadata = yaml.safe_load(match.group(1))
    assert isinstance(metadata, dict)
    return metadata, match.group(2)


def test_frontmatter_has_only_name_and_use_when_description() -> None:
    metadata, _ = skill_parts()

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "dual-mac-collaboration"
    assert isinstance(metadata["description"], str)
    assert metadata["description"].startswith("Use when")


def test_skill_routes_to_all_required_references() -> None:
    _, body = skill_parts()

    for name in REQUIRED_REFERENCES:
        assert f"references/{name}" in body
        assert (SKILL_ROOT / "references" / name).is_file()


def test_skill_routes_scheduled_runs_to_scheduled_reference() -> None:
    _, body = skill_parts()

    assert "references/scheduled-execution.md" in body
    assert "Codex App Scheduled" in body
    assert "Never use this skill to start background execution" not in body
    assert "Goal" in body


def test_scheduled_execution_requires_frozen_repository_context() -> None:
    _, body = skill_parts()
    scheduled = (
        SKILL_ROOT / "references" / "scheduled-execution.md"
    ).read_text(encoding="utf-8")

    for text in (body, scheduled):
        assert "AGENTS.md" in text
        assert "every Issue-declared context file" in text
        assert "frozen context commit" in text
        assert "blocked" in text


def test_scheduled_docs_distinguish_clean_noop_from_maintenance() -> None:
    texts = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (SKILL_ROOT / "references" / "scheduled-execution.md").read_text(
            encoding="utf-8"
        ),
        (SKILL_ROOT / "assets" / "scheduled-slot-prompt.md").read_text(
            encoding="utf-8"
        ),
    ]

    for text in texts:
        normalized = text.casefold()
        assert "clean no-candidate no-op" in normalized
        assert "maintenance-only" in normalized
        assert "stop without code execution" in normalized
        assert "report" in normalized and "maintenance" in normalized
        assert "maintenance_actions" in text
        assert "outcome" in text


def test_references_do_not_chain_to_other_references() -> None:
    for name in REQUIRED_REFERENCES:
        text = (SKILL_ROOT / "references" / name).read_text(encoding="utf-8")
        assert "references/" not in text
        assert re.search(r"\]\([^)]*\.md(?:#[^)]*)?\)", text) is None


def test_skill_discovers_every_public_script_via_help() -> None:
    _, body = skill_parts()

    for name in REQUIRED_SCRIPTS:
        assert f"scripts/{name} --help" in body


def test_skill_contains_required_hard_boundaries() -> None:
    _, body = skill_parts()

    required_phrases = (
        "explicit user confirmation",
        "Issue body is the only current task contract",
        "continue without MacBook approval",
        "Never force push",
        "product decision",
        "visible Codex App",
        "direct-main",
        "task-branch",
    )
    for phrase in required_phrases:
        assert phrase in body


def test_skill_tree_has_no_legacy_requirements_or_template_markers() -> None:
    texts = [
        path.read_text(encoding="utf-8")
        for path in [SKILL_ROOT / "SKILL.md", *(SKILL_ROOT / "references").glob("*.md")]
        if path.exists()
    ]
    joined = "\n".join(texts)

    for term in FORBIDDEN_ACTIVE:
        assert term not in joined
    assert "TODO" not in joined
    assert len((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()) < 500


def test_openai_metadata_names_the_skill() -> None:
    metadata = yaml.safe_load(
        (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )

    assert metadata["interface"]["display_name"] == "双 Mac Codex 协作"
    assert "GitHub Issue" in metadata["interface"]["short_description"]
    assert "$dual-mac-collaboration" in metadata["interface"]["default_prompt"]


def test_scheduled_prompt_has_required_boundaries() -> None:
    prompt = (SKILL_ROOT / "assets" / "scheduled-slot-prompt.md").read_text(
        encoding="utf-8"
    )

    for phrase in (
        "$dual-mac-collaboration",
        "duomac-scheduled-pick",
        "one Issue",
        "no-op",
        "Never deploy",
        "Never force push",
        "Do not use Goal",
    ):
        assert phrase in prompt


def _fake_install_tools(tmp_path: Path, head: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "install-tools.log"
    python = bin_dir / "python3.12"
    python.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DUOMAC_TEST_LOG"
if [ "$1" = "--version" ]; then
  echo 'Python 3.12.9'
elif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
elif [ "${1##*/}" = "validate_skill.py" ]; then
  exec "$DUOMAC_REAL_PYTHON" "$@"
fi
""",
        encoding="utf-8",
    )
    python.chmod(0o755)
    git = bin_dir / "git"
    git.write_text(
        f"#!/bin/sh\nif [ \"$1 $2\" = \"rev-parse HEAD\" ]; then echo '{head}'; else echo 'git version 2.50.0'; fi\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)
    return bin_dir, log


def installed_test_environment(
    tmp_path: Path, head: str = "d" * 40
) -> tuple[dict[str, str], Path]:
    bin_dir, log = _fake_install_tools(tmp_path, head)
    app_root = tmp_path / "app"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "DUOMAC_APP_ROOT": str(app_root),
        "DUOMAC_SKILLS_ROOT": str(tmp_path / "skills"),
        "DUOMAC_BIN_ROOT": str(tmp_path / "bin"),
        "DUOMAC_TEST_LOG": str(log),
        "DUOMAC_REAL_PYTHON": sys.executable,
    }
    return env, app_root


def run_installer(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/zsh", str(ROOT / "scripts/install_skill.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_copies_skill_and_records_source_commit(tmp_path: Path) -> None:
    head = "d" * 40
    env, app_root = installed_test_environment(tmp_path, head)
    skills_root = Path(env["DUOMAC_SKILLS_ROOT"])
    wrapper_root = Path(env["DUOMAC_BIN_ROOT"])
    log = Path(env["DUOMAC_TEST_LOG"])

    result = run_installer(env)

    assert result.returncode == 0, result.stderr
    installed = skills_root / "dual-mac-collaboration"
    assert (installed / "SKILL.md").is_file()
    assert (installed / ".source-commit").read_text(encoding="utf-8").strip() == head
    assert "-m pip install PyYAML>=6,<7" in log.read_text(encoding="utf-8")
    wrapper_names = (
        "duomac-config-validate",
        "duomac-issue-validate",
        "duomac-issue-create",
        "duomac-issue-checkpoint",
        "duomac-issue-complete",
        "duomac-git-preflight",
        "duomac-git-deliver",
        "duomac-scheduled-pick",
    )
    for name in wrapper_names:
        wrapper = wrapper_root / name
        assert wrapper.is_file()
        assert os.access(wrapper, os.X_OK)
    assert (app_root / "repositories.toml.example").is_file()


def test_installer_preserves_existing_repository_config(tmp_path: Path) -> None:
    env, app_root = installed_test_environment(tmp_path)
    config = app_root / "repositories.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_installer(env)

    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    assert (app_root / "repositories.toml.example").is_file()


def test_repo_skill_validator_accepts_source() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_skill.py"),
            "--skill-root",
            str(SKILL_ROOT),
            *[
                argument
                for name in REQUIRED_SCRIPTS
                for argument in ("--wrapper-target", name)
            ],
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_installer_rejects_invalid_staged_skill_before_activation(
    tmp_path: Path,
) -> None:
    env, _ = installed_test_environment(tmp_path)
    skills_root = Path(env["DUOMAC_SKILLS_ROOT"])
    installed = skills_root / "dual-mac-collaboration"
    installed.mkdir(parents=True)
    sentinel = installed / "sentinel.txt"
    sentinel.write_text("existing install\n", encoding="utf-8")

    source_root = tmp_path / "source"
    (source_root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "install_skill.sh", source_root / "scripts")
    if (ROOT / "scripts" / "validate_skill.py").exists():
        shutil.copy2(ROOT / "scripts" / "validate_skill.py", source_root / "scripts")
    shutil.copytree(
        SKILL_ROOT,
        source_root / "skills" / "dual-mac-collaboration",
    )
    invalid_metadata = (
        source_root
        / "skills"
        / "dual-mac-collaboration"
        / "agents"
        / "openai.yaml"
    )
    invalid_metadata.write_text("interface: {}\n", encoding="utf-8")

    result = subprocess.run(
        ["/bin/zsh", str(source_root / "scripts" / "install_skill.sh")],
        cwd=source_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "existing install\n"
    assert {path.name for path in installed.iterdir()} == {"sentinel.txt"}


@pytest.mark.parametrize(
    ("relative", "replacement"),
    [
        ("references/scheduled-execution.md", ""),
        ("assets/scheduled-slot-prompt.md", ""),
        (
            "references/scheduled-execution.md",
            (
                SKILL_ROOT / "references" / "scheduled-execution.md"
            ).read_text(encoding="utf-8").replace(
                "same visible Codex App Scheduled task",
                "a later Scheduled task",
            ),
        ),
        (
            "assets/scheduled-slot-prompt.md",
            (
                SKILL_ROOT / "assets" / "scheduled-slot-prompt.md"
            ).read_text(encoding="utf-8")
            + "\nUse Goal and codex exec to create Issues automatically.\n",
        ),
        (
            "assets/repositories.toml.example",
            (
                SKILL_ROOT / "assets" / "repositories.toml.example"
            ).read_text(encoding="utf-8")
            .replace("max_parallel_tasks = 3", "max_parallel_tasks = 4")
            .replace("poll_interval_minutes = 10", "poll_interval_minutes = 15"),
        ),
        (
            "assets/repositories.toml.example",
            '''schema_version = 1
max_parallel_tasks = 3
poll_interval_minutes = 10

[[repositories]]
github = "other/EaseWise"
local_path = "/tmp/EaseWise"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "/Users/qiaoz-macmini/codex-mac-worker"
''',
        ),
    ],
)
def test_installer_preserves_existing_install_for_invalid_canonical_content(
    tmp_path: Path, relative: str, replacement: str
) -> None:
    assert_invalid_stage_preserves_install(tmp_path, relative, replacement)


def assert_invalid_stage_preserves_install(
    tmp_path: Path, relative: str, replacement: str
) -> None:
    env, _ = installed_test_environment(tmp_path)
    skills_root = Path(env["DUOMAC_SKILLS_ROOT"])
    installed = skills_root / "dual-mac-collaboration"
    installed.mkdir(parents=True)
    sentinel = installed / "sentinel.txt"
    sentinel.write_text("existing install\n", encoding="utf-8")

    source_root = tmp_path / "source"
    (source_root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "install_skill.sh", source_root / "scripts")
    shutil.copy2(ROOT / "scripts" / "validate_skill.py", source_root / "scripts")
    copied_skill = source_root / "skills" / "dual-mac-collaboration"
    shutil.copytree(SKILL_ROOT, copied_skill)
    (copied_skill / relative).write_text(replacement, encoding="utf-8")

    result = subprocess.run(
        ["/bin/zsh", str(source_root / "scripts" / "install_skill.sh")],
        cwd=source_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "existing install\n"
    assert {path.name for path in installed.iterdir()} == {"sentinel.txt"}


@pytest.mark.parametrize(
    ("relative", "boundary"),
    [
        (relative, boundary)
        for relative in (
            "references/scheduled-execution.md",
            "assets/scheduled-slot-prompt.md",
        )
        for boundary in (
            "duomac-scheduled-pick",
            "outcome",
            "claimed",
            "clean-noop",
            "maintenance",
            "maintenance_actions",
        )
    ],
)
def test_installer_preserves_existing_install_when_outcome_boundary_is_removed(
    tmp_path: Path, relative: str, boundary: str
) -> None:
    original = (SKILL_ROOT / relative).read_text(encoding="utf-8")
    assert boundary in original

    assert_invalid_stage_preserves_install(
        tmp_path,
        relative,
        original.replace(boundary, "removed-boundary"),
    )


def test_scheduled_documents_state_only_claimed_executes() -> None:
    for relative in (
        "references/scheduled-execution.md",
        "assets/scheduled-slot-prompt.md",
    ):
        text = (SKILL_ROOT / relative).read_text(encoding="utf-8")
        assert "Only `outcome: claimed` proceeds to code execution." in text


@pytest.mark.parametrize(
    "instruction",
    (
        "uSe GoAl.",
        "You MAY use `CODEX EXEC`.",
        "Instruction: use Goal.",
        "Invoke the Goal tool.",
        "Run codex exec.",
        "Start a legacy daemon.",
        "Start the LaunchDaemon Worker.",
        "Launch a LaunchDaemon Worker.",
        "Run a background worker.",
        "The Mac mini may create Issues autonomously.",
        "Automatically create new Issues.",
        "Create another Issue automatically.",
        "Expand the task scope.",
        "Infer new authority from comments.",
    ),
)
def test_installer_rejects_affirmative_forbidden_scheduled_instruction(
    tmp_path: Path, instruction: str
) -> None:
    relative = "assets/scheduled-slot-prompt.md"
    original = (SKILL_ROOT / relative).read_text(encoding="utf-8")

    assert_invalid_stage_preserves_install(
        tmp_path,
        relative,
        f"{original}\n{instruction}\n",
    )


def test_installer_removes_old_client_only_with_explicit_flag(tmp_path: Path) -> None:
    bin_dir, log = _fake_install_tools(tmp_path, "e" * 40)
    skills_root = tmp_path / "skills"
    old_skill = skills_root / "dispatch-codex-task"
    old_skill.mkdir(parents=True)
    wrapper_root = tmp_path / "bin"
    wrapper_root.mkdir()
    old_cli = wrapper_root / ("codex" + "ctl")
    old_cli.symlink_to("/tmp/old-" + "codex" + "ctl")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "DUOMAC_APP_ROOT": str(tmp_path / "app"),
        "DUOMAC_SKILLS_ROOT": str(skills_root),
        "DUOMAC_BIN_ROOT": str(wrapper_root),
        "DUOMAC_TEST_LOG": str(log),
        "DUOMAC_REAL_PYTHON": sys.executable,
    }

    first = subprocess.run(
        ["/bin/zsh", str(ROOT / "scripts/install_skill.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert old_skill.exists()
    assert old_cli.is_symlink()

    second = subprocess.run(
        [
            "/bin/zsh",
            str(ROOT / "scripts/install_skill.sh"),
            "--remove-legacy-client",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert not old_skill.exists()
    assert not old_cli.exists()


def test_retirement_refuses_nonterminal_task_before_mutation(tmp_path: Path) -> None:
    app_root = tmp_path / "legacy-app"
    state = app_root / "state"
    config = app_root / "config"
    state.mkdir(parents=True)
    config.mkdir()
    database = state / "worker.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("create table tasks (state text not null)")
        connection.execute("insert into tasks values ('running')")
    (config / "worker.toml").write_text("enabled = true\n", encoding="utf-8")

    result = subprocess.run(
        ["/bin/zsh", str(ROOT / "scripts/retire_legacy_worker.sh"), "--apply"],
        cwd=ROOT,
        env={**os.environ, "DUOMAC_APP_ROOT": str(app_root)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "nonterminal" in result.stderr
    assert not (app_root / "backups").exists()


def test_retirement_waits_for_launchd_bootout_to_finish(tmp_path: Path) -> None:
    app_root = tmp_path / "legacy-app"
    state = app_root / "state"
    config = app_root / "config"
    secrets = app_root / "secrets"
    state.mkdir(parents=True)
    config.mkdir()
    secrets.mkdir()
    database = state / "worker.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("create table tasks (state text not null)")
        connection.execute("insert into tasks values ('completed')")
    (config / "worker.toml").write_text("enabled = true\n", encoding="utf-8")
    (secrets / "private-material.pem").write_text("not-a-real-key\n", encoding="utf-8")

    launchd_root = tmp_path / "launchd"
    launchd_root.mkdir()
    (launchd_root / "com.easewise.codex-worker.plist").write_text("primary\n")
    (launchd_root / "com.easewise.codex-worker-backup.plist").write_text("backup\n")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    sudo.chmod(0o755)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
if [ "$1" = "bootout" ]; then
  exit 0
fi
if [ "$1" = "print" ]; then
  name=$(printf '%s' "$2" | tr '/.' '__')
  counter="$DUOMAC_LAUNCHCTL_STATE.$name"
  value=0
  [ -f "$counter" ] && value=$(cat "$counter")
  value=$((value + 1))
  printf '%s\n' "$value" > "$counter"
  if [ "$value" -le 2 ]; then
    echo loaded
    exit 0
  fi
  echo absent >&2
  exit 1
fi
exit 2
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    pgrep = fake_bin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    pgrep.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "DUOMAC_APP_ROOT": str(app_root),
        "DUOMAC_LAUNCHD_ROOT": str(launchd_root),
        "DUOMAC_LAUNCHCTL_STATE": str(tmp_path / "launchctl-state"),
        "DUOMAC_WAIT_ATTEMPTS": "5",
        "DUOMAC_WAIT_INTERVAL_SECONDS": "0",
    }

    result = subprocess.run(
        ["/bin/zsh", str(ROOT / "scripts/retire_legacy_worker.sh"), "--apply"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not secrets.exists()
    isolated = list(app_root.glob("legacy-secrets-*"))
    assert len(isolated) == 1
    assert isolated[0].stat().st_mode & 0o777 == 0o700
    assert "private-material.pem" not in result.stdout


def test_main_tree_has_no_legacy_entry_points() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "codex" + "-worker" not in pyproject
    assert "codex" + "ctl" not in pyproject
    assert not (ROOT / "src/codex_mac_worker").exists()
    assert not (ROOT / "skills/dispatch-codex-task").exists()
    assert not (ROOT / "templates").exists()


def test_repository_is_ready_for_dual_mac_dispatch() -> None:
    project_path = ROOT / ".duomac" / "project.toml"

    assert project_path.is_file()
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    assert project["schema_version"] == 1
    assert project["default_base_branch"] == "main"
    assert ".duomac" in project["protected_paths"]
    assert ".github/workflows" in project["protected_paths"]
    assert project["max_changed_files"] == 30
    assert project["max_diff_lines"] == 3000
    assert project["verification"]["fast"]["commands"]
    assert project["verification"]["full"]["commands"]


def test_readme_documents_explicit_scheduled_model_settings() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "explicit model and reasoning-effort values" in readme
    assert "Do not edit Codex App internal files or databases" in readme
    assert "same model and reasoning effort" in readme
