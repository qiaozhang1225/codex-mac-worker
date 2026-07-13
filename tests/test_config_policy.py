from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.config import ConfigError, load_project_config, parse_project_config
from codex_mac_worker.policy import PolicyError, validate_changed_paths, validate_task_policy
from codex_mac_worker.protocol import parse_task_body

from .test_protocol import task_body


def write_config(path: Path) -> None:
    path.write_text(
        """
schema_version = 1
default_base_branch = "main"
allowed_risk_levels = ["low", "medium"]
protected_paths = [".github/workflows", ".env", "product/deploy"]
max_changed_files = 3
max_diff_lines = 20
codex_attempt_timeout_minutes = 45
task_hard_timeout_minutes = 120
max_automatic_attempts = 2

[preparation]
commands = ["python3 -m venv .venv"]

[verification.fast]
commands = ["python -m unittest"]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_load_project_config_parses_verification_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)

    config = load_project_config(config_path)

    assert config.max_changed_files == 3
    assert config.preparation == ("python3 -m venv .venv",)
    assert config.verification["fast"] == ("python -m unittest",)


def test_parse_project_config_validates_remote_text_without_a_file(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)

    config = parse_project_config(config_path.read_text(encoding="utf-8"))

    assert config.default_base_branch == "main"
    assert config.max_changed_files == 3


def test_load_project_config_rejects_unknown_schema(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config_path.write_text(config_path.read_text().replace("schema_version = 1", "schema_version = 2"))

    with pytest.raises(ConfigError, match="schema_version"):
        load_project_config(config_path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("codex_attempt_timeout_minutes = 45", "codex_attempt_timeout_minutes = 46", "45"),
        ("task_hard_timeout_minutes = 120", "task_hard_timeout_minutes = 121", "120"),
        ("max_automatic_attempts = 2", "max_automatic_attempts = 3", "2"),
    ],
)
def test_project_config_cannot_raise_global_execution_caps(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_project_config(config_path)


def test_task_policy_rejects_high_risk_and_unknown_profile(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config = load_project_config(config_path)

    with pytest.raises(PolicyError, match="risk"):
        validate_task_policy(parse_task_body(task_body(risk="high")), config)

    spec = parse_task_body(task_body().replace("verification_profile: fast", "verification_profile: missing"))
    with pytest.raises(PolicyError, match="verification profile"):
        validate_task_policy(spec, config)


def test_changed_path_policy_rejects_protected_and_out_of_scope_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config = load_project_config(config_path)
    spec = parse_task_body(task_body())

    with pytest.raises(PolicyError, match="protected"):
        validate_changed_paths(spec, config, [".github/workflows/unsafe.yml"], 1)

    with pytest.raises(PolicyError, match="outside allowed_paths"):
        validate_changed_paths(spec, config, ["README.md"], 1)


def test_changed_path_policy_enforces_file_and_diff_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config = load_project_config(config_path)
    spec = parse_task_body(task_body())

    with pytest.raises(PolicyError, match="changed-file limit"):
        validate_changed_paths(spec, config, ["src/a", "src/b", "src/c", "src/d"], 4)

    with pytest.raises(PolicyError, match="diff-line limit"):
        validate_changed_paths(spec, config, ["src/a"], 21)


@pytest.mark.parametrize("unsafe", ["../src", "src/../../etc", "/etc/passwd", "src\\escape"])
def test_policy_rejects_unsafe_repository_paths(tmp_path: Path, unsafe: str) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config = load_project_config(config_path)
    spec = parse_task_body(task_body().replace("  - src/", f"  - {unsafe}"))

    with pytest.raises(PolicyError, match="invalid repository path"):
        validate_task_policy(spec, config)


@pytest.mark.parametrize(
    "objective",
    [
        "Deploy the API to production",
        "Delete production data",
        "Run a database migration",
        "部署到生产环境",
    ],
)
def test_policy_rejects_operational_and_irreversible_objectives(
    tmp_path: Path, objective: str
) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config = load_project_config(config_path)
    spec = parse_task_body(
        task_body().replace("Add a bounded worker feature", objective)
    )

    with pytest.raises(PolicyError, match="operational or irreversible"):
        validate_task_policy(spec, config)
