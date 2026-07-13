from pathlib import Path

import yaml


SKILL = Path(__file__).parents[1] / "skills" / "dispatch-codex-task"


def test_dispatch_skill_enforces_human_confirmed_bounded_tasks() -> None:
    skill_path = SKILL / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)

    assert metadata["name"] == "dispatch-codex-task"
    assert str(metadata["description"]).startswith("Use when")
    assert set(metadata) == {"name", "description"}
    for required in (
        "git status",
        "git rev-parse",
        "git push",
        "codexctl task create",
        "allowed_paths",
        "acceptance",
        "refuse",
        "confirmation",
        "Goal",
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
        "future PR",
        "automatic merge",
    ):
        assert required in body

    assert "设计" in body
    assert "看起来可以" in body

    agent = yaml.safe_load((SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    assert "$dispatch-codex-task" in agent["interface"]["default_prompt"]
    assert "review" in agent["interface"]["short_description"].lower()
