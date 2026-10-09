# Integrated terminal: install, use, operate

How to install, use and troubleshoot the dashboard's integrated terminal,
process center and exchange connection controls. The design and the reasons
behind it are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Requirements

* Linux (the terminal uses PTYs, `/proc` and `SO_PEERCRED`).
* The project's Python environment (`.venv`) with `requirements.txt` installed;
  the terminal service needs only the standard library and `websockets`.
* `bash` for exact exit statuses of interactive commands (other shells work,
  but their command status is shown as *unknown*).
* Optional: a systemd user session (`systemctl --user`) to keep the service
  running across logins.
* Node.js 22.12+ for the dashboard.

## Install

The desktop installer installs the terminal service as one of its steps:

```bash
cd frontend && npm run desktop:install
```

Or on its own, from the repository root:

```bash
.venv/bin/python -m src.terminal install
```

This writes two files, both marked `managed-by: tradebot-terminal`:

| File | Purpose |
|---|---|
| `~/.local/bin/tradebot-term` | the CLI (runs `python -P -m src.terminal` from this checkout) |
| `~/.config/systemd/user/tradebot-terminal.service` | user service, enabled when `systemctl --user` works |

A file at either path that the installer did not write is left untouched and
reported. No configuration, credential or `.env` file is read or written.
`--no-systemd` (or `TB_TERMINAL_NO_SYSTEMD=1` for the desktop installer) skips
the unit; `TB_TERMINAL_PYTHON` selects the interpreter.

Make sure `~/.local/bin` is on `PATH`.

## Start, stop, status

```bash
tradebot-term start
```

```bash
tradebot-term status
```

```bash
tradebot-term stop
```

`start` uses the systemd unit when it is installed and otherwise starts the
service detached; it does nothing if the service is already running. The
desktop app runs it on launch, so normally nothing needs starting by hand.

`stop` ends **every session and everything running in them**. Closing the
dashboard, collapsing the terminal or disconnecting a client never does.

`restart` is `stop` followed by `start`.

## Using the terminal in the dashboard

The bar at the bottom of the dashboard has **Terminal** and **Processes**
side by side.

* **Terminal** shows or hides the dock. `+` opens a new shell in the
  repository root. Tabs switch sessions; double-click a tab or press `F2` to
  rename it; `×` closes it (with a confirmation when a command is still
  running). **Ctrl+C** sends SIGINT to the foreground command. Drag the top
  edge, or focus it and press ↑/↓, to change the dock's height.
* Everything typed goes to the shell unchanged: Ctrl+C, Ctrl+D, arrows, tab
  completion, paste, REPLs, editors and full-screen programs work as in any
  terminal.
* **Processes** lists what is running (title, PID, session, directory, start
  time, duration, CPU, memory) and what recently finished. Failures are red
  with their exit code or signal and the reason. Clicking a running command
  opens its session; clicking a finished entry or a trading-API job (backfill,
  retrain) shows its retained output. INT/TERM/KILL signal only that entry's
  process group; KILL asks first.
* The red *failed* count shows failures you have not seen yet; opening the
  process center marks them seen, and that survives a reload.

### Browser dashboard (Vite dev server)

The Electron app connects through its main process and needs no token. A
plain browser connects to `ws://127.0.0.1:8766/` and asks once per browser
session for the access token:

```bash
tradebot-term token
```

`tradebot-term token --rotate` issues a new one; connected browsers must
re-enter it. The token is kept in `sessionStorage` only. The WebSocket accepts
loopback connections from the configured origins only.

## Using the terminal from a shell

Every command talks to the same service the dashboard uses, so sessions and
processes are the same in both.

| Command | What it does |
|---|---|
| `tradebot-term ls` | list sessions |
| `tradebot-term ps [--all]` | running processes (`--all` adds history) |
| `tradebot-term new [--name N] [--cwd DIR] [--attach]` | open a shell session |
| `tradebot-term attach SESSION` | mirror a session in this terminal; **Ctrl+]** detaches and leaves it running |
| `tradebot-term output TARGET [--tail N] [--plain]` | retained output of a session (`s-…`) or process (`p-…`) |
| `tradebot-term send SESSION TEXT… [--no-enter]` | type into a session |
| `tradebot-term run [--name N] [--cwd DIR] [--wait] -- CMD ARGS…` | run a command as a tracked job; `--wait` exits with its status |
| `tradebot-term kill TARGET [--signal INT\|TERM\|KILL\|HUP]` | signal a session's foreground command or one process |
| `tradebot-term rename SESSION NAME` | rename a session |
| `tradebot-term close SESSION` | terminate a session and everything in it |
| `tradebot-term paths` | where the socket, token, log and state live |

