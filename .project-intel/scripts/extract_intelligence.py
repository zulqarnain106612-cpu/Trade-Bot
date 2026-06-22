#!/usr/bin/env python3
"""
Project Intelligence Extractor
================================
Transforms a codebase into a compact, structured knowledge base.
Agents read THIS output (~3-8KB) instead of raw source (~500KB+).

Output: .project-intel/ directory with:
  - ARCHITECTURE.md       Full system understanding (no code)
  - MODULE_MAP.json       Every module: purpose, inputs, outputs, dependencies
  - DECISION_LOG.md       Architecture decisions already made
  - OPEN_TASKS.md         What needs to be done next
  - CONTEXT_PRIMER.md     Single-file agent bootstrap (<2000 tokens)
  - SESSION_STATE.json    Current implementation state
"""

import ast
import json
import os
import re
import sys
# re already imported above — used in build_context_primer for gap extraction
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────

SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv", "dist",
    "build", ".mypy_cache", ".ruff_cache", "htmlcov", ".pytest_cache",
    "migrations", "artifacts", "models"
}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".lock", ".log", ".db", ".sqlite"}
MAX_FILE_SIZE_KB = 200  # skip huge files


# ── AST-based Python extraction (reads structure, not code) ──────────────────

def extract_python_structure(path: Path) -> dict:
    """Extract module structure via AST — zero code content captured."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
    except Exception:
        return {"error": "parse_failed"}

    info = {
        "classes": [],
        "functions": [],
        "imports": [],
        "constants": [],
        "docstring": ast.get_docstring(tree) or "",
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = [alias.name for alias in node.names]
                info["imports"].append(f"from {mod} import {', '.join(names)}")
            else:
                info["imports"].append(f"import {', '.join(a.name for a in node.names)}")

        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in ast.walk(node)
                if isinstance(n, ast.FunctionDef) and n.col_offset > node.col_offset
            ]
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            doc = ast.get_docstring(node) or ""
            info["classes"].append({
                "name": node.name,
                "bases": bases,
                "methods": methods,
                "docstring": doc[:120] if doc else "",
                "line": node.lineno,
            })

        elif isinstance(node, ast.FunctionDef) and node.col_offset == 0:
            args = [a.arg for a in node.args.args]
            doc = ast.get_docstring(node) or ""
            returns = ""
            if node.returns and hasattr(ast, "unparse"):
                returns = ast.unparse(node.returns)
            info["functions"].append({
                "name": node.name,
                "args": args,
                "returns": returns,
                "docstring": doc[:120] if doc else "",
                "line": node.lineno,
            })

        elif isinstance(node, ast.Assign) and node.col_offset == 0:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    info["constants"].append(target.id)

    # Keep imports deduplicated and only internal/key ones
    info["imports"] = list(dict.fromkeys(info["imports"]))[:20]
    return info


def extract_js_structure(path: Path) -> dict:
    """Lightweight JS/JSX structure via regex — no AST needed."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    info = {
        "exports": re.findall(r"export\s+(?:default\s+)?(?:function|class|const)\s+(\w+)", source),
        "imports": re.findall(r"import\s+.*?\s+from\s+['\"](.+?)['\"]", source)[:10],
        "components": re.findall(r"(?:function|const)\s+([A-Z]\w+)\s*[=(]", source),
        "hooks": re.findall(r"(?:const|let)\s+\w+\s*=\s*(use\w+)\(", source),
        "lines": source.count("\n"),
    }
    return info


def count_lines(path: Path) -> int:
    try:
        return path.read_text(errors="ignore").count("\n")
    except Exception:
        return 0


# ── Walk project ──────────────────────────────────────────────────────────────

