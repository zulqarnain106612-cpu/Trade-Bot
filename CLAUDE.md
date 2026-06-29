# Trade Bot — Claude Instructions

## ══ MANDATORY SESSION START — ONE COMMAND, NO EXCEPTIONS ══

```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```

**Run this FIRST. Read its output. That output IS your full context.**
Do NOT read CONTEXT_PRIMER, SESSION_STATE, DECISION_LOG, or HANDOFF separately.
The resume script merges all of them into a single compressed brief.
After reading the brief output → begin work on NEXT TASK immediately.

## HANDOFF PROTOCOL

**After every meaningful step (mandatory):**
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent claude \
  --completed "what you just did" --next "exact next action" --files "src/file.py"
```

**Clean session finish:**
```bash
python3 .project-intel/scripts/handoff.py finish --agent claude \
  --completed "what you completed" --next "TASK-XXX: next task"
```

**Commit uncommitted intel files:**
```bash
bash scripts/claude-commit.sh
```

## OUTPUT ROUTING — mandatory on every response

Use XML tags. Routes content to correct destination files automatically.

→ PROJECT FILES (never repeat in chat):
  <gap>architecture gap</gap>               → GAPS.md
  <issue>bug or broken behavior</issue>     → ISSUES.md
  <broken>non-functional component</broken> → BROKEN.md
  <missing>feature not yet built</missing>  → MISSING.md
  <decision>architecture decision</decision>→ DECISION_LOG.md
  <task>implementation task</task>          → OPEN_TASKS.md
  <risk>risk or threat</risk>               → RISK_LOG.md
  <diagnostic>diagnostic finding</diagnostic> → DIAGNOSTICS.md
  <security>security issue</security>       → SECURITY_ISSUES.md
  <debt>technical debt</debt>               → TECH_DEBT.md

→ CHAT: <chat>reply, code, explanation</chat>  or untagged content

RULES: Never write gaps/issues/tasks as plain text. Never duplicate project content in chat.

## NEVER do these
- Do NOT read CONTEXT_PRIMER.md, SESSION_STATE.json, DECISION_LOG.md, or HANDOFF.md at session start — resume.py covers all of them
- Do NOT read MODULE_MAP.json — it is 291KB and will burn your session. The resume brief contains the full slim module map already
- Do NOT read source files to understand the project — the brief has module map + signal flow
- Do NOT ask the user to explain the project
- Do NOT open source files until immediately before editing them

## ALWAYS do these
- Run resume.py as the very first action (see top of this file)
- Checkpoint via handoff.py after every meaningful change
- Use the module map embedded in the resume.py output for structure questions — never open MODULE_MAP.json
- Use XML routing tags on every response
- Read one specific source file only when about to modify it

## Project identity
Production algorithmic trading bot. Python 3.11 + FastAPI + XGBoost + GaussianHMM + React.
Primary: Binance. Secondary: OKX. Paper-first, live-gated.
Signal: Exchange → Features → Regime → Models → Filters → Sizing → Gates → Executor → API
