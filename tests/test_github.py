from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt

from codex_mac_worker.github import GitHubAppAuth, GitHubClient, GitHubError


def generate_private_key(path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def test_app_auth_builds_short_lived_jwt(tmp_path: Path) -> None:
    key_path = tmp_path / "app.pem"
    generate_private_key(key_path)
    auth = GitHubAppAuth(app_id="123", installation_id="456", private_key_path=key_path)

    token = auth.app_jwt()
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == "123"
    assert 0 < claims["exp"] - claims["iat"] <= 600


def test_installation_token_is_cached_until_near_expiry(tmp_path: Path) -> None:
    key_path = tmp_path / "app.pem"
    generate_private_key(key_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            201,
            json={
                "token": "installation-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        )

    auth = GitHubAppAuth(
        app_id="123",
        installation_id="456",
        private_key_path=key_path,
        transport=httpx.MockTransport(handler),
    )

    assert auth.installation_token() == "installation-token"
    assert auth.installation_token() == "installation-token"
    assert calls == 1


def test_client_classifies_retryable_and_permission_errors() -> None:
    responses = iter(
        [
            httpx.Response(503, json={"message": "unavailable"}),
            httpx.Response(403, json={"message": "forbidden"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    try:
        client.list_queued_issues("owner/repo")
    except GitHubError as exc:
        assert exc.retryable is True
        assert exc.status_code == 503
    else:
        raise AssertionError("expected retryable GitHubError")

    try:
        client.list_queued_issues("owner/repo")
    except GitHubError as exc:
        assert exc.retryable is False
        assert exc.status_code == 403
    else:
        raise AssertionError("expected permission GitHubError")


def test_client_sends_expected_issue_and_pr_requests() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = None
        if request.content:
            payload = __import__("json").loads(request.content)
        seen.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 44, "html_url": "https://example/pr/44"})
        if request.method == "POST":
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(200, json=[])

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    client.add_comment("owner/repo", 12, "status")
    pr = client.create_draft_pr("owner/repo", "codex/12-test", "main", "Title", "Body")

    assert pr["number"] == 44
    assert seen[0] == ("POST", "/repos/owner/repo/issues/12/comments", {"body": "status"})
    assert seen[1][2]["draft"] is True


def test_client_reads_pull_request_merge_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/pulls/44"
        return httpx.Response(
            200,
            json={"number": 44, "merged_at": "2026-07-13T01:00:00Z"},
        )

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_pull_request("owner/repo", 44)["merged_at"] is not None


def test_client_paginates_files_and_sends_expected_sha_on_merge() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/files"):
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(
                    200,
                    json=[{"filename": f"src/first-{index}.py"} for index in range(100)],
                )
            if page == 2:
                return httpx.Response(200, json=[{"filename": "src/last.py"}])
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "b" * 40})
        raise AssertionError(request.url)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    files = client.list_pull_files("owner/repo", 44)
    assert len(files) == 101
    assert files[0]["filename"] == "src/first-0.py"
    assert files[-1]["filename"] == "src/last.py"
    result = client.merge_pull_request("owner/repo", 44, expected_head="a" * 40)

    assert result["merged"] is True
    assert seen[-1][2] == {"merge_method": "squash", "sha": "a" * 40}


def test_client_reads_repository_files_installations_checks_and_rulesets() -> None:
    import base64

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents/.codex-worker/project.toml"):
            assert request.url.params["ref"] == "main"
            encoded = base64.b64encode(b"schema_version = 1\n").decode()
            return httpx.Response(200, json={"content": encoded, "encoding": "base64"})
        if request.url.path == "/installation/repositories":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "owner/repo"}] if page == 1 else []},
            )
        if request.url.path.endswith("/check-runs"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(
                200,
                json={"check_runs": [{"name": "tests"}] if page == 1 else []},
            )
        if request.url.path.endswith("/rulesets"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=[{"id": 7}] if page == 1 else [])
        raise AssertionError(request.url)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_repository_file(
        "owner/repo", ".codex-worker/project.toml", ref="main"
    ) == "schema_version = 1\n"
    assert client.list_installation_repositories() == [{"full_name": "owner/repo"}]
    assert client.list_check_runs("owner/repo", "a" * 40) == [{"name": "tests"}]
    assert client.list_rulesets("owner/repo") == [{"id": 7}]


def test_client_upserts_labels_and_updates_pull_requests() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.method == "GET" and request.url.path.endswith(("/labels/codex%3Aqueued", "/labels/codex:queued")):
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "POST" and request.url.path.endswith("/labels"):
            return httpx.Response(201, json=payload)
        if request.method == "PATCH" and request.url.path.endswith("/pulls/44"):
            return httpx.Response(200, json={"number": 44, **(payload or {})})
        raise AssertionError(request.url)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    label = client.upsert_label("owner/repo", "codex:queued", "1f6feb", "Queued")
    pull = client.update_pull_request("owner/repo", 44, body="new")

    assert label["name"] == "codex:queued"
    assert pull["body"] == "new"
    assert seen[-1][2] == {"body": "new"}


def test_client_reads_paginated_review_threads() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == "/graphql"
        variables = __import__("json").loads(request.content)["variables"]
        calls += 1
        if calls == 1:
            assert variables["cursor"] is None
            nodes = [
                {
                    "isResolved": False,
                    "comments": {"nodes": [{"url": "https://example/thread-1"}]},
                }
            ]
            page_info = {"hasNextPage": True, "endCursor": "next"}
        else:
            assert variables["cursor"] == "next"
            nodes = [
                {
                    "isResolved": True,
                    "comments": {"nodes": [{"url": "https://example/thread-2"}]},
                }
            ]
            page_info = {"hasNextPage": False, "endCursor": None}
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": nodes, "pageInfo": page_info}
                        }
                    }
                }
            },
        )

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    threads = client.list_review_threads("owner/repo", 44)

    assert [item["isResolved"] for item in threads] == [False, True]
    assert calls == 2


def test_client_reads_repository_user_commit_labels_and_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"full_name": "owner/repo"})
        if path == "/user":
            return httpx.Response(200, json={"login": "owner"})
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if path.endswith("/labels"):
            return httpx.Response(200, json=[])
        if path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": []})
        raise AssertionError(request.url)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    assert client.get_repository("owner/repo")["full_name"] == "owner/repo"
    assert client.get_authenticated_user()["login"] == "owner"
    assert client.get_commit("owner/repo", "main")["sha"] == "a" * 40
    assert client.list_labels("owner/repo") == []
    assert client.get_combined_status("owner/repo", "a" * 40)["state"] == "success"


def test_client_lists_reviews_marks_ready_and_creates_approval() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            assert request.url.params["state"] == "open"
            assert request.url.params["head"] == "owner:codex/12-test"
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path.endswith("/pulls/44/reviews"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path.endswith("/pulls/44"):
            return httpx.Response(200, json={"number": 44, "node_id": "PR_node"})
        if request.url.path == "/graphql":
            return httpx.Response(
                200,
                json={"data": {"markPullRequestReadyForReview": {
                    "pullRequest": {"id": "PR_node", "number": 44, "isDraft": False}
                }}},
            )
        if request.method == "POST" and request.url.path.endswith("/pulls/44/reviews"):
            return httpx.Response(200, json={"state": "APPROVED"})
        raise AssertionError(request.url)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )

    assert client.list_pull_requests(
        "owner/repo", head="owner:codex/12-test"
    ) == []
    assert client.list_reviews("owner/repo", 44) == []
    assert client.mark_pull_request_ready("owner/repo", 44)["isDraft"] is False
    assert client.create_pull_review("owner/repo", 44, body="Approved")["state"] == "APPROVED"
    assert seen[-1][2] == {"body": "Approved", "event": "APPROVE"}


def test_client_gets_creates_and_updates_rulesets() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.method == "GET":
            return httpx.Response(200, json={"id": 7, "name": "Codex"})
        if request.method == "POST":
            return httpx.Response(201, json={"id": 7, **(payload or {})})
        if request.method == "PUT":
            return httpx.Response(200, json={"id": 7, **(payload or {})})
        raise AssertionError(request.url)

    client = GitHubClient(
        token_provider=lambda: "token",
        transport=httpx.MockTransport(handler),
    )
    payload = {"name": "Codex", "target": "branch"}

    assert client.get_ruleset("owner/repo", 7)["name"] == "Codex"
    assert client.create_ruleset("owner/repo", payload)["id"] == 7
    assert client.update_ruleset("owner/repo", 7, payload)["target"] == "branch"
    assert seen[-1] == ("PUT", "/repos/owner/repo/rulesets/7", payload)