def walk_project(root: Path) -> dict:
    """Walk project tree, extract structural intel from every file."""
    modules = {}
    file_tree = []

    for fpath in sorted(root.rglob("*")):
        if fpath.is_dir():
            continue
        if any(skip in fpath.parts for skip in SKIP_DIRS):
            continue
        if fpath.suffix in SKIP_EXTENSIONS:
            continue
        if fpath.stat().st_size > MAX_FILE_SIZE_KB * 1024:
            continue

        rel = fpath.relative_to(root)
        rel_str = str(rel)
        file_tree.append(rel_str)

        if fpath.suffix == ".py":
            modules[rel_str] = extract_python_structure(fpath)
            modules[rel_str]["lines"] = count_lines(fpath)
            modules[rel_str]["type"] = "python"

        elif fpath.suffix in (".js", ".jsx", ".ts", ".tsx"):
            modules[rel_str] = extract_js_structure(fpath)
            modules[rel_str]["type"] = "javascript"

        elif fpath.suffix in (".md", ".txt", ".rst"):
            try:
                content = fpath.read_text(errors="ignore")
                modules[rel_str] = {
                    "type": "docs",
                    "lines": content.count("\n"),
                    "preview": content[:300],
                }
            except Exception:
                pass

        elif fpath.suffix in (".yml", ".yaml", ".toml", ".cfg", ".ini", ".env"):
            modules[rel_str] = {"type": "config", "lines": count_lines(fpath)}

        elif fpath.suffix == ".json" and fpath.stat().st_size < 10240:
            try:
                data = json.loads(fpath.read_text())
                modules[rel_str] = {
                    "type": "json",
                    "keys": list(data.keys())[:20] if isinstance(data, dict) else f"array[{len(data)}]",
                }
            except Exception:
                modules[rel_str] = {"type": "json"}

    return {"modules": modules, "file_tree": file_tree}


# ── Generate outputs ──────────────────────────────────────────────────────────

def build_module_map(root: Path, data: dict) -> str:
    """Build compact MODULE_MAP.json."""
    module_map = {}
    for path, info in data["modules"].items():
        if info.get("type") != "python":
            continue
        entry = {
            "purpose": info.get("docstring", "")[:100] or infer_purpose(path),
            "lines": info.get("lines", 0),
            "classes": [c["name"] for c in info.get("classes", [])],
            "key_functions": [f["name"] for f in info.get("functions", []) if not f["name"].startswith("_")][:8],
            "constants": info.get("constants", [])[:5],
            "imports_from": [
                i.split("import")[0].replace("from ", "").strip()
                for i in info.get("imports", [])
                if i.startswith("from src") or i.startswith("from .")
            ][:6],
        }
        module_map[path] = entry
    return json.dumps(module_map, indent=2)


def infer_purpose(path: str) -> str:
    """Infer purpose from filename without reading code."""
    name = Path(path).stem.lower()
    mapping = {
        "fetcher": "Fetches OHLCV and order book data from exchanges",
        "storage": "Async SQLite persistence for bars, trades, equity, audit",
        "pipeline": "Feature engineering — 7 features + triple-barrier labels",
        "detector": "GaussianHMM 3-state regime detection",
        "trainer": "XGBoost direction + meta-label training with CPCV",
        "kelly": "Half-Kelly position sizing",
        "gates": "Sequential hard risk gates with short-circuit logic",
        "base": "Abstract executor interface",
        "paper": "Paper trading executor — all 3 execution modes",
        "live": "Live trading executor via ccxt market orders",
        "filters": "8 professional signal filters",
        "position_sizing": "Carver / AFML / Thorp sizing methods",
        "signal_engine": "Per-timeframe signal pipeline orchestration",
        "orchestrator": "Main async event loop — drives all timeframes",
        "main": "FastAPI REST + WebSocket API",
        "auth": "API key + WS key verification",
        "middleware": "CORS validation",
        "runtime_monitor": "Async health monitor — tick stall, memory, dead tasks",
        "signal_debugger": "KS-test feature drift + model degradation tracker",
        "trade_auditor": "Per-tick decision log with features and gate chain",
        "config": "Pydantic settings, enums, RuntimeConfig",
    }
    return mapping.get(name, f"{name} module")


