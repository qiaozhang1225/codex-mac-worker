from __future__ import annotations

import hashlib
import json
from typing import Any

from .store import EventStore


class DurableGitHub:
    """Write-through GitHub proxy backed by a durable SQLite outbox."""

    def __init__(self, remote: Any, store: EventStore) -> None:
        self.remote = remote
        self.store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self.remote, name)

    def _key(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "github:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _write(self, payload: dict[str, Any]) -> dict[str, Any]:
        outbox_id = self.store.enqueue_outbox("github", payload, self._key(payload))
        existing = self.store.get_outbox(outbox_id)
        if existing and existing["delivered_at"]:
            remote_id = existing.get("remote_id")
            return {"id": int(remote_id)} if remote_id and str(remote_id).isdigit() else {}
        if existing and existing.get("failed_at"):
            raise RuntimeError(f"outbox delivery permanently failed: {existing.get('last_error', '')}")
        try:
            result = self._deliver(payload)
        except Exception as exc:
            self.store.record_outbox_failure(
                outbox_id,
                str(exc),
                retryable=bool(getattr(exc, "retryable", True)),
            )
            raise
        remote_id = result.get("id") or result.get("number")
        self.store.mark_outbox_delivered(
            outbox_id,
            remote_id=str(remote_id) if remote_id is not None else None,
        )
        return result

    def _deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload["operation"]
        if operation == "add_comment":
            comments = self.remote.list_comments(payload["repo"], payload["issue_number"])
            for comment in comments:
                if comment.get("body") == payload["body"]:
                    return comment
            return self.remote.add_comment(
                payload["repo"], payload["issue_number"], payload["body"]
            )
        if operation == "update_comment":
            return self.remote.update_comment(
                payload["repo"], payload["comment_id"], payload["body"]
            )
        if operation == "set_labels":
            return self.remote.set_labels(
                payload["repo"], payload["issue_number"], payload["labels"]
            )
        if operation == "create_draft_pr":
            finder = getattr(self.remote, "find_open_pull_request", None)
            if finder is not None:
                existing = finder(payload["repo"], payload["head"])
                if existing is not None:
                    return existing
            return self.remote.create_draft_pr(
                payload["repo"],
                payload["head"],
                payload["base"],
                payload["title"],
                payload["body"],
            )
        if operation == "update_issue":
            return self.remote.update_issue(
                payload["repo"],
                payload["issue_number"],
                labels=payload.get("labels"),
                state=payload.get("state"),
            )
        if operation == "update_pull_request":
            return self.remote.update_pull_request(
                payload["repo"],
                payload["pr_number"],
                body=payload["body"],
            )
        raise ValueError(f"unsupported durable GitHub operation: {operation}")

    def flush(self) -> None:
        for item in self.store.pending_outbox():
            if item["kind"] != "github":
                continue
            try:
                result = self._deliver(item["payload"])
            except Exception as exc:
                self.store.record_outbox_failure(
                    item["id"],
                    str(exc),
                    retryable=bool(getattr(exc, "retryable", True)),
                )
                raise
            remote_id = result.get("id") or result.get("number")
            self.store.mark_outbox_delivered(
                item["id"],
                remote_id=str(remote_id) if remote_id is not None else None,
            )

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return self._write(
            {"operation": "add_comment", "repo": repo, "issue_number": issue_number, "body": body}
        )

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        return self._write(
            {"operation": "update_comment", "repo": repo, "comment_id": comment_id, "body": body}
        )

    def set_labels(self, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        return self._write(
            {
                "operation": "set_labels",
                "repo": repo,
                "issue_number": issue_number,
                "labels": labels,
            }
        )

    def create_draft_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        return self._write(
            {
                "operation": "create_draft_pr",
                "repo": repo,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            }
        )

    def update_issue(
        self,
        repo: str,
        issue_number: int,
        *,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        return self._write(
            {
                "operation": "update_issue",
                "repo": repo,
                "issue_number": issue_number,
                "labels": labels,
                "state": state,
            }
        )

    def update_pull_request(
        self,
        repo: str,
        pr_number: int,
        *,
        body: str,
    ) -> dict[str, Any]:
        return self._write(
            {
                "operation": "update_pull_request",
                "repo": repo,
                "pr_number": pr_number,
                "body": body,
            }
        )
