from __future__ import annotations

from pathlib import Path

import pytest

from duomac_contracts import (
    ContractError,
    load_project_config,
    parse_issue_body,
    render_issue_body,
    validate_task,
)


VALID_BODY = """<!-- duomac-task:v1 -->
```yaml
schema_version: 1
revision: 1
role:
  dispatcher: macbook
  executor: mac-mini
objective: Fix the bounded history-card layout
context:
  commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  files:
    - docs/product/product-spec.md
  decisions:
    - Do not change backend behavior
acceptance:
  - The mobile card uses the available width
scope:
  allowed_paths:
    - product/frontend/src/history
  out_of_scope:
    - Backend APIs
execution_plan:
  - Update the component
  - Run the fast profile
verification_profile: fast
delivery_mode: direct-main
risk: low
```
"""


PROJECT_TOML = '''schema_version = 1
default_base_branch = "main"
protected_paths = [".env", "product/deploy"]
max_changed_files = 30
max_diff_lines = 3000

[verification.fast]
commands = ["pytest -q"]
'''


def project_config(tmp_path: Path, text: str = PROJECT_TOML):
    path = tmp_path / "project.toml"
    path.write_text(text, encoding="utf-8")
    return load_project_config(path)


def test_parse_complete_issue_contract() -> None:
    spec = parse_issue_body(VALID_BODY)

    assert spec.revision == 1
    assert spec.dispatcher == "macbook"
    assert spec.executor == "mac-mini"
    assert spec.delivery_mode == "direct-main"
    assert spec.allowed_paths == ("product/frontend/src/history",)


def test_render_round_trips_complete_contract() -> None:
    spec = parse_issue_body(VALID_BODY)

    assert parse_issue_body(render_issue_body(spec)) == spec


@pytest.mark.parametrize("field", ["objective", "acceptance", "execution_plan"])
def test_rejects_missing_required_field(field: str) -> None:
    body = VALID_BODY.replace(f"{field}:", f"missing_{field}:")

    with pytest.raises(ContractError, match=field):
        parse_issue_body(body)


def test_rejects_duplicate_machine_blocks() -> None:
    with pytest.raises(ContractError, match="exactly one"):
        parse_issue_body(VALID_BODY + "\n" + VALID_BODY)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("revision: 1", "revision: 0", "revision"),
        ("a" * 40, "abc", "commit"),
        ("risk: low", "risk: high", "risk"),
        ("delivery_mode: direct-main", "delivery_mode: force-main", "delivery_mode"),
        (
            "product/frontend/src/history",
            "../product/frontend/src/history",
            "path",
        ),
    ],
)
def test_rejects_invalid_contract_values(old: str, new: str, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        parse_issue_body(VALID_BODY.replace(old, new))


def test_project_config_owns_verification_commands(tmp_path: Path) -> None:
    config = project_config(tmp_path)

    assert config.verification["fast"] == ("pytest -q",)


def test_rejects_unknown_verification_profile(tmp_path: Path) -> None:
    spec = parse_issue_body(VALID_BODY.replace("fast", "missing"))

    with pytest.raises(ContractError, match="verification profile"):
        validate_task(spec, project_config(tmp_path))


def test_rejects_protected_path_overlap(tmp_path: Path) -> None:
    spec = parse_issue_body(
        VALID_BODY.replace("product/frontend/src/history", "product/deploy")
    )

    with pytest.raises(ContractError, match="protected"):
        validate_task(spec, project_config(tmp_path))


@pytest.mark.parametrize(
    "objective",
    [
        "Deploy this change to production",
        "部署到生产环境",
        "Delete production database data",
    ],
)
def test_rejects_operational_or_irreversible_objective(
    tmp_path: Path, objective: str
) -> None:
    spec = parse_issue_body(
        VALID_BODY.replace("Fix the bounded history-card layout", objective)
    )

    with pytest.raises(ContractError, match="operational"):
        validate_task(spec, project_config(tmp_path))
