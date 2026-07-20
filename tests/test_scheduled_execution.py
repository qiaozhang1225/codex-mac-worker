from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from typing import cast

import pytest

from duomac_contracts import ContractError, TaskSpec, parse_issue_body
from duomac_github import IssueEvent, parse_issue_events
from duomac_scheduled import (
    ActiveTask,
    Candidate,
    load_scheduled_config,
    paths_overlap,
    select_candidate,
    select_candidate_result,
)
from tests.test_contracts import LEGACY_BODY, PROJECT_TOML, VALID_BODY
from tests.test_issue_commands import event_comment, valid_task_start


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "dual-mac-collaboration" / "scripts"
READY_LABELS = ("duomac:ready",)


def write_config(tmp_path: Path, *, maximum: int = 3) -> Path:
    first = tmp_path / "EaseWise"
    second = tmp_path / "codex-mac-worker"
    first.mkdir()
    second.mkdir()
    path = tmp_path / "repositories.toml"
    path.write_text(
        f'''schema_version = 1
max_parallel_tasks = {maximum}
poll_interval_minutes = 10

[[repositories]]
github = "qiaozhang1225/EaseWise"
local_path = "{first}"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "{second}"
''',
        encoding="utf-8",
    )
    return path


def test_loads_two_repository_targets(tmp_path: Path) -> None:
    config = load_scheduled_config(write_config(tmp_path))

    assert config.max_parallel_tasks == 3
    assert [item.github for item in config.repositories] == [
        "qiaozhang1225/EaseWise",
        "qiaozhang1225/codex-mac-worker",
    ]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (("product/frontend",), ("product/frontend/src",), True),
        (("README.md",), ("product/frontend",), False),
        (("product",), ("productivity",), False),
        (("Product/Frontend",), ("product/frontend/src",), True),
        (("product//frontend",), ("docs",), True),
        ((), ("docs",), True),
    ],
)
def test_path_overlap(left: tuple[str, ...], right: tuple[str, ...], expected: bool) -> None:
    assert paths_overlap(left, right) is expected


def test_selection_skips_same_repo_overlap_but_allows_other_repo() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
        Candidate("owner/other", "https://github.com/owner/other/issues/2", "2026-01-02T00:00:00Z", spec, labels=READY_LABELS),
    )
    active = (ActiveTask("owner/repo", ("product/frontend",)),)

    selected = select_candidate(ready, active, max_parallel_tasks=3)

    assert selected is not None
    assert selected.issue_url.endswith("/issues/2")


def test_selection_treats_case_variants_as_the_same_repository() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("OWNER/REPO", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
    )
    active = (ActiveTask("owner/repo", ("product/frontend",)),)

    assert select_candidate(ready, active, max_parallel_tasks=3) is None


def task_start_event(revision: int) -> IssueEvent:
    return IssueEvent(
        comment_id="IC_start",
        created_at="2026-01-01T00:00:00Z",
        payload={
            "type": "task-start",
            "revision": revision,
            "task_hash": hashlib.sha256(VALID_BODY.encode("utf-8")).hexdigest(),
            "repository": "owner/repo",
            "base_branch": "main",
            "context_commit": "a" * 40,
            "skill_commit": "a" * 40,
            "base_commit": "b" * 40,
            "plan_summary": ["Start the task"],
            "execution_mode": "scheduled",
            "slot": 1,
            "claim_id": "c" * 40,
        },
    )


@pytest.mark.parametrize(
    ("candidate", "rejection"),
    [
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(LEGACY_BODY)),
            "schema-version",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY)),
            "missing-ready-label",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), labels=("duomac:active",)),
            "wrong-dispatch-label",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), labels=("duomac:blocked",)),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), labels=READY_LABELS, events=(task_start_event(2),)),
            "already-claimed",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), state="cancelled"),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), state="blocked"),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), state="completed"),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", cast(TaskSpec, object())),
            "invalid-spec",
        ),
    ],
)
def test_selection_skips_ineligible_candidates_with_explicit_reason(
    candidate: Candidate, rejection: str
) -> None:
    result = select_candidate_result((candidate,), (), max_parallel_tasks=3)

    assert result.candidate is None
    assert result.reason == "invalid-candidates-blocked"
    assert result.skipped[0].reason == rejection
    assert select_candidate((candidate,), (), max_parallel_tasks=3) is None


