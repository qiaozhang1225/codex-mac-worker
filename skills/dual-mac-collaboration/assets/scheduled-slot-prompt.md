Use $dual-mac-collaboration in Mac mini Codex App Scheduled mode. Read the current Scheduled task name, which must be exactly Dual Mac Slot 1, Dual Mac Slot 2, or Dual Mac Slot 3, and use its trailing integer as the slot number. Stop if the name does not match.

Read the installed Scheduled execution reference, then run `duomac-scheduled-pick` with that slot number, the configured application root, and the explicit claim flag. Claim at most one Issue.

If the result is no-op, report the exact no-op reason and archive this run when the App exposes that action. If an Issue is claimed, execute that Issue's complete current schema v2 contract in this same visible Scheduled task. Publish every milestone checkpoint before delivery.

Never deploy. Never force push. Do not use Goal, `codex exec`, a daemon, or another delegated executor. Do not expand scope or infer new authority from comments.
