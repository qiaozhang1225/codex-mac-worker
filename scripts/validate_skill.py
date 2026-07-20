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
    repositories = value["repositories"]
    if not isinstance(repositories, list) or len(repositories) != 2:
        raise SkillValidationError(
            "repositories.toml.example must contain two repositories"
        )
    for repository in repositories:
        if (
            not isinstance(repository, dict)
            or set(repository) != {"github", "local_path"}
            or not all(
                isinstance(repository[field], str) and repository[field]
                for field in ("github", "local_path")
            )
        ):
            raise SkillValidationError(
                "repositories.toml.example entries must contain github and local_path"
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
