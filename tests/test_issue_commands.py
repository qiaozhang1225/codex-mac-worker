from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from duomac_contracts import parse_issue_body


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "dual-mac-collaboration" / "scripts"
VALID_BODY = """<!-- duomac-task:v1 -->
```yaml
schema_version: 2
revision: 2
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
  - milestone: 1
    objective: Update the component
    steps:
      - Apply the bounded layout change
  - milestone: 2
    objective: Verify and deliver the change
    steps:
      - Run the fast profile
      - Publish delivery evidence
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
ISSUE_URL = "https://github.com/owner/repo/issues/7"


def run_script(name: str, *args: str | Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *(str(arg) for arg in args)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def valid_spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text(VALID_BODY, encoding="utf-8")
    return path


@pytest.fixture
def project_config_file(tmp_path: Path) -> Path:
    path = tmp_path / "project.toml"
    path.write_text(PROJECT_TOML, encoding="utf-8")
    return path


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.jsonl"
    fixture = tmp_path / "gh-fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "issue_body": VALID_BODY,
                "issue_url": ISSUE_URL,
                "labels": ["bug", "duomac:ready"],
            }
        ),
        encoding="utf-8",
    )
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
stdin = sys.stdin.read()
with Path(os.environ["GH_FAKE_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": args, "stdin": stdin}) + "\\n")
if os.environ.get("GH_FAKE_UNAUTHENTICATED") == "1":
    print("authentication required", file=sys.stderr)
    raise SystemExit(4)
fixture = json.loads(Path(os.environ["GH_FAKE_FIXTURE"]).read_text())
if args[:2] == ["issue", "create"]:
    print(fixture["issue_url"])
elif args[:2] == ["issue", "view"] and "--json" in args:
    field = args[args.index("--json") + 1]
    if field == "body":
        print(json.dumps({"body": fixture["issue_body"]}))
    elif field == "labels":
        print(json.dumps({"labels": [{"name": item} for item in fixture["labels"]]}))
    else:
        print("unsupported fixture field", file=sys.stderr)
        raise SystemExit(2)
else:
    print("")
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "GH_FAKE_LOG": str(log),
            "GH_FAKE_FIXTURE": str(fixture),
        }
    )
    env["GH_CALL_LOG"] = str(log)
    return env


def gh_calls(env: dict[str, str]) -> list[dict[str, object]]:
    path = Path(env["GH_CALL_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_payload(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "payload.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def valid_checkpoint() -> dict[str, object]:
    return {
        "type": "checkpoint",
        "revision": 2,
        "milestone": 1,
        "completed": ["Updated the component"],
        "commits": [],
        "verification": ["pytest -q: passed"],
        "scope_status": "within-scope",
        "next": ["Inspect mobile rendering"],
        "blockers": [],
    }


def valid_delivery(mode: str = "direct-main") -> dict[str, object]:
    return {
        "type": "delivery",
        "revision": 2,
        "delivery_mode": mode,
        "commit": "b" * 40,
        "changed_paths": ["product/frontend/src/history/card.tsx"],
        "acceptance_results": [
            {
                "criterion": "The mobile card uses the available width",
                "status": "met",
                "evidence": "component test passed",
            }
        ],
        "verification": ["pytest -q: passed"],
        "remaining_risks": [],
    }


def test_issue_create_is_dry_run_without_yes(
    cli_env: dict[str, str], valid_spec_file: Path
) -> None:
    result = run_script(
        "issue_create.py", "--repo", "owner/repo", "--spec", valid_spec_file, env=cli_env
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["created"] is False
    assert parse_issue_body(output["contract"]) == parse_issue_body(VALID_BODY)
    assert gh_calls(cli_env) == []


def test_issue_create_with_yes_uses_ready_label(
    cli_env: dict[str, str], valid_spec_file: Path
) -> None:
    result = run_script(
        "issue_create.py",
        "--repo",
        "owner/repo",
        "--spec",
        valid_spec_file,
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["created"] is True
    assert any("duomac:ready" in call["argv"] for call in gh_calls(cli_env))


def test_comment_cannot_change_contract(cli_env: dict[str, str], tmp_path: Path) -> None:
    payload = write_payload(tmp_path, valid_checkpoint())

    result = run_script(
        "issue_checkpoint.py", ISSUE_URL, "--payload", payload, env=cli_env
    )

    assert result.returncode == 0, result.stderr
    calls = gh_calls(cli_env)
    assert any(call["argv"][:2] == ["issue", "comment"] for call in calls)
    assert not any("--body-file" in call["argv"] for call in calls if call["argv"][:2] == ["issue", "edit"])


def test_issue_validate_accepts_body_file(
    cli_env: dict[str, str], valid_spec_file: Path, project_config_file: Path
) -> None:
    result = run_script(
        "issue_validate.py",
        "--body-file",
        valid_spec_file,
        "--project-config",
        project_config_file,
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "valid": True,
        "revision": 2,
        "delivery_mode": "direct-main",
        "verification_profile": "fast",
    }
    assert gh_calls(cli_env) == []


def test_rejects_invalid_issue_url(cli_env: dict[str, str], tmp_path: Path) -> None:
    payload = write_payload(tmp_path, valid_checkpoint())

    result = run_script(
        "issue_checkpoint.py", "https://example.com/owner/repo/issues/7", "--payload", payload, env=cli_env
    )

    assert result.returncode != 0
    assert "GitHub Issue URL" in result.stderr
    assert result.stdout == ""


def test_reports_missing_gh(valid_spec_file: Path, tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)

    result = run_script(
        "issue_create.py",
        "--repo",
        "owner/repo",
        "--spec",
        valid_spec_file,
        "--yes",
        env=env,
    )

    assert result.returncode != 0
    assert "gh CLI was not found" in result.stderr


def test_reports_unauthenticated_gh(
    cli_env: dict[str, str], valid_spec_file: Path
) -> None:
    cli_env["GH_FAKE_UNAUTHENTICATED"] = "1"

    result = run_script(
        "issue_create.py",
        "--repo",
        "owner/repo",
        "--spec",
        valid_spec_file,
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "authentication required" in result.stderr


def test_state_transition_removes_other_status_labels(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = write_payload(tmp_path, valid_checkpoint())

    result = run_script(
        "issue_checkpoint.py", ISSUE_URL, "--payload", payload, env=cli_env
    )

    assert result.returncode == 0, result.stderr
    edits = [call for call in gh_calls(cli_env) if call["argv"][:2] == ["issue", "edit"]]
    assert len(edits) == 1
    argv = edits[0]["argv"]
    assert "duomac:ready" in argv
    assert "duomac:active" in argv
    assert "bug" not in argv


def test_blocked_comment_sets_blocked_label(cli_env: dict[str, str], tmp_path: Path) -> None:
    payload = write_payload(
        tmp_path,
        {
            "type": "blocked",
            "revision": 2,
            "reason": "The requested path conflicts with a concurrent change",
            "completed": ["Created an isolated worktree"],
            "next": ["Wait for a revised Issue body"],
        },
    )

    result = run_script(
        "issue_checkpoint.py", ISSUE_URL, "--payload", payload, env=cli_env
    )

    assert result.returncode == 0, result.stderr
    assert any("duomac:blocked" in call["argv"] for call in gh_calls(cli_env))


def test_checkpoint_rejects_revision_mismatch(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    checkpoint = valid_checkpoint()
    checkpoint["revision"] = 1
    payload = write_payload(tmp_path, checkpoint)

    result = run_script(
        "issue_checkpoint.py", ISSUE_URL, "--payload", payload, env=cli_env
    )

    assert result.returncode != 0
    assert "revision" in result.stderr
    assert not any(call["argv"][:2] == ["issue", "comment"] for call in gh_calls(cli_env))


def test_delivered_task_branch_stays_open(cli_env: dict[str, str], tmp_path: Path) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    content = json.loads(fixture.read_text(encoding="utf-8"))
    content["issue_body"] = VALID_BODY.replace("direct-main", "task-branch")
    fixture.write_text(json.dumps(content), encoding="utf-8")
    payload = write_payload(tmp_path, valid_delivery("task-branch"))

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        payload,
        "--state",
        "delivered",
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    calls = gh_calls(cli_env)
    assert any("duomac:delivered" in call["argv"] for call in calls)
    assert not any(call["argv"][:2] == ["issue", "close"] for call in calls)


def test_completed_direct_main_closes_issue(cli_env: dict[str, str], tmp_path: Path) -> None:
    payload = write_payload(tmp_path, valid_delivery())

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        payload,
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    calls = gh_calls(cli_env)
    assert any("duomac:completed" in call["argv"] for call in calls)
    assert any(call["argv"][:2] == ["issue", "close"] for call in calls)


def test_completed_rejects_task_branch(cli_env: dict[str, str], tmp_path: Path) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    content = json.loads(fixture.read_text(encoding="utf-8"))
    content["issue_body"] = VALID_BODY.replace("direct-main", "task-branch")
    fixture.write_text(json.dumps(content), encoding="utf-8")
    payload = write_payload(tmp_path, valid_delivery("task-branch"))

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        payload,
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "direct-main" in result.stderr
    assert not any(call["argv"][:2] == ["issue", "close"] for call in gh_calls(cli_env))


def test_issue_complete_is_dry_run_without_yes(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = write_payload(tmp_path, valid_delivery())

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        payload,
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applied"] is False
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )
