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
