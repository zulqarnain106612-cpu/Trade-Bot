#!/usr/bin/env python3
"""
Smart Context Builder
======================
Assembles the MINIMUM context for any agent query.
Pulls from 3 sources, token-budget aware:

  1. CONTEXT_PRIMER   — always included (~600 tokens, routing protocol)
  2. RAG chunks       — relevant source code (~400 tokens per chunk, top 3)
  3. Domain knowledge — relevant cognitive layer (~500 tokens, top 2 entries)
  4. Session state    — always included (~200 tokens)

Total typical context: 2,000-3,000 tokens
vs previous approach:  15,000-50,000 tokens (full file reading)

Token reduction: 85-95%

Usage:
  python3 context_builder.py "implement entropy gate in detector.py"
  python3 context_builder.py "how does kelly sizing work" --no-rag
  python3 context_builder.py "fix slippage in live.py" --files src/execution/live.py
"""

import ast
import json
import re
import sys
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────
def find_project_root(start: Path = None) -> Path | None:
    check = (start or Path.cwd()).resolve()
    while check != check.parent:
        if (check / ".project-intel").exists():
            return check
        check = check.parent
    return None

ROOT         = find_project_root() or Path(".")
INTEL_DIR    = ROOT / ".project-intel"
KNOWLEDGE_DIR= INTEL_DIR / "knowledge"
DB_PATH      = INTEL_DIR / "rag.db"

