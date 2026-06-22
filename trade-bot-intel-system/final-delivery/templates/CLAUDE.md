# Trade Bot — Claude Instructions

## MANDATORY: Read these files before anything else
1. `.project-intel/CONTEXT_PRIMER.md` — complete project understanding + output routing protocol
2. `.project-intel/SESSION_STATE.json` — current progress and what's next
3. `.project-intel/DECISION_LOG.md` — decisions already made

## OUTPUT ROUTING PROTOCOL — mandatory for every response
Use XML tags. System auto-routes tagged content to correct destination.

→ PROJECT FILES (do not repeat in chat):
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
Always follow project-bound blocks with a brief <chat> summary.

## NEVER do these
- Do NOT read entire source files to understand the project
- Do NOT ask the user to explain what the project does
- Do NOT re-read files from a previous session — use SESSION_STATE.json
- Do NOT open more than one specific source file at a time

## ALWAYS do these
- Read the 3 mandatory files above at session start
- Use MODULE_MAP.json for structural questions
- Check GAPS.md and ISSUES.md before implementing anything
- Use XML routing tags on every response

## Project identity
Production algorithmic trading bot. Python 3.11 + FastAPI + XGBoost + GaussianHMM + React.
Primary: Binance. Secondary: OKX. Paper-first, live-gated.
Signal: Exchange → Features → Regime → Models → Filters → Sizing → Gates → Executor → API
