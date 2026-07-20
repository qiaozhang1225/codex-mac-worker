from __future__ import annotations

import hashlib
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
                "comments": [],
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
if os.environ.get("GH_FAKE_FAIL_COMMAND") == " ".join(args[:2]):
    print("injected gh failure", file=sys.stderr)
    raise SystemExit(5)
fixture = json.loads(Path(os.environ["GH_FAKE_FIXTURE"]).read_text())
if args[:2] == ["issue", "create"]:
    print(fixture["issue_url"])
elif args[:2] == ["issue", "view"] and "--json" in args:
    field = args[args.index("--json") + 1]
    if field == "body":
        print(json.dumps({"body": fixture["issue_body"]}))
    elif field == "labels":
        print(json.dumps({"labels": [{"name": item} for item in fixture["labels"]]}))
    elif field == "comments":
        print(json.dumps({"comments": fixture.get("comments", [])}))
    elif field == "state":
        print(json.dumps({"state": fixture.get("state", "OPEN")}))
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


def event_comment(
    payload: dict[str, object], comment_id: str = "IC_1"
) -> dict[str, object]:
    rendered = yaml.safe_dump(payload, sort_keys=False).rstrip()
    return {
        "id": comment_id,
        "createdAt": "2026-07-20T00:00:00Z",
        "body": f"<!-- duomac-event:v1 -->\n```yaml\n{rendered}\n```\n",
    }


def valid_task_start(
    *,
    claim_id: str = "c" * 40,
    task_hash: str | None = None,
    repository: str = "owner/repo",
    base_branch: str = "main",
    context_commit: str = "a" * 40,
) -> dict[str, object]:
    return {
        "type": "task-start",
        "revision": 2,
        "task_hash": task_hash or hashlib.sha256(VALID_BODY.encode("utf-8")).hexdigest(),
        "repository": repository,
        "base_branch": base_branch,
        "context_commit": context_commit,
        "skill_commit": "d" * 40,
        "base_commit": "a" * 40,
        "plan_summary": ["Execute two milestones"],
        "execution_mode": "scheduled",
        "slot": 1,
        "claim_id": claim_id,
    }


def valid_checkpoint() -> dict[str, object]:
    return {
        "type": "checkpoint",
        "revision": 2,
        "milestone": 1,
        "completed": ["Updated the component"],
        "commits": ["b" * 40],
        "verification": ["pytest -q: passed"],
        "scope_status": "within-scope",
        "next": ["Inspect mobile rendering"],
        "blockers": [],
    }


def checkpoint_event(milestone: int) -> dict[str, object]:
    payload = valid_checkpoint()
    payload["milestone"] = milestone
    return event_comment(payload, f"IC_{milestone}")


def completion_comments(*events: dict[str, object]) -> list[dict[str, object]]:
    return [
        event_comment(valid_task_start()),
        checkpoint_event(1),
        checkpoint_event(2),
        *events,
    ]


def set_issue_fixture(
    cli_env: dict[str, str],
    *,
    comments: list[dict[str, object]],
    labels: list[str] | None = None,
    state: str | None = None,
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text(encoding="utf-8"))
    value["comments"] = comments
    if labels is not None:
        value["labels"] = labels
    if state is not None:
        value["state"] = state
    fixture.write_text(json.dumps(value), encoding="utf-8")


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
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")
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
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")
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


def test_checkpoint_rejects_gap_after_task_start(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    payload = valid_checkpoint()
    payload["milestone"] = 2

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "milestone 1" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_checkpoint_rejects_unclaimed_issue_without_mutation(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "exactly one current-revision task-start" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_checkpoint_repair_rejects_unclaimed_issue_without_label_mutation(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_checkpoint())]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "exactly one current-revision task-start" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_checkpoint_rejects_conflicting_task_start_history_without_mutation(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start(), "IC_start_1"),
        event_comment(valid_task_start(claim_id="e" * 40), "IC_start_2"),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "exactly one current-revision task-start" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_checkpoint_repair_rejects_task_start_after_checkpoint_history(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    checkpoint = valid_checkpoint()
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(checkpoint, "IC_checkpoint"),
        event_comment(valid_task_start(), "IC_start"),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, checkpoint),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "task-start must precede checkpoint history" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_task_start_is_idempotent_for_same_claim(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_task_start()
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(payload)]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["repaired"] is True
    assert not any(
        call["argv"][:2] == ["issue", "comment"] for call in gh_calls(cli_env)
    )


def test_task_start_rejects_different_claim_without_mutation(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_task_start(claim_id="e" * 40)),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "different claim" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_task_start_rejects_same_claim_with_different_task_binding(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    changed = valid_task_start(task_hash="e" * 64)
    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, changed),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "binding" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )

