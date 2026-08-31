"""Generation via the shared Claude Code CLI wrapper (Pro/Max subscription
auth -- see common/claude_cli.py for the auth/--bare rationale)."""
from common.claude_cli import run_claude
from .config import GENERATION_MODEL


def generate(query: str, context_docs: list[dict]) -> dict:
    context = "\n\n".join(
        f"[Source: {d.get('source', 'unknown')}]\n{d['text']}" for d in context_docs
    )
    prompt = (
        "Answer the question using ONLY the context below. "
        "Cite sources by filename.\n\n"
        f"CONTEXT:\n{context}\n\nQUESTION: {query}"
    )
    out = run_claude(prompt, model=GENERATION_MODEL, max_turns=1)
    return {"answer": out["result"], "sources": context_docs}
