from pathlib import Path

import yaml


SKILL = Path(__file__).parents[1] / "skills" / "dispatch-codex-task"


def test_dispatch_skill_defines_principal_agent_bounded_delegation() -> None:
    skill_path = SKILL / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)

    assert metadata["name"] == "dispatch-codex-task"
    assert str(metadata["description"]).startswith("Use when")
    assert set(metadata) == {"name", "description"}
    for required in (
        "principal development agent",
        "strict subset of the authorized parent objective",
        "active path ownership",
        "git status",
        "git rev-parse",
        "git push",
        "codexctl task create --yes",
        "allowed_paths",
        "acceptance",
        "refuse",
        "confirmation",
        "Goal",
        'merge_mode = "automatic"',
        "merge",
        "codexctl repo status",
        "codexctl repo onboard",
        "codexctl repo finalize",
        "codexctl task review",
        "codexctl task merge",
        "expected-head",
        "expected-fingerprint",
        "head SHA",
        "explicit",
        "automatic Ruleset",
    ):
        assert required in body

    assert "Run only after explicit confirmation of that final specification" not in body
    assert "Mac mini cannot further delegate" in body
    assert "production" in body

    agent = yaml.safe_load((SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    assert "$dispatch-codex-task" in agent["interface"]["default_prompt"]
    assert "principal" in agent["interface"]["short_description"].lower()
