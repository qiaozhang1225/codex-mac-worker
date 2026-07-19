from __future__ import annotations

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "dual-mac-collaboration"
REQUIRED_REFERENCES = (
    "roles-and-delegation.md",
    "issue-protocol.md",
    "checkpoints.md",
    "git-delivery.md",
)
REQUIRED_SCRIPTS = (
    "issue_validate.py",
    "issue_create.py",
    "issue_checkpoint.py",
    "issue_complete.py",
    "git_preflight.py",
    "git_deliver.py",
)
FORBIDDEN_ACTIVE = (
    "worker_github_app_id",
    "readiness attestation",
    "merge_mode",
    "approval fingerprint",
    "codex exec",
    "LaunchDaemon",
)


def skill_parts() -> tuple[dict[str, object], str]:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match is not None
    metadata = yaml.safe_load(match.group(1))
    assert isinstance(metadata, dict)
    return metadata, match.group(2)


def test_frontmatter_has_only_name_and_use_when_description() -> None:
    metadata, _ = skill_parts()

    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "dual-mac-collaboration"
    assert isinstance(metadata["description"], str)
    assert metadata["description"].startswith("Use when")


def test_skill_routes_to_all_required_references() -> None:
    _, body = skill_parts()

    for name in REQUIRED_REFERENCES:
        assert f"references/{name}" in body
        assert (SKILL_ROOT / "references" / name).is_file()


def test_references_do_not_chain_to_other_references() -> None:
    for name in REQUIRED_REFERENCES:
        text = (SKILL_ROOT / "references" / name).read_text(encoding="utf-8")
        assert "references/" not in text
        assert re.search(r"\]\([^)]*\.md(?:#[^)]*)?\)", text) is None


def test_skill_discovers_every_public_script_via_help() -> None:
    _, body = skill_parts()

    for name in REQUIRED_SCRIPTS:
        assert f"scripts/{name} --help" in body


def test_skill_contains_required_hard_boundaries() -> None:
    _, body = skill_parts()

    required_phrases = (
        "explicit user confirmation",
        "Issue body is the only current task contract",
        "continue without MacBook approval",
        "Never force push",
        "product decision",
        "visible Codex App",
        "direct-main",
        "task-branch",
    )
    for phrase in required_phrases:
        assert phrase in body


def test_skill_tree_has_no_legacy_requirements_or_template_markers() -> None:
    texts = [
        path.read_text(encoding="utf-8")
        for path in [SKILL_ROOT / "SKILL.md", *(SKILL_ROOT / "references").glob("*.md")]
        if path.exists()
    ]
    joined = "\n".join(texts)

    for term in FORBIDDEN_ACTIVE:
        assert term not in joined
    assert "TODO" not in joined
    assert len((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()) < 500


def test_openai_metadata_names_the_skill() -> None:
    metadata = yaml.safe_load(
        (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )

    assert metadata["interface"]["display_name"] == "双 Mac Codex 协作"
    assert "GitHub Issue" in metadata["interface"]["short_description"]
    assert "$dual-mac-collaboration" in metadata["interface"]["default_prompt"]
