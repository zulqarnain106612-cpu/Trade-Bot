# Integrated terminal, process center, workspace and venue controls

Architecture plan and implementation record. Every "current state" statement
below was verified against the source at `bbe46cb6` before the design was
chosen; nothing here is taken from documentation alone.

## 1. Current state (verified)

| Area | What exists | Evidence |
|---|---|---|
| Dashboard layout | Panels are `flex-wrap` boxes. Each `Panel` owns its own pixel size through `useResizable` (mouse-only, not persisted). Visibility is persisted unversioned under `localStorage['panel-visibility']`. No drag, no maximize/minimize, no keyboard control. `body { overflow: hidden }` makes everything below the first viewport unreachable. | `frontend/src/App.jsx`, `components/Panel.jsx`, `hooks/useResizable.js`, `index.css` |
| Terminal / processes | Nothing. No PTY, no process registry, no CLI. | repository-wide search |
| Electron | `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`. Preload exposes one IPC call (`setPendingApprovals`). Dev-mode loads Vite; packaged mode loads `dist/`. No packaging tool: electron-builder was removed (SEC-0007); the supported install is `npm run desktop:install`, whose launcher runs `npm run electron:dev`. | `frontend/electron/*.cjs`, `frontend/scripts/install-desktop-shortcut.sh` |
| Venues | `GET /venues` (availability + last error) and `POST /venues/{venue}/reconnect` (API key + operator secret + rate limit; **no role check, no audit**). `MarketDataFetcher` opens each venue independently (REG-0014) and can `reconnect()`. There is **no disconnect**, no per-venue lock (two concurrent reconnects each build a client), and "available" only means `load_markets()` succeeded — a public call. Authenticated access is never checked. | `src/api/main.py` (`venues_status`, `reconnect_venue`), `src/data/fetcher.py` |
| Jobs | Retrain runs as an orchestrator task; backfill is awaited inside the request. Neither is visible anywhere as a job. | `src/engine/orchestrator.py`, `src/api/main.py` |
| Local checks | The repository hook blocks local `pytest`, `ruff`, `vitest`, `npm run build`; suites run in CI. | `config/command_policy.json` (`local_checks`) |

## 2. Requirements → design decisions

### 2.1 One authoritative terminal/process service

A host-user daemon (`python -m src.terminal daemon`, package `src/terminal`,
layer *orchestration*) owns every PTY session and the process registry. The
dashboard (GUI) and the host CLI (`tradebot-term`) are both clients of it, so
they see the same sessions, output and process state by construction.

Rejected alternatives:

* **PTY inside the Electron main process (node-pty).** Native module that must
  be rebuilt for Electron's ABI and needs a compiler toolchain at install;
  sessions would die with the window; the browser dashboard and the CLI could
  not share them.
* **PTY inside the trading API process.** Puts arbitrary shell spawning in the
  process that holds exchange keys; sessions die on every API restart; the
  production unit runs the API as a separate `tradebot` user with
  `ProtectHome=true`, i.e. not as the operator.

The daemon uses only the Python standard library for the terminal itself
(`pty`, `termios`, `fcntl`, `subprocess`) plus `websockets`, which is already a
pinned runtime dependency; its floor rises from 12.0 to 13.0, the first
release with `websockets.asyncio.server`. No new Python dependency.

**PTY mechanics.** `pty.openpty()`; the child is started with
`subprocess.Popen(start_new_session=True)` and acquires the slave as its
controlling terminal with `TIOCSCTTY`, so job control, `Ctrl+C` (the line
discipline sends SIGINT to the foreground process group), `Ctrl+D` (EOF),
`TIOCSWINSZ` resizing and exit statuses are the kernel's, not emulated.
Verified on this host: `sleep 30` becomes the foreground group, `^C` returns
the shell with `$?=130`, `exit 3` is reaped as 3.

**Process identity without guessing.**

* Foreground process: `tcgetpgrp(master)`; when it differs from the shell's
  pid a command is running. Title = `/proc/<pgid>/cmdline` (the real argv),
  cwd = `/proc/<pgid>/cwd`, start time = `/proc/<pgid>/stat`.
* Exact exit status for interactive commands: bash shell integration (an
  rcfile that sources `~/.bashrc`, then adds `PS0`/`PROMPT_COMMAND` hooks)
  emits `OSC 133;E;<command>`, `OSC 133;C` (started) and
  `OSC 133;D;<status>` (finished). Verified on bash 5.3. The working
  directory is read from `/proc/<pid>/cwd`, not from the shell.