@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"execution_mode": "automatic"}, "execution_mode"),
        ({"slot": 0}, "slot"),
        ({"slot": True}, "slot"),
        ({"claim_id": "C" * 40}, "claim_id"),
        ({"task_hash": "A" * 64}, "task_hash"),
        ({"repository": "not-a-repository"}, "repository"),
        ({"base_branch": ""}, "base_branch"),
        ({"base_branch": "main..raced"}, "base_branch"),
        ({"context_commit": "not-a-commit"}, "context_commit"),
    ],
)
def test_task_start_rejects_invalid_scheduled_identity(
    cli_env: dict[str, str],
    tmp_path: Path,
    updates: dict[str, object],
    message: str,
) -> None:
    payload = valid_task_start()
    payload.update(updates)

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


@pytest.mark.parametrize(
    "field",
    ["task_hash", "repository", "base_branch", "context_commit"],
)
def test_task_start_rejects_missing_scheduled_binding_field(
    cli_env: dict[str, str], tmp_path: Path, field: str
) -> None:
    payload = valid_task_start()
    payload.pop(field)

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert field in result.stderr


def test_interactive_task_start_remains_compatible_without_scheduled_binding(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_task_start()
    payload["execution_mode"] = "interactive"
    for field in (
        "slot",
        "claim_id",
        "task_hash",
        "repository",
        "base_branch",
        "context_commit",
    ):
        payload.pop(field)

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"commits": []}, "at least one full commit SHA"),
        ({"blockers": ["Waiting on access"]}, "blockers must be empty"),
        ({"verification": ["pytest -q"]}, "explicitly passing"),
        ({"verification": ["pytest -q: failed"]}, "failure or error marker"),
        (
            {"verification": ["pytest -q: passed", "ruff check: failed"]},
            "failure or error marker",
        ),
        ({"verification": [": passed"]}, "meaningful check description"),
        ({"verification": ["---: passed"]}, "meaningful check description"),
        (
            {"verification": ["pytest failure recovered: passed"]},
            "failure or error marker",
        ),
        (
            {"verification": ["pytest error recovered: passed"]},
            "failure or error marker",
        ),
        ({"verification": ["pytest -q: 0 passed"]}, "greater than zero"),
        ({"verification": ["pytest -q: 00 passed"]}, "greater than zero"),
    ],
)
def test_checkpoint_rejects_incomplete_or_failing_evidence_without_mutation(
    cli_env: dict[str, str],
    tmp_path: Path,
    updates: dict[str, object],
    message: str,
) -> None:
    payload = valid_checkpoint()
    payload.update(updates)

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