def build_architecture_md(root: Path, data: dict) -> str:
    """Build human+agent readable architecture document."""
    py_files = [(p, i) for p, i in data["modules"].items() if i.get("type") == "python"]
    total_lines = sum(i.get("lines", 0) for _, i in py_files)

    lines = [
        "# Trade Bot — Architecture Intelligence",
        f"> Auto-generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"{len(py_files)} Python modules | {total_lines:,} total lines",
        "",
        "## System Purpose",
        "Production algorithmic trading bot: Binance (primary) + OKX (secondary).",
        "ML signal stack → risk gates → execution. Paper-first, live-gated.",
        "",
        "## Signal Pipeline (data flow order)",
        "```",
        "Exchange OHLCV/OrderBook",
        "  → fetcher.py          [ccxt, 1m/15m/4h]",
        "  → storage.py          [SQLite WAL, async]",
        "  → pipeline.py         [7 features, triple-barrier labels, CPCV]",
        "  → detector.py         [GaussianHMM 3-state regime]",
        "  → trainer.py          [XGBoost direction P(long) + meta-label P(bet)]",
        "  → filters.py          [8 signal filters: EWM/Hurst/OBV/ATR/MTF]",
        "  → signal_engine.py    [per-timeframe signal score]",
        "  → position_sizing.py  [Half-Kelly + Carver + AFML + Thorp]",
        "  → gates.py            [sequential hard risk gates, short-circuit]",
        "  → paper.py / live.py  [execution: Auto/Restricted/Manual]",
        "  → orchestrator.py     [async event loop]",
        "  → main.py             [FastAPI + WebSocket]",
        "  → React dashboard     [equity, positions, approvals, regime]",
        "```",
        "",
        "## Module Inventory",
    ]

    for path, info in sorted(py_files, key=lambda x: x[0]):
        classes = ", ".join(c["name"] for c in info.get("classes", []))
        fns = ", ".join(f["name"] for f in info.get("functions", []) if not f["name"].startswith("_"))[:5]
        purpose = info.get("docstring", "")[:80] or infer_purpose(path)
        lines.append(f"\n### `{path}` ({info.get('lines', 0)} lines)")
        lines.append(f"**Purpose**: {purpose}")
        if classes:
            lines.append(f"**Classes**: {classes}")
        if fns:
            lines.append(f"**Key functions**: {fns}")

    lines += [
        "",
        "## Risk Architecture",
        "Gates execute sequentially — first fail short-circuits remaining gates:",
        "1. Daily drawdown halt: 2% of starting equity",
        "2. Consecutive loss halt: 3 trades",
        "3. Regime gate: block when HMM state = volatile",
        "4. Max position size: 5% of capital",
        "5. Paper minimum: 30 days required",
        "6. Live gate: OOS Sharpe > 1.5, max DD < 15%, 500+ trades",
        "",
        "## Execution Modes",
        "- AUTOMATIC: fires within risk gates, no approval",
        "- RESTRICTED: auto below notional limit, approval above, 30s timeout skip",
        "- MANUAL: every trade queued for operator approval",
        "",
        "## Timeframes",
        "- 1m: scalping, paper only",
        "- 15m: primary real-money intraday",
        "- 4h: swing, paper only",
        "",
        "## Key Design Decisions (ADR)",
        "- ADR-001: Triple-barrier + CPCV chosen over simple train/test (eliminates lookahead + serial correlation)",
        "- ADR-002: Meta-labeling separates direction from bet confidence",
        "- ADR-003: Fractional diff d=0.4 balances stationarity and memory preservation",
        "- ADR-004: Half-Kelly at 0.5× with 25% ceiling — Thorp conservative for single-strategy",
        "- ADR-005: SQLite WAL for development; migration path to TimescaleDB for live scale",
        "- ADR-006: Paper mode default — live requires explicit env var + gate pass",
        "",
        "## Known Gaps (open architecture items)",
        "- GAP-001: No slippage/market-impact model in live.py (Almgren-Chriss needed)",
        "- GAP-002: HMM regime has no posterior entropy gate (confidence not quantified)",
        "- GAP-003: KS-test drift detection misses label shift (performance-based trigger needed)",
        "- GAP-004: No order state machine (PENDING→FILLED FSM) in live executor",
        "- GAP-005: No portfolio correlation layer for multi-symbol operation",
        "- GAP-006: SQLite write contention under high-frequency multi-timeframe load",
    ]

    return "\n".join(lines)


