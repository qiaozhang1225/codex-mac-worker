from __future__ import annotations

import hashlib

import pytest

from codex_mac_worker.protocol import (
    DeliveryMetadata,
    ProtocolError,
    parse_delivery_block,
    parse_repository_attestation,
    parse_repository_probe,
    parse_task_body,
    render_repository_attestation,
    render_delivery_block,
    render_repository_probe,
)


VALID_SHA = "a" * 40


def task_body(*, sha: str = VALID_SHA, risk: str = "low") -> str:
    return f"""Human summary.

<!-- codex-task:v1 -->
```yaml
schema_version: 1
context_commit: {sha}
base_branch: main
objective: Add a bounded worker feature
acceptance:
  - Unit tests pass
context_files:
  - docs/spec.md
allowed_paths:
  - src/
  - tests/
verification_profile: fast
risk: {risk}
```
"""


def test_parse_task_body_returns_frozen_spec_and_hash() -> None:
    spec = parse_task_body(task_body())

    assert spec.context_commit == VALID_SHA
    assert spec.objective == "Add a bounded worker feature"
    assert spec.acceptance == ("Unit tests pass",)
    assert spec.allowed_paths == ("src/", "tests/")
    assert spec.task_hash == hashlib.sha256(spec.canonical_yaml.encode()).hexdigest()


def test_parse_task_body_rejects_truncated_commit() -> None:
    with pytest.raises(ProtocolError, match="40-character"):
        parse_task_body(task_body(sha="abc123"))


def test_parse_task_body_rejects_multiple_machine_blocks() -> None:
    body = task_body() + "\n" + task_body()

    with pytest.raises(ProtocolError, match="exactly one"):
        parse_task_body(body)


def test_parse_task_body_rejects_empty_acceptance() -> None:
    body = task_body().replace("  - Unit tests pass", "  []")

    with pytest.raises(ProtocolError, match="acceptance"):
        parse_task_body(body)


def test_repository_probe_round_trip_binds_default_head_and_config_hash() -> None:
    body = render_repository_probe(
        probe_id="probe-1",
        default_head="a" * 40,
        project_config_hash="b" * 64,
    )

    probe = parse_repository_probe(body)

    assert probe.probe_id == "probe-1"
    assert probe.default_head == "a" * 40
    assert probe.project_config_hash == "b" * 64


def test_repository_attestation_round_trip_records_worker_identity() -> None:
    body = render_repository_attestation(
        probe_id="probe-1",
        worker_id="mac-mini-01",
        default_head="a" * 40,
        project_config_hash="b" * 64,
        attested_at="2026-07-14T10:00:00+00:00",
    )

    attestation = parse_repository_attestation(body)

    assert attestation.worker_id == "mac-mini-01"
    assert attestation.attested_at == "2026-07-14T10:00:00+00:00"
    assert attestation.default_head == "a" * 40


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("a" * 40, "short", "default_head"),
        ("b" * 64, "short", "project_config_hash"),
    ],
)
def test_repository_probe_rejects_invalid_hashes(old: str, new: str, message: str) -> None:
    body = render_repository_probe(
        probe_id="probe-1",
        default_head="a" * 40,
        project_config_hash="b" * 64,
    ).replace(old, new)

    with pytest.raises(ProtocolError, match=message):
        parse_repository_probe(body)


def test_delivery_metadata_round_trip_binds_latest_commit() -> None:
    metadata = DeliveryMetadata(
        issue_number=12,
        task_hash="b" * 64,
        context_commit="a" * 40,
        delivery_commit="c" * 40,
        verification_profile="fast",
        verification_passed=True,
        model="gpt-5",
        cli_version="codex 1.2.3",
        acceptance_results=(
            {"criterion": "Tests pass", "status": "met", "evidence": "pytest"},
        ),
        risks=(),
        needs_human=(),
    )

    assert parse_delivery_block(render_delivery_block(metadata)) == metadata
