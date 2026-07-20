# Scheduled Clean-Noop Auto-Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically archive only clean Scheduled no-op runs while preserving every real task, maintenance event, error, preview, and blocked run.

**Architecture:** Keep standalone Scheduled tasks and their 10-minute cadence. Encode one explicit outcome-to-archive matrix in the tracked prompt and Scheduled reference, then make the repository validator and tests reject any future broadening from `clean-noop` to other outcomes.

**Tech Stack:** Markdown skill assets and references, Python 3.12, pytest, Codex App `set_thread_archived`.

## Global Constraints

- Do not change Slot names, cadence, project, model, reasoning effort, or active/paused state.
- Call `set_thread_archived` with `archived: true` and no `threadId` only after a valid `clean-noop` result is reported.
- Never automatically archive `maintenance`, `preview`, `error`, `claimed`, or blocked runs.
- Do not edit Codex App internal files or databases.

---

### Task 1: Enforce Clean-Noop-Only Archiving

**Files:**
- Modify: `skills/dual-mac-collaboration/assets/scheduled-slot-prompt.md`
- Modify: `skills/dual-mac-collaboration/references/scheduled-execution.md`
- Modify: `scripts/validate_skill.py`
- Modify: `tests/test_skill_content.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-20-scheduled-dual-mac-execution-design.md`

**Interfaces:**
- Consumes: picker JSON field `outcome` and the Codex App `set_thread_archived` action.
- Produces: one tested archive policy in which only `outcome: clean-noop` archives the calling Scheduled thread.

- [ ] **Step 1: Add the failing policy test**

Add a test that reads the prompt, Scheduled reference, and README and requires all three to name `set_thread_archived`, `archived: true`, clean-noop-only archiving, and preservation of `maintenance`, `preview`, `error`, `claimed`, and blocked runs.

```python
def test_scheduled_archives_only_clean_noop_runs() -> None:
    texts = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (SKILL_ROOT / "references" / "scheduled-execution.md").read_text(
            encoding="utf-8"
        ),
        (SKILL_ROOT / "assets" / "scheduled-slot-prompt.md").read_text(
            encoding="utf-8"
        ),
    ]
    required = (
        "Automatically archive only a valid `clean-noop`",
        "`set_thread_archived` with `archived: true` and no `threadId`",
        "Keep `maintenance`, `preview`, `error`, `claimed`, and blocked runs visible",
    )

    for text in texts:
        for phrase in required:
            assert phrase in text
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_skill_content.py::test_scheduled_archives_only_clean_noop_runs
```

Expected: FAIL because the current prompt says to archive all non-execution runs.

- [ ] **Step 3: Implement the minimal policy**

Replace the broad archive sentence in the Scheduled prompt. Require the calling run to report a valid `clean-noop`, call `set_thread_archived` with `archived: true` and no `threadId`, then stop. State separately that all other outcomes remain unarchived. Mirror the same matrix in the Scheduled reference and README, and add the exact boundaries to `scripts/validate_skill.py`.

Use these exact policy sentences in all three human-readable surfaces:

```markdown
Automatically archive only a valid `clean-noop`: after reporting its exact `reason` and `maintenance_actions: []`, call `set_thread_archived` with `archived: true` and no `threadId`, then stop.

Keep `maintenance`, `preview`, `error`, `claimed`, and blocked runs visible; never auto-archive them.
```

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
.venv/bin/pytest -q tests/test_skill_content.py::test_scheduled_archives_only_clean_noop_runs
.venv/bin/pytest -q
python scripts/validate_skill.py --skill-root skills/dual-mac-collaboration --wrapper-target scheduled_pick.py
```

Expected: all commands exit 0.

- [ ] **Step 5: Forward-test both branches**

Use fresh-context agents with the updated prompt. A `maintenance` result must remain visible; a valid `clean-noop` result must report the exact result, archive only the calling thread, and stop without code execution.

- [ ] **Step 6: Review, commit, publish, and install**

Review the diff against the approved matrix, commit the repository change, fast-forward both the delivery branch and `main`, reinstall the exact commit on both Macs, then update the three existing Scheduled definitions through supported Codex App controls without changing their other saved fields.