def build_context_primer(root: Path, data: dict) -> str:
    """Single file an agent reads first — complete bootstrap including output routing protocol."""
    # Pull dynamic gap list from GAPS.md if it exists
    gaps_file = root / ".project-intel" / "GAPS.md"
    gap_lines = ""
    if gaps_file.exists():
        content = gaps_file.read_text()
        headers = re.findall(r"(## Gap-\d+.*?)(?=## Gap-|\Z)", content, re.DOTALL)
        gap_lines = "\n".join(h.split("\n")[0] for h in headers[:8])
    if not gap_lines:
        gap_lines = (
            "GAP-001: No slippage model in live.py\n"
            "GAP-002: HMM has no entropy/confidence gate\n"
            "GAP-003: Drift detection misses label shift\n"
            "GAP-004: No order FSM in live executor\n"
            "GAP-005: No portfolio correlation layer\n"
            "GAP-006: SQLite contention risk at scale"
        )

    return f"""\
# AGENT CONTEXT PRIMER — Trade Bot
## READ THIS FIRST. Do NOT read source files until instructed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## OUTPUT ROUTING PROTOCOL — follow this in EVERY response
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Wrap output in XML tags. System auto-routes to correct destination.

→ PROJECT FILES (never repeat in chat):
  <gap>architecture gap or design hole</gap>           → GAPS.md
  <issue>bug, error, broken behavior</issue>           → ISSUES.md
  <broken>non-functional component</broken>            → BROKEN.md
  <missing>feature that does not exist</missing>       → MISSING.md
  <decision>architecture decision made</decision>      → DECISION_LOG.md
  <task>implementation task identified</task>          → OPEN_TASKS.md
  <risk>risk or threat identified</risk>               → RISK_LOG.md
  <diagnostic>diagnostic finding</diagnostic>          → DIAGNOSTICS.md
  <security>security issue or vulnerability</security> → SECURITY_ISSUES.md
  <debt>technical debt</debt>                          → TECH_DEBT.md

→ CHAT INTERFACE:
  <chat>conversational reply, code, explanation</chat>
  Any untagged content                                 → chat

EXAMPLE:
  <gap>GAP-007: No circuit breaker in live.py. Severity: High.</gap>
  <task>TASK-009: Add tenacity backoff to LiveExecutor.place_order()</task>
  <chat>Found a circuit breaker gap — logged to GAPS.md, task added. Here is the fix: ...</chat>

RULES: Never write gaps/issues/tasks as plain text. Never duplicate project content in chat.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PROJECT — Trade Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Production algorithmic trading bot. Python 3.11 + FastAPI + XGBoost + GaussianHMM + React.
Exchanges: Binance (primary), OKX (secondary). Paper-first, live-gated.

### Module map (use this — do not read source)
src/config.py                    Settings, enums, RuntimeConfig
src/data/fetcher.py              ccxt OHLCV + orderbook (1m/15m/4h)
src/data/storage.py              Async SQLite WAL
src/features/pipeline.py         7 features + triple-barrier + CPCV
src/regime/detector.py           GaussianHMM 3-state
src/models/trainer.py            XGBoost direction + meta-label
src/risk/kelly.py                Half-Kelly (0.5x, 0.25 ceiling)
src/risk/gates.py                Sequential hard risk gates
src/execution/paper.py           Paper executor (Auto/Restricted/Manual)
src/execution/live.py            Live executor (ccxt market orders)
src/strategies/filters.py        8 signal filters
src/strategies/position_sizing.py Carver + AFML + Thorp
src/engine/signal_engine.py      Per-timeframe pipeline
src/engine/orchestrator.py       Async event loop
src/api/main.py                  FastAPI REST + WebSocket
src/diagnostics/                 RuntimeMonitor, SignalDebugger, TradeAuditor
frontend/src/App.jsx             React dashboard

### Signal flow
Exchange → fetch → store → features → regime → models → filters
→ sizing → gates → executor → api → dashboard

### Risk gates (sequential, short-circuit on first fail)
DD>2% | losses>=3 | regime=volatile | pos>5% | paper<30d | live_gate_fail

### Key constants
DAILY_DD_HALT=2%  CONSECUTIVE_LOSS_HALT=3  MAX_POSITION_PCT=5%
KELLY_MULTIPLIER=0.5  KELLY_CEILING=0.25  PAPER_MIN_DAYS=30
LIVE_SHARPE_MIN=1.5  LIVE_MAX_DD=15%  LIVE_MIN_TRADES=500  PRIMARY_TF=15m

### Known gaps (check GAPS.md for full details)
{gap_lines}

### Session rules
1. This file = complete project understanding. No source reading needed.
2. Read SESSION_STATE.json → current progress
3. Read DECISION_LOG.md → past decisions (do not re-debate)
4. Read one specific source file ONLY when about to modify it
5. Use MODULE_MAP.json for structural questions
6. Use OUTPUT ROUTING PROTOCOL above for every response
"""