Add `--json` to `ls`, `ps`, `status` and `paths` for machine-readable output.

## Exchange connections

The **Exchange Connections** panel (and the venue dots in the status bar)
show, for Binance and OKX separately:

* **Market data** — whether the venue's public API is connected.
* **Account** — whether an authenticated, read-only balance request was
  accepted (*Authenticated*), refused (*Credentials rejected*) or could not be
  completed (*Check failed*).
* **Credentials** — *configured*, *incomplete* or *missing*; key values are
  never shown or returned.

Connect / Retry / Reconnect / Verify account / Disconnect act on one venue
only and need the operator secret entered in the header, like every other
operator action. Disconnect asks first and is refused when it would leave no
venue connected. None of these controls places an order or changes the
execution mode or a risk limit.

## Configuration

Environment variables read by the service (set them in the unit with
`systemctl --user edit tradebot-terminal` or in the environment of
`tradebot-term start`):

| Variable | Default | Meaning |
|---|---|---|
| `TB_TERMINAL_RUNTIME_DIR` | `$XDG_RUNTIME_DIR/tradebot-terminal`, else `/tmp/tradebot-terminal-<uid>` | socket, pid and lock (0700) |
| `TB_TERMINAL_STATE_DIR` | `$XDG_STATE_HOME/tradebot-terminal`, else `~/.local/state/tradebot-terminal` | token and log (0700) |
| `TB_TERMINAL_DEFAULT_CWD` | repository root | where new sessions start |
| `TB_TERMINAL_WS_HOST` | `127.0.0.1` | loopback address for the browser WebSocket |
| `TB_TERMINAL_WS_PORT` | `8766` | `0` disables the WebSocket (Electron and CLI do not use it) |
| `TB_TERMINAL_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | origins allowed on the WebSocket |
| `TB_TERMINAL_MAX_SESSIONS` | `12` | concurrent running sessions (1–64) |
| `TB_TERMINAL_BUFFER_BYTES` | `1048576` | retained output per session |
| `TB_TERMINAL_HISTORY` | `100` | finished processes kept |

The dashboard reads `VITE_TERMINAL_WS_URL` (default `ws://127.0.0.1:8766/`)
and refuses any non-loopback value.

## Troubleshooting

| Symptom | Check |
|---|---|
| Dock says *terminal service offline* | `tradebot-term status`; then `tradebot-term start`. Logs: `journalctl --user -u tradebot-terminal` (systemd) or the `log` path from `tradebot-term paths`. |
| Dock asks for a token | browser only: paste `tradebot-term token`. |
| *speaks a different protocol* | the service is older or newer than the dashboard: `tradebot-term restart`. |
| `socket path … is N bytes` at start | the runtime directory is too deep for a Unix socket; set `TB_TERMINAL_RUNTIME_DIR` to a short path. |
| `refusing to run as root` | run as your own user; the service is per user by design. |
| A command's status shows *unknown* | the shell is not bash, or bash was started without the integration (for example by `exec bash --norc`). |
| A full-screen program looks garbled after re-attaching | press Ctrl+L to redraw. |
| `tradebot-term: command not found` | add `~/.local/bin` to `PATH`, or run `.venv/bin/python -m src.terminal`. |

## Upgrade

```bash
git pull
```

```bash
.venv/bin/python -m src.terminal install
```

```bash
tradebot-term restart
```

Re-running the installer rewrites only its own files. The running service
keeps its sessions until `restart`, which ends them.

## Uninstall

```bash
tradebot-term uninstall
```

This stops the service and removes the wrapper and unit the installer wrote.
`--purge` also deletes the token and the state directory. Files the installer
did not write are never removed.
