# Agent Prompt Templates
# ========================
# Copy-paste these prompts when starting a new session with any agent.
# The CONTEXT_PRIMER does the heavy lifting — agents never read raw source again.


# ════════════════════════════════════════════════════════════════════════════
# TEMPLATE A — Session Start (use this EVERY time you open a new chat)
# ════════════════════════════════════════════════════════════════════════════

SESSION_START = """
You are a senior architect working on Trade Bot — a production algorithmic trading system.

MANDATORY FIRST STEPS (do these before anything else):
1. Read `.project-intel/CONTEXT_PRIMER.md` — this is your complete project understanding
2. Read `.project-intel/SESSION_STATE.json` — this tells you what has been done and what's next
3. Read `.project-intel/DECISION_LOG.md` — these are decisions already made, do not re-debate them

RULES FOR THIS SESSION:
- Do NOT read any source file unless you are about to modify that specific file
- Do NOT ask me to explain the project — CONTEXT_PRIMER.md has everything
- Do NOT re-read files you already modified — update SESSION_STATE.json instead
- For any structural question ("what does X module do?") read MODULE_MAP.json
- When you finish this session, tell me exactly what to run in update_session.py

Your task for this session: {TASK_DESCRIPTION}
"""


# ════════════════════════════════════════════════════════════════════════════
# TEMPLATE B — Targeted implementation (when you know exactly what to build)
# ════════════════════════════════════════════════════════════════════════════

TARGETED_IMPLEMENTATION = """
Project: Trade Bot (algorithmic trading — Python/FastAPI/XGBoost)
Context: Read `.project-intel/CONTEXT_PRIMER.md` for full understanding.

Task: Implement {TASK_NAME}
File to create/modify: {FILE_PATH}
Specification: {SPEC_FROM_OPEN_TASKS}

Constraints:
- Match the existing code style (pydantic-settings, async/await, type hints)
- Wire it into the pipeline at: {WHERE_TO_WIRE}
- Add a test in tests/ for the new component
- Do NOT read any other files — CONTEXT_PRIMER.md has what you need

After implementing, output the update_session.py command I should run.
"""


# ════════════════════════════════════════════════════════════════════════════
# TEMPLATE C — Continue interrupted session
# ════════════════════════════════════════════════════════════════════════════

CONTINUE_SESSION = """
Continuing Trade Bot implementation session.

Read these three files only (in order):
1. `.project-intel/CONTEXT_PRIMER.md`
2. `.project-intel/SESSION_STATE.json`  ← shows exactly where we stopped
3. `.project-intel/DECISION_LOG.md`

Then continue from where SESSION_STATE.json says we stopped.
Do NOT re-read any source files. The session state tells you everything.
"""


# ════════════════════════════════════════════════════════════════════════════
# TEMPLATE D — Architecture review / decision making
# ════════════════════════════════════════════════════════════════════════════

ARCHITECTURE_REVIEW = """
You are a principal architect reviewing Trade Bot.

Read `.project-intel/ARCHITECTURE.md` for the full system design.
Read `.project-intel/OPEN_TASKS.md` for known gaps.
Read `.project-intel/DECISION_LOG.md` for decisions already made.

Question / decision needed: {QUESTION}

Provide:
1. Your recommendation with architectural reasoning
2. Trade-offs considered
3. The ADR entry I should add to DECISION_LOG.md
4. Which OPEN_TASKS.md item this resolves or creates

Do NOT read source files — ARCHITECTURE.md has everything you need.
"""


# ════════════════════════════════════════════════════════════════════════════
# TEMPLATE E — VSCode Copilot (inline, no chat session)
# ════════════════════════════════════════════════════════════════════════════

COPILOT_FILE_HEADER = """
# PROJECT CONTEXT (read this, not the rest of the codebase)
# Full architecture: .project-intel/CONTEXT_PRIMER.md
# This file's role: {FILE_PURPOSE}
# Depends on: {DEPENDENCIES}
# Used by: {CONSUMERS}
# Open gaps in this file: {OPEN_GAPS}
"""


# ════════════════════════════════════════════════════════════════════════════
# TEMPLATE F — Ollama / local model (token-budget-aware)
# ════════════════════════════════════════════════════════════════════════════

OLLAMA_COMPACT = """
[Trade Bot context — read this, ignore source files]
{PASTE_CONTEXT_PRIMER_CONTENT_HERE}

Task: {TASK}
File: {FILE}
"""


# ════════════════════════════════════════════════════════════════════════════
# EXAMPLE USAGE
# ════════════════════════════════════════════════════════════════════════════

EXAMPLE_USAGE = """
Example — implementing the entropy gate (TASK-002):

  SESSION_START.format(
    TASK_DESCRIPTION="Implement TASK-002: Add posterior entropy gate to GaussianHMM detector.
    Spec is in .project-intel/OPEN_TASKS.md under TASK-002.
    Target file: src/regime/detector.py"
  )

After the session, run:
  python .project-intel/scripts/update_session.py /path/to/Trade-Bot \\
    --completed "TASK-002: HMM entropy gate implemented" \\
    --modified "src/regime/detector.py" "src/engine/signal_engine.py" \\
    --decision "ADR-007: Entropy threshold 0.8 nats chosen after empirical testing on BTC 2023 data" \\
    --next "TASK-001: Implement slippage model in src/risk/slippage.py" \\
    --focus "slippage_model"
"""
