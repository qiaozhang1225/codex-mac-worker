#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib

import yaml


EXPECTED_REFERENCES = {
    "checkpoints.md",
    "git-delivery.md",
    "issue-protocol.md",
    "roles-and-delegation.md",
    "scheduled-execution.md",
}
EXPECTED_ASSETS = {
    "repositories.toml.example",
    "scheduled-slot-prompt.md",
}
EXPECTED_SCRIPTS = {
    "config_validate.py",
    "git_deliver.py",
    "git_preflight.py",
    "issue_checkpoint.py",
    "issue_complete.py",
    "issue_create.py",
    "issue_validate.py",
    "scheduled_pick.py",
}
EXPECTED_REPOSITORIES = [
    {
        "github": "qiaozhang1225/EaseWise",
        "local_path": "/Users/qiaoz-macmini/EaseWise",
    },
    {
        "github": "qiaozhang1225/codex-mac-worker",
        "local_path": "/Users/qiaoz-macmini/codex-mac-worker",
    },
]
SCHEDULED_REFERENCE_BOUNDARIES = (
    "visible Mac mini Codex App Scheduled run",
    "Dual Mac Slot 1",
    "Dual Mac Slot 2",
    "Dual Mac Slot 3",
    "claim at most one Issue",
    "same visible Codex App Scheduled task",
    "checkpoints are not approval gates",
    "AGENTS.md",
    "every Issue-declared context file",
    "frozen context commit",
    "Mac mini never creates an Issue automatically",
    "Goal",
    "codex exec",
    "LaunchDaemon Worker",
)
SCHEDULED_PROMPT_BOUNDARIES = (
    "$dual-mac-collaboration",
    "Dual Mac Slot 1",
    "Dual Mac Slot 2",
    "Dual Mac Slot 3",
    "Claim at most one Issue",
    "same visible Scheduled task",
    "Checkpoints are evidence, not approval gates",
    "AGENTS.md",
    "every Issue-declared context file",
    "frozen context commit",
    "Do not create another Issue",
    "Do not use Goal",
    "codex exec",
    "daemon",
)
CONTRADICTORY_SCHEDULED_PHRASES = (
    "Use Goal and",
    "Use `codex exec`",
    "Create Issues automatically",
    "Wait for checkpoint approval",
    "Another Slot may resume",
)
SKILL_DOCUMENT = re.compile(r"---\n(.*?)\n---\n(.*)", re.DOTALL)
REFERENCE_LINK = re.compile(r"references/([a-z0-9-]+\.md)")
SCRIPT_HELP = re.compile(r"scripts/([a-z0-9_]+\.py) --help")


class SkillValidationError(ValueError):
    pass


