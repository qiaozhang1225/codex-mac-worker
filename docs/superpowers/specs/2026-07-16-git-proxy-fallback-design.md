# Git Proxy Fallback Design

## Problem

The Worker launches Git without proxy environment variables. Git therefore ignores the
macOS system proxy and connects directly to GitHub. On the Mac mini that direct route is
intermittent: Issue #12 completed execution and verification, then all three bounded push
attempts failed to connect to `github.com:443`. The same authenticated dry-run push later
succeeded both directly and through the local Clash proxy, confirming that credentials,
repository permissions, and GitHub App installation are valid.

## Considered Approaches

1. **Explicit Worker Git proxy with direct fallback (selected).** Add an optional proxy URL
   to Worker configuration. Git network commands prefer that proxy and use the direct route
   as a bounded fallback. This is deterministic, testable, and does not couple the Worker to
   macOS command output.
2. **Read the macOS system proxy dynamically.** This follows desktop settings automatically,
   but adds macOS-specific parsing and makes pre-login LaunchDaemon behavior less predictable.
3. **Only increase direct-connection retries.** This does not address the unreliable route and
   can make failures substantially slower, so it is rejected.

## Configuration and Scope

Add an optional `git_proxy_url` Worker setting. The Mac mini will set it to
`http://127.0.0.1:7897`. An absent or empty value preserves direct-only behavior for other
installations.

The setting is passed only to `GitOperations`. It is never included in Codex execution,
preparation commands, GitHub API clients, prompts, Issues, PR bodies, or logs. Git credentials
continue to use the existing temporary Askpass environment and are not stored in remote URLs.

## Route and Retry Behavior

Only network Git operations (`clone`, remote update, and push) use route selection. Local Git
commands remain unchanged.

The existing three-attempt bound remains unchanged. When a proxy is configured, network
attempts alternate routes in this deterministic order:

1. configured proxy;
2. direct connection;
3. configured proxy.

Each attempt receives only its route-specific proxy environment. Permanent errors such as
authentication failure, authorization denial, certificate failure, repository absence, and
local ref conflicts stop immediately. Only the existing classified transient transport errors
advance to the next route. Route selection clears inherited proxy-bypass variables and applies
command-scoped generic and repository-specific Git proxy overrides so global or local Git
configuration cannot collapse the two routes. Error messages report the attempt count but never
print credentials.

This order makes the known-good proxy the normal route, preserves cold-start operation when
the desktop proxy is unavailable, and keeps total latency and request volume bounded.

## Validation

Automated tests will prove:

- the configured proxy is used on the first network attempt;
- a transient proxy failure falls back to a clean direct environment;
- a later transient direct failure returns to the proxy without exceeding three attempts;
- direct-only behavior remains unchanged when no proxy is configured;
- permanent errors never fall back or retry;
- proxy settings do not enter non-network Git commands or credential-bearing remote URLs;
- configuration parsing accepts an HTTP(S) proxy URL and rejects credentials or unsupported
  schemes.

The complete test suite must pass before a Draft PR is created. After deployment, Mac mini
smoke tests will perform authenticated dry-run pushes through both configured and direct routes
without printing the installation token. Issue #12 will be retried only after a separate,
explicit control-command approval.