# Token budget allocation
BUDGET_TOTAL     = 3000
BUDGET_PRIMER    = 600
BUDGET_STATE     = 200
BUDGET_RAG       = 1200   # compact summaries, not full files
BUDGET_KNOWLEDGE = 800    # up to 2 domain entries


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def summarize_text(text: str, max_tokens: int = 400) -> str:
    """Compact a raw text block into a short, information-dense summary."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_tokens * 4:
        return cleaned
    return cleaned[: max_tokens * 4 - 20] + "…"


def truncate_to_budget(text: str, budget_tokens: int) -> str:
    if estimate_tokens(text) <= budget_tokens:
        return text
    return text[: budget_tokens * 4 - 20] + "\n... [truncated]"


def summarize_source_file(path: str | Path, query: str = "", max_tokens: int = 600) -> str:
    """Create a compact AST-based summary for a source file instead of dumping raw code."""
    target = Path(path)
    if not target.exists():
        return f"[missing] {target}"

    try:
        text = target.read_text(errors="ignore")
    except Exception:
        return f"[unreadable] {target}"

    try:
        rel_path = target.relative_to(ROOT)
    except Exception:
        rel_path = target.name

    lines = text.splitlines()
    if target.suffix.lower() != ".py":
        preview = " ".join(line.strip() for line in lines[:8] if line.strip())
        preview = preview[:220]
        return f"{rel_path} — {len(lines)} lines; preview: {preview or 'no preview'}"

    try:
        tree = ast.parse(text)
    except SyntaxError:
        preview = " ".join(line.strip() for line in lines[:8] if line.strip())
        return f"{rel_path} — {len(lines)} lines; non-parseable Python preview: {preview[:220]}"

    terms = {term for term in re.split(r"[^a-z0-9]+", query.lower()) if len(term) >= 3}
    definitions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node) or ""
            doc = re.sub(r"\s+", " ", doc).strip()
            definitions.append((node.name, "func", doc))
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            doc = re.sub(r"\s+", " ", doc).strip()
            definitions.append((node.name, "class", doc))

    if not definitions:
        return f"{rel_path} — {len(lines)} lines; no top-level functions/classes"

    relevant = []
    for name, kind, doc in definitions:
        name_l = name.lower()
        if not terms or any(term in name_l for term in terms):
            relevant.append((name, kind, doc))
    if not relevant:
        relevant = definitions[:4]

    summary_parts = [f"{rel_path} — {len(lines)} lines, {len(definitions)} top-level symbols"]
    for name, kind, doc in relevant[:6]:
        label = "func" if kind == "func" else "class"
        if doc:
            summary_parts.append(f"- {label} {name}: {doc[:110]}")
        else:
            summary_parts.append(f"- {label} {name}: compact interface only")
    if len(definitions) > len(relevant):
        summary_parts.append(f"- ... {len(definitions) - len(relevant)} additional symbols omitted")

    return truncate_to_budget("\n".join(summary_parts), max_tokens)


def load_primer() -> str:
    f = INTEL_DIR / "CONTEXT_PRIMER.md"
    return f.read_text() if f.exists() else ""


def load_session_state() -> str:
    f = INTEL_DIR / "SESSION_STATE.json"
    if not f.exists():
        return ""
    state = json.loads(f.read_text())
    return (
        f"[SESSION STATE]\n"
        f"Focus: {state.get('current_focus','not set')}\n"
        f"Next task: {state.get('next_recommended_task','check OPEN_TASKS.md')}\n"
        f"Last commit: {state.get('last_commit_message','none')}\n"
        f"Last files: {', '.join(state.get('last_files_modified', ['none']))}\n"
    )


def load_rag(query: str, top_k: int = 3) -> str:
    if not DB_PATH.exists():
        return ""
    # Always import from project's own scripts dir — no daemon dependency
    _scripts = str(INTEL_DIR / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    from rag_engine import BM25Index, format_results
    idx     = BM25Index(DB_PATH)
    results = idx.query(query, top_k=top_k)
    if not results:
        return ""
    # Trim each chunk to stay in budget
    per_chunk = BUDGET_RAG // max(len(results), 1)
    for r in results:
        if estimate_tokens(r["content"]) > per_chunk:
            r["content"] = r["content"][:per_chunk * 4] + "\n... [truncated]"
    return format_results(results, query)


def load_knowledge(query: str) -> str:
    if not KNOWLEDGE_DIR.exists():
        return ""
    _scripts = str(INTEL_DIR / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    from cognitive_layer import load_relevant
    return load_relevant(query, KNOWLEDGE_DIR, max_tokens=BUDGET_KNOWLEDGE)


def load_specific_files(file_paths: list[str], query: str = "") -> str:
    """Load compact summaries for specific source files (only when explicitly requested)."""
    parts = []
    used  = 0
    for fp in file_paths[:3]:
        fpath = ROOT / fp
        if not fpath.exists():
            continue
        summary = summarize_source_file(fpath, query=query)
        tokens  = estimate_tokens(summary)
        if used + tokens > BUDGET_RAG:
            summary = summary[:BUDGET_RAG * 4] + "\n... [truncated]"
            tokens  = estimate_tokens(summary)
        parts.append(f"### {fp}\n{summary}")
        used += tokens
        if used >= BUDGET_RAG:
            break
    return "\n\n".join(parts)


def build(query: str, specific_files: list[str] = None,
          use_rag: bool = True, use_knowledge: bool = True) -> tuple[str, dict]:
    """
    Assemble minimum context for query.
    Returns (context_string, token_breakdown_dict)
    """
    sections = []
    tokens   = {}

    # 1. Primer (always)
    primer = load_primer()
    if primer:
        sections.append(primer[:BUDGET_PRIMER * 4])
        tokens["primer"] = min(estimate_tokens(primer), BUDGET_PRIMER)

    # 2. Session state (always)
    state = load_session_state()
    if state:
        sections.append(state)
        tokens["session_state"] = estimate_tokens(state)

    # 3. Domain knowledge (selective)
    if use_knowledge:
        knowledge = load_knowledge(query)
        if knowledge:
            knowledge = truncate_to_budget(knowledge, BUDGET_KNOWLEDGE // 2)
            sections.append(f"[DOMAIN KNOWLEDGE — relevant to query]\n{knowledge}")
            tokens["domain_knowledge"] = estimate_tokens(knowledge)

    # 4. RAG source chunks OR specific files
    if specific_files:
        code = load_specific_files(specific_files, query=query)
        if code:
            code = truncate_to_budget(code, BUDGET_RAG // 2)
            sections.append(f"[SOURCE FILES]\n{code}")
            tokens["specific_files"] = estimate_tokens(code)
    elif use_rag:
        rag = load_rag(query)
        if rag:
            rag = truncate_to_budget(rag, BUDGET_RAG // 2)
            sections.append(f"[RELEVANT SOURCE — retrieved by query]\n{rag}")
            tokens["rag_chunks"] = estimate_tokens(rag)

    # 5. Task
    sections.append(
        f"[YOUR TASK]\n{query}\n\n"
        f"[RULES]\n"
        f"Use OUTPUT ROUTING PROTOCOL — tag gaps/issues/tasks with XML tags.\n"
        f"Only read additional source files if absolutely necessary.\n"
        f"Use compact summaries from context_builder.py instead of raw file dumps.\n"
        f"Use domain knowledge above for any quant/crypto/risk reasoning."
    )
    tokens["task"] = estimate_tokens(query)

    context = "\n\n" + ("─" * 60) + "\n\n".join(sections)
    tokens["TOTAL"] = sum(v for k, v in tokens.items() if k != "TOTAL")
    if tokens["TOTAL"] > BUDGET_TOTAL:
        context = truncate_to_budget(context, BUDGET_TOTAL)
        tokens["TOTAL"] = estimate_tokens(context)

    return context, tokens


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("query",          help="Your task or question")
    p.add_argument("--files", "-f",  nargs="*", help="Specific files to include")
    p.add_argument("--no-rag",       action="store_true")
    p.add_argument("--no-knowledge", action="store_true")
    p.add_argument("--tokens-only",  action="store_true", help="Show token breakdown only")
    p.add_argument("--clipboard",    action="store_true", help="Copy to clipboard")
    args = p.parse_args()

    context, tokens = build(
        args.query,
        specific_files=args.files,
        use_rag=not args.no_rag,
        use_knowledge=not args.no_knowledge,
    )

    if args.tokens_only:
        print("Token breakdown:")
        for k, v in tokens.items():
            print(f"  {k:<20} {v:>6} tokens")
        return

    if args.clipboard:
        import subprocess
        for cmd in [["xclip", "-selection", "clipboard"], ["wl-copy"]]:
            try:
                subprocess.run(cmd, input=context, text=True, capture_output=True)
                print(f"✓ Context copied ({tokens['TOTAL']} tokens)")
                return
            except FileNotFoundError:
                continue

    print(context)
    print(f"\n# Token breakdown: {tokens}")


if __name__ == "__main__":
    main()
