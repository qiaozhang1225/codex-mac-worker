from __future__ import annotations

import pytest

from codex_mac_worker.references import parse_issue_reference, parse_pull_request_reference


def test_references_accept_urls_and_short_forms() -> None:
    issue = parse_issue_reference("https://github.com/owner/repo/issues/12")
    short_issue = parse_issue_reference("owner/repo#12")
    pull = parse_pull_request_reference("https://github.com/owner/repo/pull/44")
    short_pull = parse_pull_request_reference("owner/repo#44")

    assert (issue.repo, issue.number) == ("owner/repo", 12)
    assert short_issue == issue
    assert (pull.repo, pull.number) == ("owner/repo", 44)
    assert short_pull == pull


@pytest.mark.parametrize(
    "reference",
    (
        "",
        "owner/repo",
        "owner/repo#0",
        "https://example.com/owner/repo/issues/12",
        "https://github.com/owner/repo/pull/not-a-number",
    ),
)
def test_references_reject_invalid_values(reference: str) -> None:
    with pytest.raises(ValueError, match="reference must be"):
        parse_issue_reference(reference)
