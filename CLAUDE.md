# Trade Bot — Claude Instructions

## ══ MANDATORY SESSION START — ONE COMMAND, NO EXCEPTIONS ══

```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```

**Run this FIRST. Read its output. That IS your complete context.**
After reading the brief → begin work on NEXT TASK immediately. No other reads needed.

## ══ FILE SIZE DANGER TABLE — NEVER OPEN THESE ══

| File | Size | Why forbidden |
|------|------|---------------|
| `.project-intel/MODULE_MAP.json` | 291KB | resume.py slim map replaces it |
| `.project-intel/ARCHITECTURE.md` | 160KB | stale, resume.py covers structure |
| `.project-intel/RAW_SCAN.json` | 87KB | internal tool output only |
| `.project-intel/rag.db` | 8.9MB | binary database |
| `frontend/package-lock.json` | 230KB | lockfile, never read |
| `requirements.lock` | 173KB | lockfile, never read |
| `.project-intel/scripts/extract_intelligence.py` | 29KB | internal only |
| `.project-intel/scripts/cognitive_layer.py` | 23KB | internal only |
| `.project-intel/GAPS.md` | 34KB | summary in resume brief |
| `.project-intel/TECH_DEBT.md` | 16KB | summary in resume brief |
| `.project-intel/SECURITY_ISSUES.md` | 14KB | summary in resume brief |
| `.project-intel/SESSION_STATE.json` | 11KB | resume.py reads and embeds this |
| `.project-intel/CONTEXT_PRIMER.md` | 4.6KB | superseded by resume.py |
| `Vulner-Fix.md` | 23KB | historical doc, not actionable |

These are all listed in `.claudeignore`. Reading any one burns the session.

## HANDOFF PROTOCOL

**After every meaningful step:**
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent claude \
  --completed "what you just did" --next "exact next action" --files "src/file.py"
```

**Clean session finish:**
```bash
python3 .project-intel/scripts/handoff.py finish --agent claude \
  --completed "what you completed" --next "TASK-XXX: next task"
```

**Commit intel files:**
```bash
bash scripts/claude-commit.sh
```

## OUTPUT ROUTING — mandatory on every response

→ PROJECT FILES (never repeat in chat):
  `<gap>` → GAPS.md | `<issue>` → ISSUES.md | `<broken>` → BROKEN.md
  `<missing>` → MISSING.md | `<decision>` → DECISION_LOG.md | `<task>` → OPEN_TASKS.md
  `<risk>` → RISK_LOG.md | `<security>` → SECURITY_ISSUES.md | `<debt>` → TECH_DEBT.md

→ CHAT: `<chat>` or untagged content

## NEVER do these
- Do NOT read any file listed in the danger table above or in `.claudeignore`
- Do NOT read source files to understand the project — resume brief has module map
- Do NOT read MODULE_MAP.json — 291KB, session-killer, slim map is in the brief
- Do NOT read files to orient — the resume.py output IS orientation

## ALWAYS do these
- Run resume.py as the very first action
- Checkpoint after every meaningful change
- Read a source file ONLY immediately before editing it
- Use XML routing tags on every response

## Project identity
Production algorithmic trading bot. Python 3.11 + FastAPI + XGBoost + GaussianHMM + React.
Primary: Binance. Secondary: OKX. Paper-first, live-gated.
Signal: Exchange → Features → Regime → Models → Filters → Sizing → Gates → Executor → API