@pytest.mark.parametrize(
    "verification",
    [
        ["pytest -q: passed"],
        ["pytest -q: 7 passed"],
        ["ruff check: PASSED", "pytest -q: 92 PASSED"],
    ],
)
def test_checkpoint_accepts_unambiguous_passing_verification(
    cli_env: dict[str, str], tmp_path: Path, verification: list[str]
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    payload = valid_checkpoint()
    payload["verification"] = verification

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["published"] is True


def test_checkpoint_ignores_events_from_previous_revision(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    old_checkpoint = valid_checkpoint()
    old_checkpoint["revision"] = 1
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(old_checkpoint, "IC_old"),
        event_comment(valid_task_start(), "IC_start"),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["published"] is True


def test_checkpoint_rerun_repairs_label_without_duplicate_comment(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    checkpoint = valid_checkpoint()
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start(), "IC_start"),
        event_comment(checkpoint, "IC_checkpoint"),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, checkpoint),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["repaired"] is True
    calls = gh_calls(cli_env)
    assert not any(call["argv"][:2] == ["issue", "comment"] for call in calls)
    assert any(call["argv"][:2] == ["issue", "edit"] for call in calls)


def test_checkpoint_rejects_after_all_declared_milestones(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    first = valid_checkpoint()
    second = valid_checkpoint()
    second["milestone"] = 2
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start(), "IC_start"),
        event_comment(first, "IC_1"),
        event_comment(second, "IC_2"),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    payload = valid_checkpoint()
    payload["milestone"] = 3

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "all declared milestones" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_matching_out_of_plan_checkpoint_cannot_trigger_label_repair(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    checkpoints: list[dict[str, object]] = []
    for milestone in (1, 2, 3):
        checkpoint = valid_checkpoint()
        checkpoint["milestone"] = milestone
        checkpoints.append(checkpoint)
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start(), "IC_start"),
        *(
            event_comment(checkpoint, f"IC_{checkpoint['milestone']}")
            for checkpoint in checkpoints
        ),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, checkpoints[-1]),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "milestone 3" in result.stderr
    assert "not declared" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_malformed_marked_comment_rejects_without_mutation(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        {
            "id": "IC_bad",
            "createdAt": "2026-07-20T00:00:00Z",
            "body": "<!-- duomac-event:v1 -->\n```yaml\n[unterminated\n```\n",
        }
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "invalid YAML" in result.stderr
    assert "Traceback" not in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_comment_with_multiple_event_markers_rejects_without_mutation(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    comment = event_comment(valid_task_start())
    comment["body"] += "<!-- duomac-event:v1 -->\n"
    value["comments"] = [comment]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "one YAML block" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("Leading prose\n", ""),
        ("", "Trailing prose\n"),
        ("", "```yaml\ntype: checkpoint\nrevision: 2\n```\n"),
    ],
)
def test_marked_comment_rejects_content_outside_complete_envelope(
    cli_env: dict[str, str], tmp_path: Path, prefix: str, suffix: str
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    comment = event_comment(valid_task_start())
    comment["body"] = prefix + str(comment["body"]) + suffix
    value["comments"] = [comment]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "duomac event" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_spoofed_out_of_order_checkpoint_cannot_trigger_label_repair(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    checkpoint = valid_checkpoint()
    checkpoint["milestone"] = 2
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start(), "IC_start"),
        event_comment(checkpoint),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, checkpoint),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "continuous from 1" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"scope_status": "outside-scope"}, "scope_status"),
        ({"blockers": [1]}, "blockers"),
        ({"commits": ["short"]}, "commit SHA"),
        ({"verification": []}, "verification"),
    ],
)
def test_malformed_existing_checkpoint_evidence_cannot_advance_sequence(
    cli_env: dict[str, str],
    tmp_path: Path,
    updates: dict[str, object],
    message: str,
) -> None:
    checkpoint = valid_checkpoint()
    checkpoint.update(updates)
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start(), "IC_start"),
        event_comment(checkpoint),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    payload = valid_checkpoint()
    payload["milestone"] = 2

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_malformed_existing_task_start_cannot_repair_claim(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    task_start = valid_task_start()
    task_start["plan_summary"] = []
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(task_start)]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_task_start()),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "plan_summary" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
        for call in gh_calls(cli_env)
    )


def test_successful_event_is_commented_before_label_update(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_checkpoint()),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    mutations = [
        call["argv"][:2]
        for call in gh_calls(cli_env)
        if call["argv"][:2] in (["issue", "comment"], ["issue", "edit"])
    ]
    assert mutations == [["issue", "comment"], ["issue", "edit"]]


def test_delivered_task_branch_stays_open(cli_env: dict[str, str], tmp_path: Path) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    content = json.loads(fixture.read_text(encoding="utf-8"))
    content["issue_body"] = VALID_BODY.replace("direct-main", "task-branch")
    content["comments"] = [
        event_comment(valid_task_start()),
        checkpoint_event(1),
        checkpoint_event(2),
    ]
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


def test_completion_rejects_missing_final_checkpoint(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start()), checkpoint_event(1)]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "missing checkpoint milestones: 2" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


def test_completion_accepts_all_current_revision_checkpoints(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start()),
        checkpoint_event(1),
        checkpoint_event(2),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr


