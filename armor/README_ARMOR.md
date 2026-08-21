# TRADEBOT ARMOR — Deployment Guide

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    TRADEBOT ARMOR STACK                         │
├─────────────────────────────────────────────────────────────────┤
│  L1  auditd          Kernel syscall audit — write/exec/network  │
│  L2  sentinel.py     Python watchdog — FS + process + network   │
│  L3  git hooks       pre-commit / pre-push / post-commit        │
│  L4  VSCode          settings.json + tasks.json hardened        │
└─────────────────────────────────────────────────────────────────┘
```

## Authorized Identities

| Who | How sentinel recognizes them |
|---|---|
| **YOU (human)** | Interactive TTY session, your Linux username |
| **CLAUDE_MAIN_AGENT_v1** | `CLAUDE_AGENT_IDENTITY=CLAUDE_MAIN_AGENT_v1` env var in process |

## Deploy (one time, as root)

```bash
# 1. Copy armor/ folder into your project
cp -r armor/ ~/Projects/Trade-Bot-main/

# 2. Run installer
cd ~/Projects/Trade-Bot-main/armor
sudo bash install_armor.sh /home/fujitsu/Projects/Trade-Bot-main fujitsu

# 3. Verify all layers
bash verify_armor.sh /home/fujitsu/Projects/Trade-Bot-main
```

## When Claude (main agent) needs to make changes

```bash
# Export identity token BEFORE running any command
export CLAUDE_AGENT_IDENTITY=CLAUDE_MAIN_AGENT_v1

# Now all commands from this shell are recognized as authorized
git add . && git commit -m "feat: ..."
python3 some_script.py
```

## Alert channels

| Channel | How |
|---|---|
| Desktop popup | `notify-send` (requires display session) |
| All terminals | `wall` broadcast |
| Log file | `.armor/alerts.log` (JSON per line) |
| systemd journal | `journalctl -u tradebot-sentinel -f` |
| auditd | `ausearch -k tradebot_write -i` |

## Live monitoring commands

```bash
# Watch alerts in real time
journalctl -u tradebot-sentinel -f

# Search auditd for unauthorized writes
ausearch -k tradebot_write -i | tail -50

# Search for network connections from project
ausearch -k tradebot_network_out -i | tail -20

# Check sentinel status
systemctl status tradebot-sentinel

# Run health check
bash ~/Projects/Trade-Bot-main/armor/verify_armor.sh
```

## What triggers a CRITICAL alert

- Any file write/delete/move by a process that is NOT you or CLAUDE_MAIN_AGENT_v1
- Any write to protected files (settings.json, tasks.json, .git/hooks, .env, pyproject.toml)
- SHA256 hash mismatch on any protected file
- Any outbound network connection from a project process that is not authorized
- Automated git commit attempt (no TTY)
- Automated git push attempt (no TTY)
- Unknown process spawning inside project directory

## Limitations (honest)

| Limitation | What it means |
|---|---|
| inotify/watchdog cannot **block** writes | It alerts immediately after the write; cannot prevent the first byte |
| AppArmor not active on your kernel `6.18.5-fc-v20` | Kernel-level blocking unavailable without recompiling with AppArmor |
| PID resolution is best-effort | Fast processes may exit before identification |
| `lsof` has ~100ms latency | Very fast file writes may be attributed to wrong PID |
| Network monitor polls every 5s | Short-lived connections < 5s may be missed by psutil; auditd catches these |

## To get hard BLOCKING (not just alerting)

Your kernel `6.18.5-fc-v20` does not have AppArmor enabled.  
Options:
1. Switch to Ubuntu's stock kernel: `sudo apt install linux-generic` → reboot → AppArmor will be active
2. Add AppArmor profile on top of this stack (ask Claude for `apparmor.profile`)

## Files written by armor

```
.armor/
├── sentinel.py       # main sentinel (owned root, chmod 750)
├── alerts.log        # CRITICAL/WARNING events (JSON per line)
├── access.log        # authorized writes (INFO, JSON per line)
└── integrity.json    # SHA256 snapshot of protected files
```