def _mapping(path: Path) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SkillValidationError(f"unable to read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillValidationError(f"YAML must contain a mapping: {path}")
    return value


def _required_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise SkillValidationError(f"required skill file is missing: {relative}")
    return path


def _validate_repository_example(path: Path) -> None:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SkillValidationError(f"invalid repositories.toml.example: {exc}") from exc
    if set(value) != {
        "schema_version",
        "max_parallel_tasks",
        "poll_interval_minutes",
        "repositories",
    }:
        raise SkillValidationError("repositories.toml.example has unexpected fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise SkillValidationError("repositories.toml.example schema_version must be 1")
    if (
        type(value["max_parallel_tasks"]) is not int
        or value["max_parallel_tasks"] != 3
        or type(value["poll_interval_minutes"]) is not int
        or value["poll_interval_minutes"] != 10
    ):
        raise SkillValidationError(
            "repositories.toml.example must use the approved Slot limits"
        )
    if value["repositories"] != EXPECTED_REPOSITORIES:
        raise SkillValidationError(
            "repositories.toml.example must contain the exact approved repositories"
        )


def _validate_scheduled_content(skill_root: Path) -> None:
    reference_path = skill_root / "references" / "scheduled-execution.md"
    prompt_path = skill_root / "assets" / "scheduled-slot-prompt.md"
    try:
        reference = reference_path.read_text(encoding="utf-8")
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillValidationError(f"unable to read Scheduled content: {exc}") from exc
    for label, text, boundaries in (
        ("Scheduled reference", reference, SCHEDULED_REFERENCE_BOUNDARIES),
        ("Scheduled prompt", prompt, SCHEDULED_PROMPT_BOUNDARIES),
    ):
        if not text.strip():
            raise SkillValidationError(f"{label} must be non-empty")
        missing = [boundary for boundary in boundaries if boundary not in text]
        if missing:
            raise SkillValidationError(
                f"{label} is missing canonical boundary: {missing[0]}"
            )
        contradictory = [
            phrase for phrase in CONTRADICTORY_SCHEDULED_PHRASES if phrase in text
        ]
        if contradictory:
            raise SkillValidationError(
                f"{label} contains contradictory instruction: {contradictory[0]}"
            )


def validate_skill(root: Path, wrapper_targets: tuple[str, ...]) -> None:
    skill_root = root.expanduser().resolve()
    skill_path = _required_file(skill_root, "SKILL.md")
    try:
        skill_text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillValidationError(f"unable to read SKILL.md: {exc}") from exc
    match = SKILL_DOCUMENT.fullmatch(skill_text)
    if match is None:
        raise SkillValidationError("SKILL.md must contain one YAML frontmatter block")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"invalid SKILL.md frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict) or set(frontmatter) != {"name", "description"}:
        raise SkillValidationError(
            "SKILL.md frontmatter must contain only name and description"
        )
    if frontmatter.get("name") != "dual-mac-collaboration":
        raise SkillValidationError("SKILL.md has the wrong skill name")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.startswith("Use when"):
        raise SkillValidationError("SKILL.md description must start with 'Use when'")

    body = match.group(2)
    references = set(REFERENCE_LINK.findall(body))
    if references != EXPECTED_REFERENCES:
        raise SkillValidationError(
            "SKILL.md reference set does not match required references"
        )
    scripts = set(SCRIPT_HELP.findall(body))
    if scripts != EXPECTED_SCRIPTS:
        raise SkillValidationError("SKILL.md script help set does not match required scripts")
    for name in sorted(references):
        _required_file(skill_root, f"references/{name}")
    for name in sorted(EXPECTED_ASSETS):
        _required_file(skill_root, f"assets/{name}")
    _validate_repository_example(
        skill_root / "assets" / "repositories.toml.example"
    )
    _validate_scheduled_content(skill_root)
    for name in sorted(scripts):
        _required_file(skill_root, f"scripts/{name}")

    metadata = _mapping(_required_file(skill_root, "agents/openai.yaml"))
    interface = metadata.get("interface")
    if not isinstance(interface, dict):
        raise SkillValidationError("agents/openai.yaml interface must be a mapping")
    for field in ("display_name", "short_description", "default_prompt"):
        value = interface.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SkillValidationError(
                f"agents/openai.yaml interface.{field} must be non-empty"
            )
    if "$dual-mac-collaboration" not in interface["default_prompt"]:
        raise SkillValidationError("agents/openai.yaml default_prompt must name the skill")

    if not wrapper_targets:
        raise SkillValidationError("at least one installer wrapper target is required")
    if len(set(wrapper_targets)) != len(wrapper_targets):
        raise SkillValidationError("installer wrapper targets must be unique")
    for target in wrapper_targets:
        if Path(target).name != target or target not in EXPECTED_SCRIPTS:
            raise SkillValidationError(f"invalid installer wrapper target: {target}")
        _required_file(skill_root, f"scripts/{target}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a staged dual-mac-collaboration skill"
    )
    parser.add_argument("--skill-root", required=True, type=Path)
    parser.add_argument("--wrapper-target", action="append", default=[])
    args = parser.parse_args()
    try:
        validate_skill(args.skill_root, tuple(args.wrapper_target))
    except SkillValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("dual-mac-collaboration skill is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
