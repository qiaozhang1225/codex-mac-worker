from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import quote

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
        self._client = httpx.Client(
            base_url=api_url,
            transport=transport,
            timeout=30,
            trust_env=False,
        )
        self._cached_token: str | None = None
        self._expires_at: datetime | None = None
        self._deadline_monotonic: float | None = None

    @contextmanager
    def request_deadline(self, deadline_monotonic: float):
        previous = self._deadline_monotonic
        self._deadline_monotonic = (
            deadline_monotonic
            if previous is None
            else min(previous, deadline_monotonic)
        )
        try:
            yield
        finally:
            self._deadline_monotonic = previous

    def _request_timeout(self) -> float | None:
        if self._deadline_monotonic is None:
            return None
        remaining = self._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise GitHubError(
                "GitHub request deadline exceeded",
                status_code=None,
                retryable=False,
            )
        return min(30.0, remaining)

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
        request_options: dict[str, Any] = {}
        timeout = self._request_timeout()
        if timeout is not None:
            request_options["timeout"] = timeout
        response = self._client.post(
            f"/app/installations/{self.installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.app_jwt()}",
                "X-GitHub-Api-Version": API_VERSION,
            },
            **request_options,
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
        self._client = httpx.Client(
            base_url=api_url,
            transport=transport,
            timeout=30,
            trust_env=False,
        )
        self._deadline_monotonic: float | None = None

    @contextmanager
    def request_deadline(self, deadline_monotonic: float):
        previous = self._deadline_monotonic
        self._deadline_monotonic = (
            deadline_monotonic
            if previous is None
            else min(previous, deadline_monotonic)
        )
        provider_owner = getattr(self._token_provider, "__self__", None)
        provider_scope = getattr(provider_owner, "request_deadline", None)
        scope = (
            provider_scope(self._deadline_monotonic)
            if provider_scope is not None
            else nullcontext()
        )
        try:
            with scope:
                yield
        finally:
            self._deadline_monotonic = previous

    def _request_timeout(self) -> float | None:
        if self._deadline_monotonic is None:
            return None
        remaining = self._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise GitHubError(
                "GitHub request deadline exceeded",
                status_code=None,
                retryable=False,
            )
        return min(30.0, remaining)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token_provider()}",
            "X-GitHub-Api-Version": API_VERSION,
        }
        timeout = self._request_timeout()
        try:
            if timeout is not None:
                kwargs["timeout"] = timeout
            response = self._client.request(
                method,
                path,
                headers=headers,
                **kwargs,
            )
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

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        list_key: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            page_params = dict(params or {})
            page_params.update({"per_page": 100, "page": page})
            payload = self._request("GET", path, params=page_params)
            items = payload.get(list_key, []) if list_key is not None else payload
            if not isinstance(items, list):
                raise GitHubError(
                    f"GitHub API returned invalid pagination data for {path}",
                    status_code=None,
                    retryable=False,
                )
            result.extend(item for item in items if isinstance(item, dict))
            if len(items) < 100:
                return result
            page += 1

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/graphql",
            json={"query": query, "variables": variables},
        )
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise GitHubError(
                f"GitHub GraphQL error: {errors}",
                status_code=None,
                retryable=False,
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise GitHubError(
                "GitHub GraphQL response did not contain data",
                status_code=None,
                retryable=False,
            )
        return data

    def get_repository(self, repo: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}")

    def get_authenticated_user(self) -> dict[str, Any]:
        return self._request("GET", "/user")

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        encoded_path = quote(path, safe="/")
        payload = self._request(
            "GET",
            f"/repos/{repo}/contents/{encoded_path}",
            params={"ref": ref},
        )
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise GitHubError(
                f"repository file {path} did not contain base64 content",
                status_code=None,
                retryable=False,
            )
        content = payload.get("content")
        if not isinstance(content, str):
            raise GitHubError(
                f"repository file {path} did not contain text content",
                status_code=None,
                retryable=False,
            )
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubError(
                f"repository file {path} is not valid UTF-8 base64 content",
                status_code=None,
                retryable=False,
            ) from exc

    def list_installation_repositories(self) -> list[dict[str, Any]]:
        return self._paginate("/installation/repositories", list_key="repositories")

    def get_commit(self, repo: str, ref: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/commits/{quote(ref, safe='')}")

    def list_queued_issues(self, repo: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{repo}/issues",
            params={"state": "open", "labels": "codex:queued", "per_page": 100},
        )

    def list_issues(
        self,
        repo: str,
        *,
        state: str = "open",
        labels: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state}
        if labels is not None:
            params["labels"] = labels
        return self._paginate(f"/repos/{repo}/issues", params=params)

    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/issues/{issue_number}")

    def list_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        return self._paginate(
            f"/repos/{repo}/issues/{issue_number}/comments",
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

    def list_labels(self, repo: str) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{repo}/labels")

    def get_label(self, repo: str, name: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/labels/{quote(name, safe='')}")

    def upsert_label(
        self,
        repo: str,
        name: str,
        color: str,
        description: str,
    ) -> dict[str, Any]:
        payload = {"name": name, "color": color, "description": description}
        try:
            self.get_label(repo, name)
        except GitHubError as exc:
            if exc.status_code != 404:
                raise
            return self._request("POST", f"/repos/{repo}/labels", json=payload)
        return self._request(
            "PATCH",
            f"/repos/{repo}/labels/{quote(name, safe='')}",
            json=payload,
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

    def list_pull_requests(
        self,
        repo: str,
        *,
        state: str = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state}
        if head is not None:
            params["head"] = head
        if base is not None:
            params["base"] = base
        return self._paginate(f"/repos/{repo}/pulls", params=params)

    def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/pulls/{pr_number}")

    def update_pull_request(
        self,
        repo: str,
        pr_number: int,
        *,
        body: str | None = None,
        title: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
        if not payload:
            raise ValueError("pull request update requires at least one field")
        return self._request("PATCH", f"/repos/{repo}/pulls/{pr_number}", json=payload)

    def mark_pull_request_ready(self, repo: str, pr_number: int) -> dict[str, Any]:
        pull = self.get_pull_request(repo, pr_number)
        node_id = pull.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise GitHubError(
                "pull request does not contain a GraphQL node ID",
                status_code=None,
                retryable=False,
            )
        data = self.graphql(
            """mutation MarkReady($pullRequestId: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                pullRequest { id number isDraft }
              }
            }""",
            {"pullRequestId": node_id},
        )
        return data["markPullRequestReadyForReview"]["pullRequest"]

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{repo}/pulls/{pr_number}/files")

    def list_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{repo}/pulls/{pr_number}/reviews")

    def create_pull_review(
        self,
        repo: str,
        pr_number: int,
        *,
        body: str,
        event: str = "APPROVE",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            json={"body": body, "event": event},
        )

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        owner, name = repo.split("/", 1)
        query = """query ReviewThreads(
          $owner: String!, $name: String!, $number: Int!, $cursor: String
        ) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $cursor) {
                nodes {
                  isResolved
                  comments(first: 1) { nodes { url } }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }"""
        cursor: str | None = None
        threads: list[dict[str, Any]] = []
        while True:
            data = self.graphql(
                query,
                {"owner": owner, "name": name, "number": pr_number, "cursor": cursor},
            )
            try:
                connection = data["repository"]["pullRequest"]["reviewThreads"]
                nodes = connection["nodes"]
                page_info = connection["pageInfo"]
            except (KeyError, TypeError) as exc:
                raise GitHubError(
                    "GitHub GraphQL review thread response was incomplete",
                    status_code=None,
                    retryable=False,
                ) from exc
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise GitHubError(
                    "GitHub GraphQL review thread response was invalid",
                    status_code=None,
                    retryable=False,
                )
            threads.extend(item for item in nodes if isinstance(item, dict))
            if not page_info.get("hasNextPage"):
                return threads
            cursor_value = page_info.get("endCursor")
            if not isinstance(cursor_value, str) or not cursor_value:
                raise GitHubError(
                    "GitHub GraphQL review thread cursor was missing",
                    status_code=None,
                    retryable=False,
                )
            cursor = cursor_value

    def list_check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return self._paginate(
            f"/repos/{repo}/commits/{sha}/check-runs",
            list_key="check_runs",
        )

    def get_combined_status(self, repo: str, sha: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/commits/{sha}/status")

    def list_rulesets(self, repo: str) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{repo}/rulesets")

    def get_ruleset(self, repo: str, ruleset_id: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo}/rulesets/{ruleset_id}")

    def create_ruleset(self, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/repos/{repo}/rulesets", json=payload)

    def update_ruleset(
        self,
        repo: str,
        ruleset_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/repos/{repo}/rulesets/{ruleset_id}",
            json=payload,
        )

    def merge_pull_request(
        self,
        repo: str,
        pr_number: int,
        *,
        expected_head: str,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/repos/{repo}/pulls/{pr_number}/merge",
            json={"merge_method": "squash", "sha": expected_head},
        )

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
