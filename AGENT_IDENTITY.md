# AGENT IDENTITY PROTOCOL

## Authorized Writers

| Identity | Type | Verification |
|---|---|---|
| **HUMAN** | Project owner (you) | Physical keyboard / terminal session |
| **CLAUDE_MAIN_AGENT_v1** | Claude main agent | `CLAUDE_AGENT_IDENTITY` env var + session token |

## All Others: READ-ALERT / WRITE-BLOCKED

Any process not listed above that touches this directory triggers a sentinel alert.

---

## How CLAUDE_MAIN_AGENT_v1 Identifies Itself

When Claude (main agent) executes commands in your project, the terminal session
must be started with this env var set:

```bash
export CLAUDE_AGENT_IDENTITY=CLAUDE_MAIN_AGENT_v1
```

The sentinel reads `/proc/<pid>/environ` to verify this before suppressing alerts.

---

## Zero Auto-Execution Policy

- No auto-commits
- No auto-push
- No auto-fetch
- No auto-session-start/end
- No auto-context-primer loading
- No auto-hook execution
- No auto-environment activation
- No background daemons writing to this directory
- No telemetry writes

Any violation → sentinel alert → you decide.

---

## Sentinel Log

`.sentinel/access.log` — all events written here.
Only you and CLAUDE_MAIN_AGENT_v1 may clear this log.