def build_session_state() -> str:
    """Initial session state JSON."""
    return json.dumps({
        "last_updated": datetime.now().isoformat(),
        "implementation_status": {
            "core_pipeline": "complete",
            "risk_gates": "complete",
            "paper_executor": "complete",
            "live_executor": "complete — missing order FSM",
            "api": "complete",
            "frontend": "complete",
            "diagnostics": "complete",
            "slippage_model": "NOT STARTED — GAP-001",
            "entropy_gate": "NOT STARTED — GAP-002",
            "performance_drift_trigger": "NOT STARTED — GAP-003",
            "order_fsm": "NOT STARTED — GAP-004",
            "portfolio_correlation_layer": "NOT STARTED — GAP-005",
        },
        "current_focus": "none — set this when starting a session",
        "last_file_modified": "none",
        "open_decisions": [
            "Choose TimescaleDB vs QuestDB for storage migration",
            "Decide online retraining trigger: time-based vs performance-based",
            "Define order FSM states for partial fills",
        ],
        "next_recommended_task": "GAP-002: Add HMM posterior entropy gate in src/regime/detector.py",
    }, indent=2)


def build_decision_log() -> str:
    return """\
# Architecture Decision Log

## ADR-001: Triple-barrier labeling + CPCV validation
**Date**: Project inception
**Decision**: Use triple-barrier method (AFML Ch.3) for labels + Combinatorial Purged Cross-Validation (Ch.7)
**Rationale**: Eliminates lookahead bias and serial correlation that breaks standard train/test splits
**Status**: Implemented in src/features/pipeline.py

## ADR-002: Meta-labeling architecture
**Date**: Project inception
**Decision**: Separate XGBoost model for direction (P(long)) and meta-label gate (P(bet))
**Rationale**: Separates "which direction" from "should we bet at all" — reduces false positives
**Status**: Implemented in src/models/trainer.py

## ADR-003: Fractional differencing d=0.4
**Date**: Project inception
**Decision**: Apply fractional diff at d=0.4 to price series
**Rationale**: Achieves stationarity while preserving long memory — López de Prado recommendation
**Status**: Implemented in src/features/pipeline.py

## ADR-004: Half-Kelly with ceiling
**Date**: Project inception
**Decision**: Kelly multiplier=0.5, ceiling=0.25 (25% max position)
**Rationale**: Full Kelly is theoretically optimal but practically causes catastrophic drawdowns; Thorp recommends 0.5×
**Status**: Implemented in src/risk/kelly.py

## ADR-005: SQLite WAL for development
**Date**: Project inception
**Decision**: Use SQLite with WAL mode for all storage
**Rationale**: Zero-dependency, sufficient for single-symbol development and paper trading
**Consequence**: Will need migration to TimescaleDB/QuestDB before multi-symbol live trading
**Status**: Implemented in src/data/storage.py — migration NOT started

## ADR-006: Paper mode as default, live requires explicit unlock
**Date**: Project inception
**Decision**: Default mode=paper; live requires TRADING_MODE=live in .env + all gate passes
**Rationale**: Safety-first — accidental live trading is worse than missed opportunity
**Status**: Implemented in src/execution/ and src/risk/gates.py

---
## Add new decisions below this line when implementing changes
"""


