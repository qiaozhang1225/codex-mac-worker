from __future__ import annotations

import json
from typing import Any

from .protocol import TaskSpec


def result_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "summary",
            "changed_files",
            "risks",
            "needs_human",
            "acceptance_results",
        ],
        "properties": {
            "status": {"type": "string", "enum": ["completed", "blocked"]},
            "summary": {"type": "string", "minLength": 1},
            "changed_files": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "needs_human": {"type": "array", "items": {"type": "string"}},
            "acceptance_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "status", "evidence"],
                    "properties": {
                        "criterion": {"type": "string", "minLength": 1},
                        "status": {
                            "type": "string",
                            "enum": ["met", "not_met", "needs_review"],
                        },
                        "evidence": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    }


def _task_contract(spec: TaskSpec, issue_number: int | None = None) -> str:
    issue = f"GitHub issue: #{issue_number}\n" if issue_number is not None else ""
    return f"""{issue}Context commit: {spec.context_commit}
Objective: {spec.objective}

Acceptance criteria:
{chr(10).join(f'- {item}' for item in spec.acceptance)}

Required context files:
{chr(10).join(f'- {item}' for item in spec.context_files)}

Only modify these paths:
{chr(10).join(f'- {item}' for item in spec.allowed_paths)}
"""


def build_execution_prompt(spec: TaskSpec, *, issue_number: int) -> str:
    return f"""Execute one bounded repository task. Do not broaden or reinterpret the objective.

{_task_contract(spec, issue_number)}

Safety boundaries:
- Do not commit, push, open a pull request, deploy, or merge.
- Do not change Git HEAD, repository policy, workflow files, credentials, or production systems.
- Do not access files outside the supplied worktree.
- Stop and report blocked if the task cannot be completed inside the allowed paths.
- Finish this single attempt; do not start a persistent objective or schedule follow-up work.

Run focused local checks when useful. The worker will run the authoritative verification profile.
Return only the structured final result required by the supplied JSON Schema.
"""


def build_revision_prompt(spec: TaskSpec, requirements: str, current_diff: str) -> str:
    return f"""Start a new bounded attempt to revise an existing task branch.

{_task_contract(spec)}

Revision requirements:
{requirements}

Current diff summary:
{current_diff}

Do not commit, push, open a pull request, deploy, or merge. Do not broaden the original objective.
Only address the explicit revision requirements inside the allowed paths, then return the structured result.
"""


def write_result_schema(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result_schema(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