def test_completion_rejects_not_met_acceptance(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_delivery()
    payload["acceptance_results"][0]["status"] = "not-met"

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "all acceptance results must be met" in result.stderr


def test_completion_preview_rejects_absent_task_start(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    set_issue_fixture(cli_env, comments=[checkpoint_event(1), checkpoint_event(2)])

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "exactly one current-revision task-start" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


@pytest.mark.parametrize(
    ("comments", "message"),
    [
        (
            [event_comment(valid_task_start()), checkpoint_event(1), checkpoint_event(1)],
            "existing checkpoint milestones must be continuous from 1",
        ),
        (
            [event_comment(valid_task_start()), checkpoint_event(2)],
            "existing checkpoint milestones must be continuous from 1",
        ),
        (
            completion_comments(checkpoint_event(3)),
            "historical checkpoint milestone 3 is not declared",
        ),
        (
            [
                event_comment(valid_task_start()),
                event_comment(
                    {**valid_checkpoint(), "blockers": ["waiting on dependency"]}
                ),
                checkpoint_event(2),
            ],
            "checkpoint blockers must be empty",
        ),
    ],
)
def test_completion_preview_rejects_invalid_checkpoint_history(
    cli_env: dict[str, str],
    tmp_path: Path,
    comments: list[dict[str, object]],
    message: str,
) -> None:
    set_issue_fixture(cli_env, comments=comments)

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


def test_completion_preview_rejects_current_blocked_event(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    blocked = event_comment(
        {
            "type": "blocked",
            "revision": 2,
            "reason": "Need approval",
            "completed": [],
            "next": ["Wait for approval"],
        }
    )
    set_issue_fixture(cli_env, comments=completion_comments(blocked))

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "current revision contains an unresolved blocked event" in result.stderr


@pytest.mark.parametrize(
    "delivery",
    [
        {**valid_delivery(), "commit": "e" * 40},
        {"type": "delivery", "revision": 2},
    ],
)
def test_completion_preview_rejects_conflicting_or_malformed_delivery_history(
    cli_env: dict[str, str], tmp_path: Path, delivery: dict[str, object]
) -> None:
    set_issue_fixture(cli_env, comments=completion_comments(event_comment(delivery)))

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode != 0
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


def test_completion_rejects_duplicate_matching_delivery_history(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_delivery()
    set_issue_fixture(
        cli_env,
        comments=completion_comments(
            event_comment(payload, "IC_delivery_1"),
            event_comment(payload, "IC_delivery_2"),
        ),
    )

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "at most one delivery event" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


def test_completion_rejects_delivery_before_final_checkpoint(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_delivery()
    set_issue_fixture(
        cli_env,
        comments=[
            event_comment(valid_task_start()),
            checkpoint_event(1),
            event_comment(payload, "IC_delivery"),
            checkpoint_event(2),
        ],
    )

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "delivery event must follow the final required checkpoint" in result.stderr
    assert not any(
        call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


def test_completion_first_run_comments_before_label_and_close(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    set_issue_fixture(cli_env, comments=completion_comments())

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    mutations = [
        call["argv"][:2]
        for call in gh_calls(cli_env)
        if call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
    ]
    assert mutations == [["issue", "comment"], ["issue", "edit"], ["issue", "close"]]


def test_completion_rerun_repairs_without_duplicate_delivery_comment(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_delivery()
    set_issue_fixture(
        cli_env,
        comments=completion_comments(event_comment(payload, "IC_delivery")),
        labels=["bug", "duomac:ready"],
        state="OPEN",
    )

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    mutations = [
        call["argv"][:2]
        for call in gh_calls(cli_env)
        if call["argv"][:2] in (["issue", "comment"], ["issue", "edit"], ["issue", "close"])
    ]
    assert mutations == [["issue", "edit"], ["issue", "close"]]


def test_completion_comment_failure_prevents_label_and_close_mutations(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    set_issue_fixture(cli_env, comments=completion_comments())
    cli_env["GH_FAKE_FAIL_COMMAND"] = "issue comment"

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert not any(
        call["argv"][:2] in (["issue", "edit"], ["issue", "close"])
        for call in gh_calls(cli_env)
    )


def test_completed_direct_main_closes_issue(cli_env: dict[str, str], tmp_path: Path) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    content = json.loads(fixture.read_text(encoding="utf-8"))
    content["comments"] = [
        event_comment(valid_task_start()),
        checkpoint_event(1),
        checkpoint_event(2),
    ]
    fixture.write_text(json.dumps(content), encoding="utf-8")
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
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    content = json.loads(fixture.read_text(encoding="utf-8"))
    content["comments"] = [
        event_comment(valid_task_start()),
        checkpoint_event(1),
        checkpoint_event(2),
    ]
    fixture.write_text(json.dumps(content), encoding="utf-8")
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