* Without integration (zsh, fish, `exec`'d shells) commands are still detected
  from the foreground group; their status is reported as **unknown**, never
  invented.
* Managed jobs (`tradebot-term run -- <argv>`) run their argv directly under a
  PTY (no shell, no injection surface); status comes from `waitpid`.
* Application jobs (retrain, backfill inside the API) register over the Unix
  socket (`job.begin/output/end`); best effort, bounded by a short timeout,
  never on the order path, never able to fail the request.
* Resource usage: `/proc/<pid>/stat` (CPU ticks, RSS) summed over the process group,
  sampled once per poll tick; omitted when `/proc` cannot be read.

### 2.2 Transports and authentication

| Client | Transport | Authentication |
|---|---|---|
| Host CLI | Unix socket `$XDG_RUNTIME_DIR/tradebot-terminal/termd.sock` (dir 0700, socket 0600), newline-delimited JSON | filesystem permissions **and** `SO_PEERCRED` uid == daemon uid |
| Electron dashboard | renderer → `ipcRenderer` (preload, narrow API, frames validated in main) → main process → Unix socket | the Unix socket's; the renderer never sees a token and still has no Node access |
| Browser dashboard (Vite dev) | WebSocket on `127.0.0.1:8766` only | 256-bit token from `tradebot-term token` sent in the first frame (never in the URL), `Origin` allow-list, loopback `Host` check (DNS rebinding), 5 s hello deadline |
| Trading API (job registration) | Unix socket | as CLI |

The listener refuses to bind a non-loopback address. The daemon refuses to run
as root unless `--allow-root` is passed. Terminal authorization is separate
from trading authorization: the terminal token grants no API access and no API
key grants terminal access; nothing in the terminal path calls a trading
endpoint.

Limits (all validated server-side): sessions (default 12, max 64), clients
(32), inbound frame 1 MiB, input 64 KiB/frame, name 64 chars, terminal size
1–1000 × 1–500, retained output 1 MiB/session, history 100 entries with
64 KiB output each, per-client send queue 2048 frames (a slow client is
disconnected and resyncs instead of stalling a PTY).

### 2.3 Session lifecycle

`create → running → (shell exits) exited → close`. Closing sends SIGHUP to the
session's process groups, waits a grace period, then SIGKILLs anything still
in the session (`getsid == leader`), so nothing is orphaned. Detaching or
hiding the GUI never closes a session. Daemon shutdown (SIGTERM, `tradebot-term
stop`, `systemctl --user stop`) closes every session the same way; the systemd
unit additionally uses `KillMode=mixed` so the cgroup is emptied.

Reconnect: a client re-sends `hello` and receives a full `snapshot`
(sessions, active processes, history); output re-attaches from a byte offset,
and the response says when retained output was truncated.

### 2.4 Process center

Single registry (`src/terminal/registry.py`) with `active` and a bounded
`history`. Events are pushed (`process`, `process_done`); the GUI never
polls. A finished entry leaves `active` and enters history with its exit
code, signal, failure reason and retained output; non-zero/terminated entries
render red. Clicking an active command opens the dock on its session; a job
opens its session or console; a history entry opens its retained output.
Killing targets only the entry's process group, after re-checking that the
group still belongs to the session (pid-reuse guard).

### 2.5 Workspace

`react-grid-layout` 2.3.0 (MIT; deps `react-draggable` and `react-resizable`,
both MIT; passes `nodeRef`, so no `findDOMNode`, which React 19 removed).
Every panel (the overview included) is a grid item with: drag (from the
header's title area only; buttons, inputs, links, labels and anything marked
`.ws-no-drag` cancel a drag), resize with minimum sizes, minimize (header
only, body kept mounted), maximize (fills the measured visible workspace via
CSS without remounting the panel, previous geometry restored), hide/restore,
keyboard move/resize (focus the grip, then arrows to move and Shift+arrows
to resize), `Esc` to leave maximize. Layout persists as versioned JSON under
`localStorage['tradebot.workspace']` (`version: 1`); invalid data is repaired
panel by panel; the old `panel-visibility` key is migrated; "Reset layout"
restores defaults.
Narrow screens render a single column without touching the saved layout.

### 2.6 Venue controls

Extend `MarketDataFetcher` instead of a second client: per-venue lock (no
duplicate clients), explicit phases (`connecting`, `connected`,
`reconnecting`, `disconnected`, `failed`, `unavailable`), credential presence
(`configured`/`incomplete`/`missing`, never values), and a separate account
state (`unconfigured`, `unverified`, `verifying`, `authenticated`,
`rejected`, `failed`, `unavailable`) decided by a read-only
`fetch_balance()` with a 10 s bound.
New endpoints `POST /venues/{venue}/connect`, `/disconnect`, `/verify`, and the
existing `/reconnect`, all require the API key, a new `MANAGE_VENUES`
permission (trade-authorizing role only), the operator secret, a rate limit,
and write an audit event. Disconnecting the last connected venue is refused
(409) — a fetcher with no venue is the state `initialize()` already treats as
fatal. Nothing here places orders, changes the execution mode or touches risk
limits. Error strings pass through the shared secret redactor before they are
stored or returned.

## 3. Data flow

```
xterm.js ─┐                          ┌─ PTY session (bash + integration) ─ processes
          ├─ Electron IPC ─ main ─┐  │
CLI ──────┼──────────────── Unix socket ── TerminalServer ── SessionManager ─┤
browser ──┴── WS 127.0.0.1 + token ┘        │                │               └─ job sessions
                                            └── ProcessRegistry ◄── job.* (trading API)
```

## 4. Installation, upgrade, rollback

* `tradebot-term install` (called by `npm run desktop:install`) writes
  `~/.local/bin/tradebot-term` and, when `systemctl --user` works, a user unit
  `tradebot-terminal.service`; both carry a `managed-by` marker and an
  unmarked file at either path is never overwritten. The token is created
  once and never rewritten except by `token --rotate`.
* The Electron main process runs `tradebot-term start` on launch; `start` is
  idempotent (systemd unit if installed, otherwise a detached daemon).
* Upgrade: re-run the installer; the running daemon keeps its sessions until
  `tradebot-term restart`. A client whose protocol version differs from the
  daemon's is refused at hello and the dock asks for a restart.
* Rollback/uninstall: `tradebot-term uninstall [--purge]` stops and removes
  exactly the files it created.

## 5. Acceptance criteria → deciding tests

Every row is a registry entry in `config/quality_registry.json`; the
traceability document is generated from it.

| Registry | Criterion | Deciding tests |
|---|---|---|
| TERM-001 | Real PTY: Ctrl+C, Ctrl+D, resize, ANSI/Unicode, concurrency; GUI and CLI share sessions | `tests/terminal/test_terminal_sessions.py`, `tests/terminal/test_terminal_server.py` |
| TERM-002 | Process identity and exit status observed, never invented | `tests/terminal/test_terminal_registry.py`, `tests/terminal/test_terminal_host.py`, `tests/terminal/test_terminal_markers_buffer.py`, `tests/terminal/test_terminal_sessions.py` |
| TERM-003 | Every request validated; the Electron bridge relays only allowed types | `tests/terminal/test_terminal_protocol.py`, `tests/terminal/test_terminal_server.py`, `frontend/src/desktop/terminalBridge.test.js` |
| TERM-004 | Local-only and authenticated: socket modes, peer uid, loopback, Host/Origin, token, root refusal | `tests/terminal/test_terminal_server.py`, `tests/terminal/test_terminal_host.py`, `frontend/src/terminal/client.test.js` |
| TERM-005 | Output survives reconnect; gaps reported; snapshot resync | `tests/terminal/test_terminal_markers_buffer.py`, `frontend/src/terminal/client.test.js` |
| TERM-006 | No orphans on close or stop; kill only the recorded group | `tests/terminal/test_terminal_sessions.py`, `tests/terminal/test_terminal_server.py` |
| TERM-007 | Host CLI; API backfill/retrain reported as jobs without blocking | `tests/terminal/test_terminal_cli.py`, `tests/terminal/test_terminal_install_jobs.py`, `tests/test_api_terminal_jobs.py` |
| TERM-008 | Install/upgrade/uninstall never clobber; stop never kills a stranger | `tests/terminal/test_terminal_install_jobs.py`, `tests/terminal/test_terminal_cli.py` |
| TERM-009 | Status bar, process center, terminal dock | `frontend/src/components/terminal/terminalUi.test.jsx` |
| TERM-010 | Workspace persistence, repair, maximize/minimize, keyboard | `frontend/src/workspace/layoutStore.test.js`, `frontend/src/components/workspace/Workspace.test.jsx` |
| VEN-001 | Per-venue connect/disconnect/verify with honest state and every guard | `tests/test_venue_controls.py`, `tests/test_venue_controls_api.py`, `frontend/src/components/panels/VenuesPanel.test.jsx` |
| SEC-0010 | No path hands a shell to anyone but the local user | `tests/terminal/test_terminal_server.py`, `tests/terminal/test_terminal_host.py`, `frontend/src/desktop/terminalBridge.test.js` |

## 6. Known limitations

* Linux is the supported platform (PTY + `/proc` + `SO_PEERCRED`); macOS would
  need `/proc` replacements and is not claimed. Windows is not supported.
* Exact exit codes for interactive commands need the bash integration; other
  shells report "unknown".
* Background jobs (`cmd &`) are not listed as active processes; only the
  foreground job of each session is.
* A full-screen program's screen is reconstructed from retained output when a
  client attaches; a program that redraws only on change may need `Ctrl+L`.
