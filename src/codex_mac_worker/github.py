from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import jwt


API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class GitHubAppAuth:
    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str,
        private_key_path: Path,
        transport: httpx.BaseTransport | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_path = private_key_path
        self._client = httpx.Client(base_url=api_url, transport=transport, timeout=30)
        self._cached_token: str | None = None
        self._expires_at: datetime | None = None

    def app_jwt(self) -> str:
        now = datetime.now(UTC)
        private_key = self.private_key_path.read_text(encoding="utf-8")
        return jwt.encode(
            {
                "iat": int((now - timedelta(seconds=60)).timestamp()),
                "exp": int((now + timedelta(minutes=9)).timestamp()),
                "iss": self.app_id,
            },
            private_key,
            algorithm="RS256",
        )

    def installation_token(self) -> str:
        now = datetime.now(UTC)
        if (
            self._cached_token
            and self._expires_at
            and self._expires_at - now > timedelta(minutes=2)
        ):
            return self._cached_token
        response = self._client.post(
            f"/app/installations/{self.installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.app_jwt()}",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if response.status_code != 201:
            raise GitHubError(
                f"unable to mint installation token: {response.status_code}",
                status_code=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        payload = response.json()
        self._cached_token = str(payload["token"])
        self._expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        return self._cached_token


class GitHubClient:
    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._token_provider = token_provider
        self._client = httpx.Client(base_url=api_url, transport=transport, timeout=30)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token_provider()}",
            "X-GitHub-Api-Version": API_VERSION,
        }
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(str(exc), status_code=None, retryable=True) from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except (json.JSONDecodeError, AttributeError):
                message = response.text
            raise GitHubError(
                f"GitHub API {response.status_code}: {message}",
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def list_queued_issues(self, repo: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{repo}/issues",
            params={"state": "open", "labels": "codex:queued", "per_page": 100},
        )

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/issues/{issue_number}")

    def list_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{repo}/issues/{issue_number}/comments",
            params={"per_page": 100},
        )

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/repos/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )

    def set_labels(self, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/repos/{repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )

    def collaborator_permission(self, repo: str, username: str) -> str:
        payload = self._request(
            "GET",
            f"/repos/{repo}/collaborators/{username}/permission",
        )
        return str(payload.get("permission", "none"))

    def create_draft_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "draft": True},
        )

    def find_open_pull_request(self, repo: str, branch: str) -> dict[str, Any] | None:
        owner = repo.split("/", 1)[0]
        pulls = self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}", "per_page": 10},
        )
        return pulls[0] if pulls else None

    def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/pulls/{pr_number}")

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )

    def update_issue(
        self,
        repo: str,
        issue_number: int,
        *,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if labels is not None:
            payload["labels"] = labels
        if state is not None:
            payload["state"] = state
        return self._request("PATCH", f"/repos/{repo}/issues/{issue_number}", json=payload)
