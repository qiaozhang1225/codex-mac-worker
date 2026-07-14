from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class IssueReference:
    repo: str
    number: int


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    repo: str
    number: int


def _parse(reference: str, resource: str) -> tuple[str, int]:
    url = re.fullmatch(
        rf"https://github\.com/([^/]+/[^/]+)/{resource}/(\d+)/?",
        reference,
    )
    short = re.fullmatch(r"([^/]+/[^#]+)#(\d+)", reference)
    match = url or short
    if not match:
        raise ValueError(f"reference must be a GitHub {resource} URL or owner/repo#number")
    number = int(match.group(2))
    if number <= 0:
        raise ValueError(f"reference must be a GitHub {resource} URL or owner/repo#number")
    return match.group(1), number


def parse_issue_reference(reference: str) -> IssueReference:
    repo, number = _parse(reference, "issues")
    return IssueReference(repo, number)


def parse_pull_request_reference(reference: str) -> PullRequestReference:
    repo, number = _parse(reference, "pull")
    return PullRequestReference(repo, number)
