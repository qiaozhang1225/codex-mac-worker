from __future__ import annotations

from typing import Any


MANUAL = "manual"
AUTOMATIC = "automatic"
MERGE_MODES = frozenset({MANUAL, AUTOMATIC})
RULESET_NAME = "Codex Worker Default Branch"


def ruleset_payload(profile: str = MANUAL) -> dict[str, Any]:
    if profile not in MERGE_MODES:
        raise ValueError(f"unknown Ruleset profile: {profile}")
    automatic = profile == AUTOMATIC
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        },
        "bypass_actors": [
            {
                "actor_id": 5,
                "actor_type": "RepositoryRole",
                "bypass_mode": "pull_request",
            }
        ],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "update"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash"],
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": not automatic,
                    "required_approving_review_count": 0 if automatic else 1,
                    "required_review_thread_resolution": True,
                },
            },
        ],
    }


def _security_fields(payload: dict[str, Any]) -> tuple[Any, ...] | None:
    bypass = payload.get("bypass_actors")
    if not isinstance(bypass, list):
        return None
    normalized_bypass: list[tuple[Any, Any, Any]] = []
    for actor in bypass:
        if not isinstance(actor, dict) or actor.get("actor_type") == "Integration":
            return None
        normalized_bypass.append(
            (actor.get("actor_type"), actor.get("actor_id"), actor.get("bypass_mode"))
        )

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        return None
    rules = {
        item.get("type"): item
        for item in raw_rules
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    }
    required_types = frozenset({"deletion", "non_fast_forward", "update", "pull_request"})
    if not required_types.issubset(rules):
        return None
    pull_request = rules["pull_request"].get("parameters")
    if not isinstance(pull_request, dict):
        return None

    conditions = payload.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(ref_name, dict):
        return None
    return (
        payload.get("name"),
        payload.get("target"),
        payload.get("enforcement"),
        tuple(ref_name.get("include", [])),
        tuple(ref_name.get("exclude", [])),
        tuple(normalized_bypass),
        tuple(sorted(required_types)),
        tuple(pull_request.get("allowed_merge_methods", [])),
        pull_request.get("dismiss_stale_reviews_on_push"),
        pull_request.get("require_code_owner_review"),
        pull_request.get("require_last_push_approval"),
        pull_request.get("required_approving_review_count"),
        pull_request.get("required_review_thread_resolution"),
    )


def classify_ruleset(payload: dict[str, Any]) -> str | None:
    observed = _security_fields(payload)
    if observed is None:
        return None
    for profile in (MANUAL, AUTOMATIC):
        if observed == _security_fields(ruleset_payload(profile)):
            return profile
    return None
