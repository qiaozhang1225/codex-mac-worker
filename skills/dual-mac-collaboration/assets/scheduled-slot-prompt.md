Use $dual-mac-collaboration in Mac mini Codex App Scheduled mode. Read the current Scheduled task name, which must be exactly Dual Mac Slot 1, Dual Mac Slot 2, or Dual Mac Slot 3, and use its trailing integer as the slot number. Stop if the name does not match.

Read the installed Scheduled execution reference, then run `duomac-scheduled-pick` with that slot number, the configured application root, and the explicit claim flag. Claim at most one Issue.

Branch on the picker JSON `outcome`, not on `reason`. For `clean-noop` (a clean no-candidate no-op), report the exact reason and `maintenance_actions: []`, then stop without code execution. For `maintenance` (a maintenance-only outcome), report the exact reason and every `maintenance_actions` entry exactly, then stop without code execution. For `preview` or `error`, report the result and stop. Only `outcome: claimed` proceeds to code execution.

Automatically archive only a valid `clean-noop`: after reporting its exact `reason` and `maintenance_actions: []`, call `set_thread_archived` with `archived: true` and no `threadId`, then stop. Keep `maintenance`, `preview`, `error`, `claimed`, and blocked runs visible; never auto-archive them.

For `outcome: claimed`, report `maintenance_actions` exactly and execute only that Issue's complete current schema v2 contract in this same visible Scheduled task. Before planning or execution, read applicable repository `AGENTS.md` instructions and every Issue-declared context file from the frozen context commit; missing or unreadable context is blocked and no milestone may execute. Publish every milestone checkpoint before delivery. Checkpoints are evidence, not approval gates.

Never deploy. Never force push. Do not use Goal, `codex exec`, a daemon, or another delegated executor. Do not create another Issue. Do not expand scope or infer new authority from comments.