def build_open_tasks() -> str:
    return """\
# Open Tasks — Prioritized

## P0 — Pre-live requirements

### TASK-001: Slippage + market impact model [GAP-001]
**File**: src/risk/ (new file: slippage.py)
**What**: Almgren-Chriss model — expected_slippage_bps = spread_bps + impact_coeff * sqrt(qty / adv_20d)
**Why**: Market orders on Binance have real spread + impact costs; Kelly without this overstates edge
**Interface needed**:
  SlippageModel.estimate(symbol, qty, adv_20d) -> SlippageEstimate
  SlippageModel.veto_if_negative_ev(signal, slippage) -> bool
**Wire into**: gates.py as gate 0 (before all other gates)

### TASK-002: HMM posterior entropy gate [GAP-002]
**File**: src/regime/detector.py
**What**: hmm.predict_proba() → compute entropy → if entropy > threshold → position scalar *= 0.5
**Why**: HMM gives state but not confidence; regime transitions are highest-risk moments
**Change**: detector.py predict() should return (state, confidence, entropy) not just state
**Wire into**: signal_engine.py — apply entropy scalar before sizing

## P1 — Reliability improvements

### TASK-003: Performance-based model degradation trigger [GAP-003]
**File**: src/diagnostics/signal_debugger.py
**What**: Rolling 50-trade accuracy + rolling 100-trade Sharpe; trigger retrain alert if below threshold
**Why**: KS-test catches covariate shift but not label shift (feature→return relationship change)
**Threshold**: accuracy < 0.52 OR rolling_sharpe < 0.8 → alert + tighten meta-label threshold to 0.65

### TASK-004: Order state machine in live executor [GAP-004]
**File**: src/execution/ (new file: order_fsm.py)
**States**: PENDING → SUBMITTED → PARTIAL_FILL → FILLED → CLOSED | REJECTED | TIMEOUT
**Why**: Binance market orders can partially fill; without FSM position size vs intended size diverges
**Wire into**: live.py — all order placement goes through FSM

## P2 — Scale preparation

### TASK-005: Portfolio correlation layer [GAP-005]
**File**: src/risk/ (new file: correlation.py)
**What**: Compute portfolio beta vs BTC benchmark; reduce Kelly when beta > 1.3
**Why**: Multi-symbol correlated drawdown can breach 2% daily halt faster than per-symbol Kelly predicts

### TASK-006: Storage migration to TimescaleDB [GAP-006]
**File**: src/data/storage.py
**What**: Replace SQLite with asyncpg + TimescaleDB hypertables
**Why**: SQLite WAL serializes writes; under 3 timeframes + audit log = lock contention at scale

## P3 — Intelligence enhancements

### TASK-007: Prometheus metrics endpoint
**File**: src/api/main.py
**What**: GET /metrics → Prometheus format for Grafana dashboarding
**Metrics**: signal_score, regime_state, kelly_fraction, gate_pass_rate, model_accuracy_rolling

### TASK-008: Online learning hook
**File**: src/models/ (new file: online_trainer.py)
**What**: river or vowpal wabbit for incremental model updates without full retrain
**Why**: XGBoost batch retrain is expensive; online learning catches drift faster
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_intelligence.py /path/to/project")
        sys.exit(1)

    root = Path(sys.argv[1]).resolve()
    if not root.exists():
        print(f"Error: {root} does not exist")
        sys.exit(1)

    output_dir = root / ".project-intel"
    output_dir.mkdir(exist_ok=True)

    print(f"Scanning {root} ...")
    data = walk_project(root)
    py_count = sum(1 for v in data["modules"].values() if v.get("type") == "python")
    print(f"Found {len(data['file_tree'])} files, {py_count} Python modules")

    print("Building intelligence outputs...")

    # MODULE_MAP.json
    module_map = build_module_map(root, data)
    (output_dir / "MODULE_MAP.json").write_text(module_map)

    # Raw scan data
    (output_dir / "RAW_SCAN.json").write_text(
        json.dumps({"file_tree": data["file_tree"]}, indent=2)
    )

    # ARCHITECTURE.md
    arch = build_architecture_md(root, data)
    (output_dir / "ARCHITECTURE.md").write_text(arch)

    # CONTEXT_PRIMER.md — the key file agents read first
    primer = build_context_primer(root, data)
    (output_dir / "CONTEXT_PRIMER.md").write_text(primer)

    # SESSION_STATE.json — only write if not exists (preserve session history)
    state_file = output_dir / "SESSION_STATE.json"
    if not state_file.exists():
        state_file.write_text(build_session_state())
        print("Created SESSION_STATE.json (fresh)")
    else:
        print("Preserved existing SESSION_STATE.json")

    # DECISION_LOG.md — only write if not exists
    dl_file = output_dir / "DECISION_LOG.md"
    if not dl_file.exists():
        dl_file.write_text(build_decision_log())
        print("Created DECISION_LOG.md (fresh)")
    else:
        print("Preserved existing DECISION_LOG.md")

    # OPEN_TASKS.md — always regenerate from ground truth
    (output_dir / "OPEN_TASKS.md").write_text(build_open_tasks())

    # Summary
    sizes = {}
    for f in output_dir.iterdir():
        sizes[f.name] = f.stat().st_size
    total = sum(sizes.values())

    print(f"\n✓ Intelligence layer built at {output_dir}")
    print(f"  Total size: {total/1024:.1f} KB (vs source: ~500KB+)")
    print("\nFiles generated:")
    for name, size in sorted(sizes.items()):
        print(f"  {name:<30} {size/1024:.1f} KB")

    print("\n── Agent Instructions ──────────────────────────────────────")
    print("Tell your agent:")
    print(f"  'Read {output_dir}/CONTEXT_PRIMER.md first.")
    print("   Do NOT read source files unless you need to edit a specific file.")
    print("   Use MODULE_MAP.json for any structural question.")
    print("   Check SESSION_STATE.json for current progress.'")


if __name__ == "__main__":
    main()
