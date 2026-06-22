#!/usr/bin/env python3
"""
CONTEXT_PRIMER builder with Output Routing Protocol embedded.
Agents read this once → they know the project AND how to format output.
"""

CONTEXT_PRIMER_TEMPLATE = """\
# AGENT CONTEXT PRIMER — {project_name}
## READ THIS FIRST. Do NOT read source files until instructed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## OUTPUT ROUTING PROTOCOL (follow this every response)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Structure EVERY response using these XML tags.
The system auto-routes tagged content to correct destination.

TAGS THAT GO TO PROJECT FILES (never repeat in chat):
  <gap>      Architecture gap or design hole        → GAPS.md
  <issue>    Bug, error, or broken behavior         → ISSUES.md
  <broken>   Component that is non-functional       → BROKEN.md
  <missing>  Feature or module that does not exist  → MISSING.md
  <decision> Architecture decision made this session→ DECISION_LOG.md
  <task>     Implementation task identified         → OPEN_TASKS.md
  <risk>     Risk assessment or threat identified   → RISK_LOG.md
  <diagnostic> Diagnostic finding or analysis      → DIAGNOSTICS.md
  <security> Security issue or vulnerability        → SECURITY_ISSUES.md
  <debt>     Technical debt identified              → TECH_DEBT.md

TAGS THAT GO TO CHAT INTERFACE:
  <chat>     Conversational reply, explanation      → chat only
  Untagged content                                  → chat only

EXAMPLE RESPONSE FORMAT:
  <gap>
  GAP-007: No circuit breaker in live executor. If Binance API returns
  5xx errors repeatedly, the executor will keep retrying and exhaust
  rate limits. Needs exponential backoff with max_retries=3.
  Severity: High. Affects: src/execution/live.py
  </gap>

  <task>
  TASK-009: Implement circuit breaker in live.py
  File: src/execution/live.py
  Pattern: tenacity library with exponential backoff
  Wire into: LiveExecutor.place_order()
  </task>

  <chat>
  I found a circuit breaker gap in the live executor. I've logged it
  to GAPS.md and added TASK-009 to OPEN_TASKS.md. Here's the implementation:
  [code follows in chat]
  </chat>

RULES:
- NEVER write gaps/issues/tasks as plain chat text — always tag them
- NEVER duplicate project-bound content in chat (it's already filed)
- Always write a brief <chat> summary after any project-bound blocks
- Keep <chat> content focused — details live in the project files

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PROJECT UNDERSTANDING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Project: {project_name}
{project_description}

### Module map (do not read source — use this)
{module_map_summary}

### Signal flow
{signal_flow}

### Risk gates (sequential, first fail stops execution)
{risk_gates}

### Current session state
Focus:      {current_focus}
Next task:  {next_task}
Last files: {last_modified}

### Known issues (read before implementing)
Check these files first:
  .project-intel/GAPS.md
  .project-intel/ISSUES.md
  .project-intel/OPEN_TASKS.md
  .project-intel/DECISION_LOG.md

### Key constants (never guess)
{key_constants}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## SESSION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Read this file — done. You now understand the project.
2. Read SESSION_STATE.json for current progress.
3. Read DECISION_LOG.md for past decisions.
4. Only open a source file when you are about to modify IT SPECIFICALLY.
5. Use MODULE_MAP.json for any "what does X do" question.
6. Use the OUTPUT ROUTING PROTOCOL above for every response.
7. Never ask the user to explain the project — it's all here.
"""


TRADE_BOT_PRIMER = CONTEXT_PRIMER_TEMPLATE.format(
    project_name="Trade Bot",
    project_description=(
        "Production algorithmic trading bot. Python 3.11 + FastAPI + "
        "XGBoost + GaussianHMM + React.\n"
        "Exchanges: Binance (primary), OKX (secondary). Paper-first, live-gated."
    ),
    module_map_summary="""\
src/config.py              Settings, enums, RuntimeConfig
src/data/fetcher.py        ccxt OHLCV + orderbook fetch (1m/15m/4h)
src/data/storage.py        Async SQLite WAL persistence
src/features/pipeline.py   7 features, triple-barrier labels, CPCV
src/regime/detector.py     GaussianHMM 3-state (range/trend/volatile)
src/models/trainer.py      XGBoost direction P(long) + meta-label P(bet)
src/risk/kelly.py          Half-Kelly sizing (0.5× multiplier, 0.25 ceiling)
src/risk/gates.py          Hard risk gates (sequential, short-circuit)
src/execution/paper.py     Paper executor (Auto/Restricted/Manual modes)
src/execution/live.py      Live executor (ccxt market orders)
src/strategies/filters.py  8 signal filters (EWM/Hurst/OBV/ATR/Vol/MTF)
src/strategies/position_sizing.py  Carver + AFML + Thorp sizing
src/engine/signal_engine.py        Per-timeframe signal pipeline
src/engine/orchestrator.py         Async event loop
src/api/main.py            FastAPI REST + WebSocket
src/diagnostics/           RuntimeMonitor, SignalDebugger, TradeAuditor
frontend/src/App.jsx       React dashboard""",
    signal_flow=(
        "Exchange → fetch → store → features → regime → direction_model\n"
        "→ meta_label → filters → signal_score → sizing → gates → executor → api → dashboard"
    ),
    risk_gates=(
        "DD>2% | losses>=3 | regime=volatile | pos>5% | paper<30d | live_gate_fail"
    ),
    current_focus="{current_focus}",
    next_task="{next_task}",
    last_modified="{last_modified}",
    key_constants="""\
DAILY_DD_HALT=2%   CONSECUTIVE_LOSS_HALT=3   MAX_POSITION_PCT=5%
KELLY_MULTIPLIER=0.5   KELLY_CEILING=0.25   PAPER_MIN_DAYS=30
LIVE_SHARPE_MIN=1.5   LIVE_MAX_DD=15%   LIVE_MIN_TRADES=500
PRIMARY_TIMEFRAME=15m""",
)


def render_primer(state: dict) -> str:
    """Render the primer with live session state values."""
    return TRADE_BOT_PRIMER.format(
        current_focus=state.get("current_focus", "not set"),
        next_task=state.get("next_recommended_task", "check OPEN_TASKS.md"),
        last_modified=", ".join(state.get("last_files_modified", ["none"])),
    )


if __name__ == "__main__":
    # Print the primer for debugging
    print(render_primer({
        "current_focus": "slippage_model",
        "next_recommended_task": "TASK-001: Implement slippage model in src/risk/slippage.py",
        "last_files_modified": ["src/regime/detector.py"],
    }))
