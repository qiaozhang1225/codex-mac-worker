# Preparation Network Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow only reviewed dependency installation commands to reach PyPI and npm while keeping Codex execution and verification offline.

**Architecture:** Add a dedicated Codex permission profile with the same filesystem boundary and a three-domain network allowlist. Thread an explicit profile selection through the command runner and choose it only for preparation calls.

**Tech Stack:** Python 3.12, pytest, Codex CLI 0.144 permission profiles, macOS Seatbelt, TOML.

## Global Constraints

- Preparation remains sandboxed.
- Codex execution and verification remain network-disabled.
- Allowed domains are exactly `pypi.org`, `files.pythonhosted.org`, and `registry.npmjs.org`.
- Host secrets and `.env` files remain unreadable.
- Do not resume failed Issue #7; cancel and re-dispatch only after deployment verification.

---

### Task 1: Select a dedicated profile for preparation

**Files:**
- Modify: `src/codex_mac_worker/verification.py`
- Modify: `src/codex_mac_worker/worker.py`
- Modify: `tests/test_prompting_verification.py`
- Modify: `tests/test_worker_service.py`

- [ ] Add failing tests proving `run_commands` honors a requested profile and Worker preparation requests `codex-worker-preparation`.
- [ ] Run focused tests and verify the missing parameter/profile behavior fails.
- [ ] Add `permission_profile: str = "codex-worker"` and use it in the sandbox argument list.
- [ ] Pass `permission_profile="codex-worker-preparation"` in both preparation call sites.
- [ ] Run focused tests and the complete suite.

### Task 2: Ship the least-privilege profile

**Files:**
- Modify: `templates/codex-worker.config.toml`
- Modify: `tests/test_operational_assets.py`
- Modify: `scripts/install_macos.sh`
- Modify: `docs/MAC_MINI_SETUP.md`

- [ ] Add a failing template test for inheritance, network enablement, and the exact domain allowlist.
- [ ] Add the preparation profile to the template and make the installer validate both profiles.
- [ ] Document the separation between preparation and execution permissions.
- [ ] Run the complete suite, shell syntax checks, and `git diff --check`.
- [ ] Commit, publish a Draft PR, and obtain explicit merge approval.

### Task 3: Deploy and recover

- [ ] Install the exact merged Worker commit on Mac mini.
- [ ] Deploy the reviewed Codex config template and validate both permission profiles.
- [ ] Run a sandboxed PyPI/npm connectivity smoke test through `codex-worker-preparation` and verify the default profile remains offline.
- [ ] Restart the LaunchDaemon and confirm stable operation.
- [ ] Obtain confirmation, cancel Issue #7, create one replacement Issue with the same task hash, and monitor through `running`.