def test_selection_skips_malformed_candidates_without_preventing_a_valid_pick() -> None:
    spec = parse_issue_body(VALID_BODY)
    valid = Candidate("owner/repo", "https://github.com/owner/repo/issues/2", "2026-01-02T00:00:00Z", spec, labels=READY_LABELS)

    result = select_candidate_result((cast(Candidate, object()), valid), (), 3)

    assert result.candidate == valid
    assert result.reason == "selected"
    assert result.skipped[0].reason == "malformed-candidate"


@pytest.mark.parametrize(
    ("events", "rejection"),
    [
        ((IssueEvent("IC_bad", "2026-01-01T00:00:00Z", {"type": "task-start", "revision": "2"}),), "invalid-events"),
        ((IssueEvent("IC_bad", "2026-01-01T00:00:00Z", {"type": "blocked", "revision": 2}),), "invalid-events"),
        ((IssueEvent("IC_bad", "2026-01-01T00:00:00Z", {"type": "delivery", "revision": 2}),), "invalid-events"),
        ((task_start_event(2), task_start_event(2)), "invalid-current-revision-claim"),
    ],
)
def test_selection_fails_closed_for_malformed_or_ambiguous_current_events(
    events: tuple[IssueEvent, ...], rejection: str
) -> None:
    candidate = Candidate(
        "owner/repo",
        "https://github.com/owner/repo/issues/1",
        "2026-01-01T00:00:00Z",
        parse_issue_body(VALID_BODY),
        labels=READY_LABELS,
        events=events,
    )

    result = select_candidate_result((candidate,), (), 3)

    assert result.candidate is None
    assert result.skipped[0].reason == rejection


def test_selection_rejects_spec_that_cannot_be_rendered() -> None:
    spec = replace(
        parse_issue_body(VALID_BODY),
        decisions=cast(tuple[str, ...], (object(),)),
    )
    candidate = Candidate(
        "owner/repo",
        "https://github.com/owner/repo/issues/1",
        "2026-01-01T00:00:00Z",
        spec,
        labels=READY_LABELS,
    )

    result = select_candidate_result((candidate,), (), 3)

    assert result.candidate is None
    assert result.skipped[0].reason == "invalid-spec"


@pytest.mark.parametrize(
    "active",
    [
        (ActiveTask("", ("docs",)),),
        (ActiveTask("owner/other", ("product//frontend",)),),
    ],
)
def test_selection_rejects_invalid_active_tasks_before_repository_comparison(
    active: tuple[ActiveTask, ...]
) -> None:
    candidate = Candidate(
        "owner/repo",
        "https://github.com/owner/repo/issues/1",
        "2026-01-01T00:00:00Z",
        parse_issue_body(VALID_BODY),
        labels=READY_LABELS,
    )

    result = select_candidate_result((candidate,), active, 3)

    assert result.candidate is None
    assert result.reason == "invalid-active"


def test_selection_uses_creation_time_then_issue_url_order() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("owner/repo", "https://github.com/owner/repo/issues/20", "2026-01-02T00:00:00Z", spec, labels=READY_LABELS),
        Candidate("owner/repo", "https://github.com/owner/repo/issues/11", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
        Candidate("owner/repo", "https://github.com/owner/repo/issues/10", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
    )

    selected = select_candidate(ready, (), max_parallel_tasks=3)

    assert selected is not None
    assert selected.issue_url.endswith("/issues/10")


def test_selection_stops_at_parallel_limit() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec),)
    active = tuple(ActiveTask(f"owner/repo-{index}", ("README.md",)) for index in range(3))

    assert select_candidate(ready, active, max_parallel_tasks=3) is None


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = 2", "schema_version"),
        ("schema_version = 1", "schema_version = true", "schema_version"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = 0", "max_parallel_tasks"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = 2", "max_parallel_tasks"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = true", "max_parallel_tasks"),
        ("poll_interval_minutes = 10", "poll_interval_minutes = 61", "poll_interval_minutes"),
        ("poll_interval_minutes = 10", "poll_interval_minutes = 15", "poll_interval_minutes"),
        ('github = "qiaozhang1225/EaseWise"', 'github = "not a repo"', "github"),
    ],
)
def test_rejects_invalid_scheduled_config_values(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        load_scheduled_config(path)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("unexpected = true\n", "unknown scheduled config fields"),
        ("\nunknown = true\n", "unknown repository fields"),
    ],
)
def test_rejects_unknown_scheduled_config_fields(
    tmp_path: Path, extra: str, message: str
) -> None:
    path = write_config(tmp_path)
    if "repository" in message:
        content = path.read_text(encoding="utf-8").replace(
            'github = "qiaozhang1225/EaseWise"\n',
            'github = "qiaozhang1225/EaseWise"\nunknown = true\n',
        )
    else:
        content = extra + path.read_text(encoding="utf-8")
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        load_scheduled_config(path)


