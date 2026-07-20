# Scheduled Execution

Use this route only for a visible Mac mini Codex App Scheduled run named `Dual Mac Slot 1`, `Dual Mac Slot 2`, or `Dual Mac Slot 3`. MacBook remains the sole dispatcher: Mac mini never creates an Issue automatically, expands scope, or infers authority from comments.

1. Read the Mac-local application root and `repositories.toml`, then validate the configuration. Discover the installed interface with `duomac-config-validate --help` before use. Stop on missing or invalid configuration.
2. Run `duomac-scheduled-pick` in preview mode when testing. Production Scheduled prompts use its explicit write flag. Discover arguments and the write flag with `duomac-scheduled-pick --help`; pass the trailing task-name integer as the slot and claim at most one Issue.
3. If the picker returns a no-op, report its exact reason and end without mutating a repository, Issue, or task. Do not create a replacement Issue.
4. After a claim, re-read the current Issue body and require its complete schema v2 contract. Create the task worktree at the claimed base, execute every declared milestone in strict order, and publish each structured checkpoint before beginning the next milestone. Continue without MacBook per-milestone approval while the contract remains valid; checkpoints are not approval gates.
5. Re-read the Issue before delivery. Discover the completion gate with `duomac-issue-complete --help`, run Git preflight and the selected verification profile from `.duomac/project.toml`, deliver normally, and complete only after every milestone checkpoint exists and the final checkpoint precedes delivery evidence.
6. On any error after task-start, publish a blocked event with retained evidence and end this run. Never let another Slot resume or inherit the task automatically.

Never deploy, force push, use production data, use Goal, run `codex exec`, start a daemon or LaunchDaemon Worker, delegate to another executor, or broaden the frozen Issue contract.
