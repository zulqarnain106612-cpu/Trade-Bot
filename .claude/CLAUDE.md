## SESSION INIT (every session, first action, no exceptions)
```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```
That output IS your complete context. Act on NEXT task immediately.

## Checkpoint
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent claude \
  --completed "what you did" --next "exact next step" --files "src/file.py"
bash scripts/claude-commit.sh --msg "type(scope): desc [claude]"
```

## Output tags (all outputs — no exceptions)
`<gap>` `<issue>` `<broken>` `<missing>` `<decision>` `<task>` `<risk>` `<security>` `<debt>` → project files | `<chat>` → chat only

## Never
- Read files for context — resume.py IS the context
- cat any file >100 lines — use grep/sed/head/tail
- Read: MODULE_MAP.json · ARCHITECTURE.md · RAW_SCAN.json · SESSION_STATE.json · GAPS.md · rag.db · package-lock.json · requirements.lock
- print() in src/ — use structlog
- Hardcode secrets
- Push to git (commit only)