def test_rejects_relative_local_repository_path(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(str(tmp_path / "EaseWise"), "EaseWise"),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="absolute path"):
        load_scheduled_config(path)


def test_config_validator_only_reads_configuration(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    before = path.read_bytes()
    entries_before = sorted(item.name for item in tmp_path.iterdir())

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "config_validate.py"), "--config", str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["valid"] is True
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == entries_before


def test_rejects_duplicate_resolved_repository_paths(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    content = path.read_text(encoding="utf-8")
    first = tmp_path / "EaseWise"
    path.write_text(
        content.replace(str(tmp_path / "codex-mac-worker"), str(first / ".." / first.name)),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="local paths must be unique"):
        load_scheduled_config(path)


def test_rejects_duplicate_repository_names_case_insensitively(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "qiaozhang1225/codex-mac-worker", "QIAOZHANG1225/EASEWISE"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="GitHub names must be unique"):
        load_scheduled_config(path)


@dataclass
class ScheduledEnv:
    env: dict[str, str]
    config: Path
    app_root: Path
    fixture: Path
    log: Path

    def run_picker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "scheduled_pick.py"),
                "--config",
                str(self.config),
                "--app-root",
                str(self.app_root),
                *args,
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def state(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def write_state(self, value: dict[str, object]) -> None:
        self.fixture.write_text(json.dumps(value), encoding="utf-8")

    def github_writes(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [
            call
            for call in calls
            if call["argv"][:2]
            in (["issue", "edit"], ["issue", "comment"], ["issue", "close"])
        ]

    def task_start_comments(self) -> list[dict[str, object]]:
        comments = self.state()["issues"][0]["comments"]
        return [item for item in comments if "type: task-start" in item["body"]]

    def blocked_comments(self) -> list[dict[str, object]]:
        comments = self.state()["issues"][0]["comments"]
        return [item for item in comments if "type: blocked" in item["body"]]

    def replace_ready_body(self, body: str) -> None:
        value = self.state()
        value["issues"][0]["body"] = body
        self.write_state(value)

    def add_comment(self, comment: dict[str, object]) -> None:
        value = self.state()
        value["issues"][0]["comments"].append(comment)
        self.write_state(value)

    def set_labels(self, labels: list[str]) -> None:
        value = self.state()
        value["issues"][0]["labels"] = labels
        self.write_state(value)

    def current_label(self) -> str:
        labels = self.state()["issues"][0]["labels"]
        return next(item for item in labels if item.startswith("duomac:"))

    def repository_path(self, index: int = 0) -> Path:
        config = tomllib.loads(self.config.read_text(encoding="utf-8"))
        return Path(config["repositories"][index]["local_path"])

    def claim_files(self) -> list[Path]:
        claims = self.app_root / "claims"
        return sorted(claims.glob("*.json")) if claims.is_dir() else []

    def scheduled_start(self, *, claim_id: str = "c" * 40) -> dict[str, object]:
        issue = self.state()["issues"][0]
        spec = parse_issue_body(issue["body"])
        return valid_task_start(
            claim_id=claim_id,
            task_hash=hashlib.sha256(issue["body"].encode("utf-8")).hexdigest(),
            repository=issue["repo"],
            base_branch="main",
            context_commit=spec.context_commit,
        )

    def run_two_pickers_concurrently(
        self, first_slot: int, second_slot: int
    ) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
        command = lambda slot: [
            sys.executable,
            str(SCRIPTS / "scheduled_pick.py"),
            "--config",
            str(self.config),
            "--app-root",
            str(self.app_root),
            "--slot",
            str(slot),
            "--yes",
        ]
        env = {**self.env, "GH_FAKE_BARRIER_TARGET": "2"}
        processes = [
            subprocess.Popen(
                command(slot),
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for slot in (first_slot, second_slot)
        ]
        completed = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            completed.append(
                subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            )
        return tuple(completed)


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _create_repository(
    tmp_path: Path,
    name: str,
    github: str,
    git_env: dict[str, str],
) -> tuple[Path, str]:
    remote = tmp_path / "remotes" / f"{name}.git"
    remote.parent.mkdir(exist_ok=True)
    remote.mkdir()
    _git(remote, "init", "--bare")

    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Scheduled Test")
    _git(repo, "config", "user.email", "scheduled@example.invalid")
    (repo / ".duomac").mkdir()
    (repo / ".duomac" / "project.toml").write_text(PROJECT_TOML, encoding="utf-8")
    context = repo / "docs" / "product" / "product-spec.md"
    context.parent.mkdir(parents=True)
    context.write_text("# Frozen product context\n", encoding="utf-8")
    (repo / "product" / "frontend" / "src" / "history").mkdir(parents=True)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test: freeze scheduled context")
    _git(repo, "remote", "add", "origin", f"https://github.com/{github}.git")
    _git(repo, "push", "-u", "origin", "main", env=git_env)
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def scheduled_env(tmp_path: Path) -> ScheduledEnv:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.jsonl"
    fixture = tmp_path / "gh-fixture.json"
    git_config = tmp_path / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    repositories: list[tuple[str, Path, str]] = []
    for name, github in (
        ("EaseWise", "qiaozhang1225/EaseWise"),
        ("codex-mac-worker", "qiaozhang1225/codex-mac-worker"),
    ):
        remote = tmp_path / "remotes" / f"{name}.git"
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(git_config),
                f"url.{remote.as_uri()}.insteadOf",
                f"https://github.com/{github}.git",
            ],
            check=True,
        )
        repo, commit = _create_repository(tmp_path, name, github, git_env)
        repositories.append((github, repo, commit))

    config = tmp_path / "repositories.toml"
    config.write_text(
        f'''schema_version = 1
max_parallel_tasks = 3
poll_interval_minutes = 10

[[repositories]]
github = "qiaozhang1225/EaseWise"
local_path = "{repositories[0][1]}"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "{repositories[1][1]}"
''',
        encoding="utf-8",
    )
    issue_body = VALID_BODY.replace("a" * 40, repositories[0][2])
    fixture.write_text(
        json.dumps(
            {
                "list_calls": 0,
                "issues": [
                    {
                        "repo": repositories[0][0],
                        "url": "https://github.com/qiaozhang1225/EaseWise/issues/7",
                        "createdAt": "2026-07-20T00:00:00Z",
                        "body": issue_body,
                        "labels": ["duomac:ready"],
                        "comments": [],
                        "state": "OPEN",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    real_git = shutil.which("git")
    assert real_git is not None
    gh = bin_dir / "gh"
    gh.write_text(
        '''#!/usr/bin/env python3
import fcntl
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
stdin = sys.stdin.read()
fixture_path = Path(os.environ["GH_FAKE_FIXTURE"])
lock_path = fixture_path.with_suffix(".lock")
log_path = Path(os.environ["GH_FAKE_LOG"])

def locked_state(mutator=None):
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        value = json.loads(fixture_path.read_text(encoding="utf-8"))
        result = mutator(value) if mutator else None
        if mutator:
            temporary = fixture_path.with_suffix(f".tmp.{os.getpid()}")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            temporary.replace(fixture_path)
        return value, result

with log_path.open("a", encoding="utf-8") as stream:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    stream.write(json.dumps({"argv": args, "stdin": stdin}) + "\\n")

failure = os.environ.get("GH_FAKE_FAIL_COMMAND")
if failure == " ".join(args[:2]):
    print(os.environ.get("GH_FAKE_FAILURE_DETAIL", "injected gh failure"), file=sys.stderr)
    raise SystemExit(5)

def issue_for(value, url):
    return next(item for item in value["issues"] if item["url"] == url)

if args[:2] == ["issue", "list"]:
    repo = args[args.index("--repo") + 1]
    label = args[args.index("--label") + 1]
    def count(value):
        value["list_calls"] = value.get("list_calls", 0) + 1
        return value["list_calls"]
    value, call_number = locked_state(count)
    target = int(os.environ.get("GH_FAKE_BARRIER_TARGET", "0"))
    if target and call_number <= target:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current, _ = locked_state()
            if current.get("list_calls", 0) >= target:
                break
            time.sleep(0.01)
        else:
            print("fake gh barrier timeout", file=sys.stderr)
            raise SystemExit(6)
    value, _ = locked_state()
    matches = [
        {
            "url": item["url"],
            "createdAt": item["createdAt"],
            "body": item["body"],
            "labels": [{"name": name} for name in item["labels"]],
        }
        for item in value["issues"]
        if item["repo"].casefold() == repo.casefold()
        and item["state"] == "OPEN"
        and label in item["labels"]
    ]
    print(json.dumps(matches))
elif args[:2] == ["issue", "view"]:
    value, _ = locked_state()
    issue = issue_for(value, args[2])
    field = args[args.index("--json") + 1]
    if field == "body":
        print(json.dumps({"body": issue["body"]}))
    elif field == "comments":
        print(json.dumps({"comments": issue["comments"]}))
    elif field == "labels":
        print(json.dumps({"labels": [{"name": name} for name in issue["labels"]]}))
    elif field == "state":
        print(json.dumps({"state": issue["state"]}))
    elif field == "body,state,labels,comments":
        def authority_mutation(value):
            value["snapshot_calls"] = value.get("snapshot_calls", 0) + 1
            mutation = os.environ.get("GH_FAKE_AUTHORITY_MUTATION")
            if value["snapshot_calls"] != 2 or not mutation:
                return
            selected = issue_for(value, args[2])
            if mutation == "body":
                selected["body"] += "\\n<!-- authority-window edit -->\\n"
            elif mutation == "labels":
                selected["labels"] = []
            elif mutation == "state":
                selected["state"] = "CLOSED"
            elif mutation == "terminal":
                selected["comments"].append({
                    "id": "IC_authority_terminal",
                    "createdAt": "2026-07-20T00:00:02Z",
                    "body": "<!-- duomac-event:v1 -->\\n```yaml\\ntype: blocked\\nrevision: 2\\nreason: Authority-window terminal event\\ncompleted: []\\nnext:\\n  - Publish a new revision\\n```\\n",
                })
            elif mutation == "progress":
                selected["comments"].append({
                    "id": "IC_authority_progress",
                    "createdAt": "2026-07-20T00:00:02Z",
                    "body": "<!-- duomac-event:v1 -->\\n```yaml\\ntype: checkpoint\\nrevision: 2\\nmilestone: 1\\ncompleted:\\n  - Raced progress\\ncommits:\\n  - bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\nverification:\\n  - 'pytest -q: passed'\\nscope_status: within-scope\\nnext:\\n  - Stop stale claim\\nblockers: []\\n```\\n",
                })
        value, _ = locked_state(authority_mutation)
        issue = issue_for(value, args[2])
        print(json.dumps({
            "body": issue["body"],
            "state": issue["state"],
            "labels": [{"name": name} for name in issue["labels"]],
            "comments": issue["comments"],
        }))
    else:
        print("unsupported issue view field", file=sys.stderr)
        raise SystemExit(2)
elif args[:2] == ["issue", "comment"]:
    def comment(value):
        issue = issue_for(value, args[2])
        issue["comments"].append(
            {
                "id": f"IC_{len(issue['comments']) + 1}",
                "createdAt": "2026-07-20T00:00:01Z",
                "body": stdin,
            }
        )
    locked_state(comment)
elif args[:2] == ["issue", "edit"]:
    def edit(value):
        issue = issue_for(value, args[2])
        index = 3
        while index < len(args):
            if args[index] == "--remove-label":
                label = args[index + 1]
                issue["labels"] = [item for item in issue["labels"] if item != label]
                index += 2
            elif args[index] == "--add-label":
                label = args[index + 1]
                if label not in issue["labels"]:
                    issue["labels"].append(label)
                index += 2
            else:
                index += 1
    locked_state(edit)
elif args[:2] == ["issue", "close"]:
    def close(value):
        issue_for(value, args[2])["state"] = "CLOSED"
    locked_state(close)
else:
    print("unsupported fake gh command", file=sys.stderr)
    raise SystemExit(2)
''',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    git = bin_dir / "git"
    git.write_text(
        f'''#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

result = subprocess.run(
    [{real_git!r}, *sys.argv[1:]],
    input=sys.stdin.read(),
    text=True,
    capture_output=True,
    check=False,
)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
mutation = os.environ.get("GH_FAKE_GIT_MUTATION")
if result.returncode == 0 and mutation and "fetch" in sys.argv[1:]:
    fixture_path = Path(os.environ["GH_FAKE_FIXTURE"])
    lock_path = fixture_path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        value = json.loads(fixture_path.read_text(encoding="utf-8"))
        if not value.get("git_mutated"):
            issue = value["issues"][0]
            if mutation == "body":
                issue["body"] += "\\n<!-- raced body edit -->\\n"
            elif mutation == "labels":
                issue["labels"] = []
            elif mutation == "cancel":
                issue["labels"] = ["duomac:cancelled"]
            elif mutation == "blocked_label":
                issue["labels"] = ["duomac:blocked"]
            elif mutation == "state":
                issue["state"] = "CLOSED"
            elif mutation in {{"claim", "terminal"}}:
                if mutation == "claim":
                    context = re.search(r"commit:\\s+([0-9a-f]{{40}})", issue["body"]).group(1)
                    body_hash = hashlib.sha256(issue["body"].encode("utf-8")).hexdigest()
                    payload = f"""type: task-start
revision: 2
task_hash: {{body_hash}}
repository: {{issue['repo']}}
base_branch: main
context_commit: {{context}}
skill_commit: {{'d' * 40}}
base_commit: {{context}}
plan_summary:
  - Raced claim
execution_mode: scheduled
slot: 3
claim_id: {{'e' * 40}}"""
                else:
                    payload = """type: blocked
revision: 2
reason: Raced terminal event
completed: []
next:
  - Publish a new revision"""
                issue["comments"].append({{
                    "id": "IC_race",
                    "createdAt": "2026-07-20T00:00:00Z",
                    "body": f"<!-- duomac-event:v1 -->\\n```yaml\\n{{payload}}\\n```\\n",
                }})
            elif mutation in {{"capacity", "path_conflict"}}:
                count = 3 if mutation == "capacity" else 1
                active_repo = (
                    "qiaozhang1225/codex-mac-worker"
                    if mutation == "capacity"
                    else issue["repo"]
                )
                for index in range(count):
                    active_body = issue["body"]
                    context = re.search(r"commit:\\s+([0-9a-f]{{40}})", active_body).group(1)
                    body_hash = hashlib.sha256(active_body.encode("utf-8")).hexdigest()
                    payload = f"""type: task-start
revision: 2
task_hash: {{body_hash}}
repository: {{active_repo}}
base_branch: main
context_commit: {{context}}
skill_commit: {{'d' * 40}}
base_commit: {{context}}
plan_summary:
  - Concurrent active work
execution_mode: scheduled
slot: {{index + 1}}
claim_id: a{{format(index + 1, '039x')}}"""
                    value["issues"].append({{
                        "repo": active_repo,
                        "url": f"https://github.com/{{active_repo}}/issues/{{100 + index}}",
                        "createdAt": f"2026-07-20T00:00:0{{index + 1}}Z",
                        "body": active_body,
                        "labels": ["duomac:active"],
                        "comments": [{{
                            "id": f"IC_active_{{index}}",
                            "createdAt": "2026-07-20T00:00:01Z",
                            "body": f"<!-- duomac-event:v1 -->\\n```yaml\\n{{payload}}\\n```\\n",
                        }}],
                        "state": "OPEN",
                    }})
            value["git_mutated"] = True
            temporary = fixture_path.with_suffix(f".tmp.{{os.getpid()}}")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            temporary.replace(fixture_path)
raise SystemExit(result.returncode)
''',
        encoding="utf-8",
    )
    git.chmod(0o755)
    app_root = tmp_path / "app"
    app_root.mkdir()
    env = {
        **git_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_FAKE_LOG": str(log),
        "GH_FAKE_FIXTURE": str(fixture),
    }
    return ScheduledEnv(env, config, app_root, fixture, log)


def test_picker_preview_has_no_github_writes(scheduled_env: ScheduledEnv) -> None:
    sentinel = scheduled_env.app_root / "sentinel.txt"
    sentinel.write_bytes(b"unchanged\x00bytes")
    before = {
        path.relative_to(scheduled_env.app_root): path.read_bytes()
        for path in scheduled_env.app_root.rglob("*")
        if path.is_file()
    }

    result = scheduled_env.run_picker("--slot", "1")

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["claimed"] is False
    assert output["reason"] == "preview"
    assert not scheduled_env.github_writes()
    after = {
        path.relative_to(scheduled_env.app_root): path.read_bytes()
        for path in scheduled_env.app_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_picker_preview_does_not_create_missing_application_root(
    scheduled_env: ScheduledEnv,
) -> None:
    missing = scheduled_env.app_root.parent / "missing-app-root"
    env = replace(scheduled_env, app_root=missing)

    result = env.run_picker("--slot", "1")

    assert result.returncode == 0, result.stderr
    assert not missing.exists()


def test_picker_claims_oldest_eligible_issue_once(scheduled_env: ScheduledEnv) -> None:
    first = scheduled_env.run_picker("--slot", "1", "--yes")
    second = scheduled_env.run_picker("--slot", "2", "--yes")

    assert first.returncode == 0, first.stderr
    first_output = json.loads(first.stdout)
    assert first_output["claimed"] is True
    assert first_output["slot"] == 1
    assert len(first_output["claim_id"]) == 40
    assert json.loads(second.stdout)["claimed"] is False
    assert len(scheduled_env.task_start_comments()) == 1
    assert len(scheduled_env.claim_files()) == 1
    issue = scheduled_env.state()["issues"][0]
    task_start = parse_issue_events(tuple(issue["comments"]))[0].payload
    spec = parse_issue_body(issue["body"])
    expected = {
        "task_hash": hashlib.sha256(issue["body"].encode("utf-8")).hexdigest(),
        "repository": issue["repo"],
        "base_branch": "main",
        "context_commit": spec.context_commit,
        "skill_commit": _git(ROOT, "rev-parse", "HEAD"),
        "base_commit": _git(scheduled_env.repository_path(), "rev-parse", "origin/main"),
        "revision": spec.revision,
        "execution_mode": "scheduled",
        "slot": 1,
        "claim_id": first_output["claim_id"],
    }
    assert {field: task_start[field] for field in expected} == expected
    local_claim = json.loads(scheduled_env.claim_files()[0].read_text(encoding="utf-8"))
    assert {field: local_claim[field] for field in expected} == expected
    assert [call["argv"][:2] for call in scheduled_env.github_writes()[:2]] == [
        ["issue", "comment"],
        ["issue", "edit"],
    ]
    all_calls = [
        json.loads(line) for line in scheduled_env.log.read_text().splitlines()
    ]
    comment_index = next(
        index
        for index, call in enumerate(all_calls)
        if call["argv"][:2] == ["issue", "comment"]
    )
    assert all_calls[comment_index - 1]["argv"][-1] == "body,state,labels,comments"


def test_picker_blocks_ready_schema_v1(scheduled_env: ScheduledEnv) -> None:
    scheduled_env.replace_ready_body(LEGACY_BODY)

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["claimed"] is False
    assert scheduled_env.current_label() == "duomac:blocked"
    assert len(scheduled_env.blocked_comments()) == 1


def test_picker_repairs_active_label_after_task_start_comment(
    scheduled_env: ScheduledEnv,
) -> None:
    claim_id = "c" * 40
    payload = scheduled_env.scheduled_start(claim_id=claim_id)
    scheduled_env.add_comment(event_comment(payload))
    scheduled_env.set_labels(["duomac:ready"])

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert scheduled_env.current_label() == "duomac:active"
    assert len(scheduled_env.task_start_comments()) == 1
    assert len(scheduled_env.claim_files()) == 1
    local_claim = json.loads(scheduled_env.claim_files()[0].read_text(encoding="utf-8"))
    assert all(local_claim[field] == payload[field] for field in (
        "task_hash",
        "repository",
        "base_branch",
        "context_commit",
        "skill_commit",
        "base_commit",
        "revision",
        "execution_mode",
        "slot",
        "claim_id",
    ))


def test_picker_repairs_missing_local_claim_for_active_issue(
    scheduled_env: ScheduledEnv,
) -> None:
    first = scheduled_env.run_picker("--slot", "1", "--yes")
    assert first.returncode == 0, first.stderr
    scheduled_env.claim_files()[0].unlink()

    second = scheduled_env.run_picker("--slot", "2", "--yes")

    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["claimed"] is False
    assert len(scheduled_env.claim_files()) == 1
    assert len(scheduled_env.task_start_comments()) == 1


def test_picker_counts_interactive_active_issue_without_local_scheduled_claim(
    scheduled_env: ScheduledEnv,
) -> None:
    payload = valid_task_start()
    payload["execution_mode"] = "interactive"
    payload.pop("slot")
    payload.pop("claim_id")
    for field in ("task_hash", "repository", "base_branch", "context_commit"):
        payload.pop(field)
    scheduled_env.add_comment(event_comment(payload))
    scheduled_env.set_labels(["duomac:active"])

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"claimed": False, "reason": "no-ready"}
    assert not scheduled_env.claim_files()


def test_picker_rejects_invalid_slot_identity(scheduled_env: ScheduledEnv) -> None:
    result = scheduled_env.run_picker("--slot", "4", "--yes")

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"claimed": False, "reason": "error"}
    assert not scheduled_env.github_writes()


def test_picker_blocks_repository_remote_mismatch(scheduled_env: ScheduledEnv) -> None:
    _git(
        scheduled_env.repository_path(),
        "config",
        "remote.origin.url",
        "https://github.com/other/repository.git",
    )

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "claimed": False,
        "reason": "invalid-candidates-blocked",
    }
    assert scheduled_env.current_label() == "duomac:blocked"
    assert len(scheduled_env.task_start_comments()) == 0


def test_comment_failure_is_not_a_claim_and_redacts_error_detail(
    scheduled_env: ScheduledEnv,
) -> None:
    scheduled_env.env["GH_FAKE_FAIL_COMMAND"] = "issue comment"
    scheduled_env.env["GH_FAKE_FAILURE_DETAIL"] = "token ghp_supersecret"

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 1
    assert json.loads(result.stdout) == {"claimed": False, "reason": "error"}
    assert "ghp_supersecret" not in result.stdout + result.stderr
    assert scheduled_env.current_label() == "duomac:ready"
    assert not scheduled_env.task_start_comments()
    assert not scheduled_env.claim_files()


def test_label_failure_after_task_start_preserves_and_repairs_authoritative_claim(
    scheduled_env: ScheduledEnv,
) -> None:
    scheduled_env.env["GH_FAKE_FAIL_COMMAND"] = "issue edit"

    first = scheduled_env.run_picker("--slot", "1", "--yes")

    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["claimed"] is True
    assert scheduled_env.current_label() == "duomac:ready"
    assert len(scheduled_env.task_start_comments()) == 1
    assert len(scheduled_env.claim_files()) == 1

    scheduled_env.env.pop("GH_FAKE_FAIL_COMMAND")
    second = scheduled_env.run_picker("--slot", "2", "--yes")

    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["claimed"] is False
    assert scheduled_env.current_label() == "duomac:active"
    assert len(scheduled_env.task_start_comments()) == 1
    assert len(scheduled_env.claim_files()) == 1


def test_two_slots_cannot_claim_the_same_issue(scheduled_env: ScheduledEnv) -> None:
    results = scheduled_env.run_two_pickers_concurrently(1, 2)

    assert all(result.returncode == 0 for result in results)
    assert sum(json.loads(result.stdout)["claimed"] for result in results) == 1
    assert len(scheduled_env.task_start_comments()) == 1


@pytest.mark.parametrize(
    "mutation",
    ["body", "labels", "state", "cancel", "blocked_label", "claim", "terminal"],
)
def test_picker_revalidates_selected_issue_after_repository_validation(
    scheduled_env: ScheduledEnv, mutation: str
) -> None:
    scheduled_env.env["GH_FAKE_GIT_MUTATION"] = mutation

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["claimed"] is False
    picker_comments = [
        call for call in scheduled_env.github_writes()
        if call["argv"][:2] == ["issue", "comment"]
    ]
    assert not picker_comments
    assert not scheduled_env.claim_files()
    expected_external_starts = 1 if mutation == "claim" else 0
    assert len(scheduled_env.task_start_comments()) == expected_external_starts


@pytest.mark.parametrize(
    "mutation", ["body", "labels", "state", "terminal", "progress"]
)
def test_authority_snapshot_rejects_final_window_issue_races(
    scheduled_env: ScheduledEnv, mutation: str
) -> None:
    scheduled_env.env["GH_FAKE_AUTHORITY_MUTATION"] = mutation

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["claimed"] is False
    assert not scheduled_env.task_start_comments()
    assert not scheduled_env.claim_files()
    assert not any(
        call["argv"][:2] == ["issue", "comment"]
        for call in scheduled_env.github_writes()
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [("capacity", "parallel-limit"), ("path_conflict", "path-conflict")],
)
def test_picker_refreshes_global_active_state_after_repository_validation(
    scheduled_env: ScheduledEnv, mutation: str, reason: str
) -> None:
    scheduled_env.env["GH_FAKE_GIT_MUTATION"] = mutation

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"claimed": False, "reason": reason}
    assert not scheduled_env.task_start_comments()
    assert not scheduled_env.claim_files()
    assert not any(
        call["argv"][:2] == ["issue", "comment"]
        for call in scheduled_env.github_writes()
    )
