# Scheduled Final Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make first-run maintenance reporting concurrency-accurate and reject canonical Scheduled content that loses executable outcome semantics or adds affirmative forbidden behavior.

**Architecture:** Filesystem helpers will return atomic creation ownership while preserving advisory locking. The staged validator will require exact executable outcome boundaries and use case-insensitive semantic contradiction regular expressions that exclude canonical negated prohibitions. Existing installer staging remains the preservation boundary.

**Tech Stack:** Python 3.12, pytest, POSIX `mkdir`/`os.open`/`flock`, TOML/YAML validation, zsh installer.

## Global Constraints

- Exactly three Scheduled Slot identities remain supported.
- At most one Issue is claimed per picker run and exactly one concurrent first-run claim wins.
- Only `outcome: claimed` authorizes code execution.
- Invalid staged content must never replace the existing live skill.
- Tests precede production changes and must demonstrate the expected RED failure.

---

### Task 1: Atomic creation ownership

**Files:**
- Modify: `skills/dual-mac-collaboration/scripts/duomac_scheduled.py`
- Modify: `skills/dual-mac-collaboration/scripts/scheduled_pick.py`
- Test: `tests/test_scheduled_execution.py`

**Interfaces:**
- Produces: `ensure_directory(path: Path, mode: int) -> bool`, true only when this call creates the final directory.
- Produces: `dispatch_lock(path: Path) -> Iterator[bool]`, yielding true only when this call creates the lock file while preserving exclusive flock coverage.

- [ ] Add a simultaneous-first-run regression that launches two pickers against a missing application root and asserts one claim, one `application-root-created`, and one `dispatch-lock-file-created` action.
- [ ] Run the regression and confirm duplicate creation reporting fails.
- [ ] Implement atomic directory creation and `os.open(O_CREAT|O_EXCL)` lock-file creation with existing-file fallback.
- [ ] Use only returned creation ownership to append picker actions.
- [ ] Run concurrent and maintenance picker tests and confirm they pass.

### Task 2: Executable outcome validation

**Files:**
- Modify: `scripts/validate_skill.py`
- Test: `tests/test_skill_content.py`

**Interfaces:**
- Extends: `_validate_scheduled_content(skill_root: Path) -> None`.
- Requires in both canonical documents: picker command, `outcome`, `claimed`, `clean-noop`, `maintenance`, `maintenance_actions`, and wording that only claimed proceeds to execution.

- [ ] Add staged-install preservation cases that remove each executable outcome boundary from the prompt/reference.
- [ ] Run the cases and confirm validation currently accepts at least one invalid stage.
- [ ] Add the exact required boundaries to both Scheduled boundary tuples.
- [ ] Run content and installer preservation tests and confirm they pass.

### Task 3: Semantic contradiction detection and verification

**Files:**
- Modify: `scripts/validate_skill.py`
- Test: `tests/test_skill_content.py`
- Append: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Produces: case-insensitive contradiction patterns for affirmative Goal, `codex exec`, legacy daemon/LaunchDaemon/background worker, autonomous Issue creation, and scope expansion instructions.
- Preserves: canonical negated prohibitions such as `Do not use Goal`.

- [ ] Add parametrized preservation regressions for mixed-case affirmative forbidden instructions appended beside canonical prohibitions.
- [ ] Run them and confirm the current case-sensitive phrase list misses them.
- [ ] Replace literal phrase matching with compiled case-insensitive semantic patterns scoped to affirmative instructions.
- [ ] Run content, installer, concurrency, maintenance, focused, and full pytest suites.
- [ ] Run system/repo validators, `zsh -n`, Python compilation, config help, TOML parsing, and `git diff --check`.
- [ ] Append RED/GREEN evidence to the Task 6 report and commit the scoped files.
