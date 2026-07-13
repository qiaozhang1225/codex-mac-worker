from __future__ import annotations

import json

import codex_mac_worker.cli as cli
from codex_mac_worker.cli import build_ctl_parser, ctl_main, personal_github_token
from codex_mac_worker.repository_onboarding import OnboardingSnapshot, ReadinessReport


def test_ctl_parser_supports_task_review() -> None:
    args = build_ctl_parser().parse_args(
        ["task", "review", "https://github.com/owner/repo/issues/12"]
    )

    assert (args.resource, args.action) == ("task", "review")


def test_task_review_returns_two_when_gates_block(monkeypatch, capsys) -> None:
    from codex_mac_worker.assisted_merge import GateResult, ReviewSnapshot

    monkeypatch.setattr(cli, "personal_github_token", lambda: "token")
    monkeypatch.setattr(cli, "GitHubClient", lambda **kwargs: object())
    snapshot = ReviewSnapshot(
        repo="owner/repo", issue_number=12, pr_number=44, pr_url="https://example/pr/44",
        base_branch="main", base_sha="a" * 40, head_sha="c" * 40, is_draft=True,
        task_hash="b" * 64, context_commit="a" * 40, changed_paths=("src/a.py",),
        additions=1, deletions=0, checks=(), acceptance_results=(), model=None,
        cli_version=None, risks=(), needs_human=(), unresolved_threads=(),
        gates=GateResult(False, ("checks pending",)), approval_fingerprint="d" * 64,
    )
    monkeypatch.setattr(cli, "review_task", lambda github, reference: snapshot)

    assert ctl_main(["task", "review", "owner/repo#12"]) == 2
    assert json.loads(capsys.readouterr().out)["gates"]["allowed"] is False


def test_ctl_parser_requires_full_head_for_task_merge() -> None:
    parser = build_ctl_parser()
    args = parser.parse_args(
        ["task", "merge", "owner/repo#12", "--expected-head", "c" * 40]
    )

    assert args.expected_head == "c" * 40


def test_task_merge_uses_local_operation_ledger(monkeypatch, tmp_path, capsys) -> None:
    from codex_mac_worker.assisted_merge import MergeResult

    monkeypatch.setenv("CODEXCTL_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(cli, "personal_github_token", lambda: "token")
    monkeypatch.setattr(cli, "GitHubClient", lambda **kwargs: object())
    seen: dict = {}

    def fake_merge(github, state, reference, *, expected_head):
        seen.update(reference=reference, expected_head=expected_head)
        return MergeResult(
            repo=reference.repo,
            issue_number=reference.number,
            pr_number=44,
            approved_head=expected_head,
            merge_commit_sha="e" * 40,
            actor_login="qiaoz",
            approval_fingerprint="f" * 64,
            merged=True,
        )

    monkeypatch.setattr(cli, "merge_task", fake_merge)

    assert ctl_main(
        ["task", "merge", "owner/repo#12", "--expected-head", "c" * 40]
    ) == 0
    assert seen["reference"].number == 12
    assert json.loads(capsys.readouterr().out)["merged"] is True


def test_personal_token_prefers_environment(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token-from-env")

    assert personal_github_token() == "token-from-env"


def test_ctl_parser_supports_create_status_and_control_commands() -> None:
    parser = build_ctl_parser()

    create = parser.parse_args(
        ["task", "create", "--repo", "owner/repo", "--spec", "task.yaml", "--yes"]
    )
    status = parser.parse_args(["task", "status", "owner/repo#12"])
    pause = parser.parse_args(["task", "pause", "owner/repo#12"])

    assert create.action == "create"
    assert create.title is None
    assert create.yes is True
    assert status.action == "status"
    assert pause.action == "pause"


def test_ctl_parser_supports_repository_lifecycle() -> None:
    parser = build_ctl_parser()
    onboard = parser.parse_args(
        ["repo", "onboard", "--repo", "owner/repo", "--adopt-pr", "1"]
    )
    status = parser.parse_args(["repo", "status", "owner/repo"])
    finalize = parser.parse_args(
        [
            "repo",
            "finalize",
            "https://github.com/owner/repo/pull/1",
            "--expected-head",
            "a" * 40,
        ]
    )

    assert (onboard.resource, onboard.action, onboard.adopt_pr) == ("repo", "onboard", 1)
    assert status.action == "status"
    assert finalize.expected_head == "a" * 40


def test_repo_status_prints_structured_readiness(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "personal_github_token", lambda: "token")
    monkeypatch.setattr(cli, "GitHubClient", lambda **kwargs: object())
    monkeypatch.setattr(
        cli,
        "repository_status",
        lambda github, repo: ReadinessReport(
            repo=repo,
            phase="ready",
            default_branch="main",
            default_head="a" * 40,
            files_valid=True,
            labels_valid=True,
            ruleset_valid=True,
            worker_attested=True,
            worker_login="worker[bot]",
            blockers=(),
        ),
    )

    assert ctl_main(["repo", "status", "owner/repo"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "ready"


def test_repo_onboard_adopts_existing_pr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "personal_github_token", lambda: "token")
    monkeypatch.setattr(cli, "GitHubClient", lambda **kwargs: object())
    snapshot = OnboardingSnapshot(
        repo="owner/repo",
        pr_number=1,
        url="https://github.com/owner/repo/pull/1",
        base_branch="main",
        base_sha="a" * 40,
        head_branch="codex/onboard-worker",
        head_sha="b" * 40,
        changed_paths=(".codex-worker/project.toml",),
        project_config_hash="c" * 64,
        is_draft=True,
        mergeable=True,
    )
    seen: dict = {}

    def fake_prepare(github, repo, **kwargs):
        seen.update({"repo": repo, **kwargs})
        return snapshot

    monkeypatch.setattr(cli, "prepare_onboarding", fake_prepare)

    assert ctl_main(["repo", "onboard", "--repo", "owner/repo", "--adopt-pr", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["head_sha"] == "b" * 40
    assert seen["adopt_pr"] == 1


def test_repo_finalize_uses_local_operation_ledger(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("CODEXCTL_STATE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(cli, "personal_github_token", lambda: "token")
    monkeypatch.setattr(cli, "GitHubClient", lambda **kwargs: object())
    seen: dict = {}

    def fake_finalize(github, state, reference, *, expected_head):
        seen.update({"reference": reference, "expected_head": expected_head})
        return ReadinessReport(
            repo=reference.repo,
            phase="awaiting-worker",
            default_branch="main",
            default_head="c" * 40,
            files_valid=True,
            labels_valid=True,
            ruleset_valid=True,
            worker_attested=False,
            worker_login=None,
            blockers=("worker attestation pending",),
        )

    monkeypatch.setattr(cli, "finalize_onboarding", fake_finalize)

    assert ctl_main([
        "repo", "finalize", "https://github.com/owner/repo/pull/1",
        "--expected-head", "b" * 40,
    ]) == 0
    assert seen["reference"].number == 1
    assert seen["expected_head"] == "b" * 40
    assert json.loads(capsys.readouterr().out)["phase"] == "awaiting-worker"
