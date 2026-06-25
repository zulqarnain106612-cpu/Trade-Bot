#!/usr/bin/env python3
"""
RAG Engine — BM25 on SQLite, zero external dependencies
=========================================================
Indexes every source chunk once. On query, retrieves only
the top-K most relevant chunks. Agents load 3-5 chunks
instead of entire files.

Token savings: ~95% vs reading full source files.

Index lives at: .project-intel/rag.db
Rebuild:        python3 rag_engine.py --index /path/to/project
Query:          python3 rag_engine.py --query "kelly sizing" --top 5
"""

import ast
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


# ── Config ────────────────────────────────────────────────────────────────────
CHUNK_SIZE      = 60    # lines per chunk
CHUNK_OVERLAP   = 10    # overlap between chunks
TOP_K_DEFAULT   = 5
MAX_CHUNK_TOKENS= 400   # ~300 words per chunk shown to agent
BM25_K1         = 1.5
BM25_B          = 0.75

SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".project-intel",
    "models", "artifacts", "htmlcov"
}
INDEX_EXTENSIONS = {".py", ".md", ".toml", ".yaml", ".yml"}


# ── Tokenizer (no deps) ───────────────────────────────────────────────────────
def tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens, strip noise."""
    text = text.lower()
    # Expand camelCase and snake_case
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'_', ' ', text)
    tokens = re.findall(r'\b[a-z][a-z0-9]{1,}\b', text)
    # Remove very common stop words
    stops = {
        'the','a','an','in','is','it','of','to','and','or','for',
        'with','as','at','by','from','be','are','was','were','has',
        'have','had','not','but','if','on','this','that','self','true',
        'false','none','def','class','import','return','pass','else',
        'elif','try','except','finally','raise','yield','async','await',
    }
    return [t for t in tokens if t not in stops and len(t) > 2]

def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 chars."""
    return len(text) // 4


# ── Chunker ───────────────────────────────────────────────────────────────────
def chunk_python_file(path: Path, rel: str) -> list[dict]:
    """
    Smart chunking: split at class/function boundaries when possible,
    fall back to line-based chunks.
    """
    try:
        source = path.read_text(errors="ignore")
        lines  = source.splitlines()
    except Exception:
        return []

    chunks = []

    # Try AST-based chunking first
    try:
        tree = ast.parse(source)
        nodes = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.col_offset == 0  # top-level only
        ]
        nodes.sort(key=lambda n: n.lineno)

        for i, node in enumerate(nodes):
            start = node.lineno - 1
            end   = nodes[i+1].lineno - 2 if i+1 < len(nodes) else len(lines)
            chunk_lines = lines[start:end]
            if not chunk_lines:
                continue

            # Build content: signature + docstring + body preview
            sig_end = min(start + 20, end)
            content = "\n".join(lines[start:sig_end])
            if end - sig_end > 0:
                content += f"\n    ... ({end - sig_end} more lines)"

            chunks.append({
                "file":    rel,
                "type":    type(node).__name__,
                "name":    node.name,
                "start":   start + 1,
                "end":     end + 1,
                "content": content[:MAX_CHUNK_TOKENS * 4],
                "tokens":  estimate_tokens(content),
            })
    except SyntaxError:
        pass

    # Fall back to line-based if no AST chunks
    if not chunks:
        for i in range(0, len(lines), CHUNK_SIZE - CHUNK_OVERLAP):
            block = lines[i:i + CHUNK_SIZE]
            content = "\n".join(block)
            chunks.append({
                "file":    rel,
                "type":    "block",
                "name":    f"lines_{i+1}_{i+len(block)}",
                "start":   i + 1,
                "end":     i + len(block) + 1,
                "content": content[:MAX_CHUNK_TOKENS * 4],
                "tokens":  estimate_tokens(content),
            })

    return chunks


def chunk_text_file(path: Path, rel: str) -> list[dict]:
    """Chunk markdown/config files by section."""
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return []

    chunks = []
    # Split markdown by ## headers
    sections = re.split(r'\n(?=#{1,3} )', text)
    for section in sections:
        if len(section.strip()) < 50:
            continue
        title_match = re.match(r'#{1,3} (.+)', section)
        name = title_match.group(1)[:50] if title_match else rel
        chunks.append({
            "file":    rel,
            "type":    "section",
            "name":    name,
            "start":   0,
            "end":     0,
            "content": section[:MAX_CHUNK_TOKENS * 4],
            "tokens":  estimate_tokens(section),
        })
    if not chunks:
        chunks.append({
            "file": rel, "type": "file", "name": rel,
            "start": 0, "end": 0,
            "content": text[:MAX_CHUNK_TOKENS * 4],
            "tokens": estimate_tokens(text),
        })
    return chunks


# ── BM25 Index ────────────────────────────────────────────────────────────────
class BM25Index:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(str(db_path))
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id      INTEGER PRIMARY KEY,
                file    TEXT,
                type    TEXT,
                name    TEXT,
                start   INTEGER,
                end     INTEGER,
                content TEXT,
                tokens  INTEGER,
                hash    TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS terms (
                chunk_id INTEGER,
                term     TEXT,
                freq     INTEGER
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_terms ON terms(term);
            CREATE INDEX IF NOT EXISTS idx_chunk ON terms(chunk_id);
        """)
        self.db.commit()

    def clear(self):
        self.db.executescript("DELETE FROM chunks; DELETE FROM terms; DELETE FROM meta;")
        self.db.commit()

    def add_chunks(self, chunks: list[dict]):
        for chunk in chunks:
            content_hash = hashlib.md5(chunk["content"].encode()).hexdigest()
            try:
                cur = self.db.execute(
                    "INSERT INTO chunks (file,type,name,start,end,content,tokens,hash) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (chunk["file"], chunk["type"], chunk["name"],
                     chunk["start"], chunk["end"], chunk["content"],
                     chunk["tokens"], content_hash)
                )
                chunk_id = cur.lastrowid
                terms = Counter(tokenize(chunk["content"]))
                self.db.executemany(
                    "INSERT INTO terms (chunk_id,term,freq) VALUES (?,?,?)",
                    [(chunk_id, t, f) for t, f in terms.items()]
                )
            except sqlite3.IntegrityError:
                pass  # Already indexed (same hash)
        self.db.commit()

    def finalize(self):
        """Compute and store IDF values."""
        n_docs = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        avg_len = self.db.execute(
            "SELECT AVG(tokens) FROM chunks"
        ).fetchone()[0] or 100

        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('n_docs', ?)", (str(n_docs),)
        )
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('avg_len', ?)", (str(avg_len),)
        )
        self.db.commit()

    def query(self, query_text: str, top_k: int = TOP_K_DEFAULT,
              file_filter: str | None = None) -> list[dict]:
        """BM25 retrieval."""
        terms = tokenize(query_text)
        if not terms:
            return []

        meta = dict(self.db.execute("SELECT key,value FROM meta").fetchall())
        n_docs  = int(meta.get("n_docs", 1))
        avg_len = float(meta.get("avg_len", 100))

        scores: dict[int, float] = defaultdict(float)

        for term in set(terms):
            # IDF
            df = self.db.execute(
                "SELECT COUNT(DISTINCT chunk_id) FROM terms WHERE term=?", (term,)
            ).fetchone()[0]
            if df == 0:
                continue
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)

            # TF per chunk
            rows = self.db.execute(
                "SELECT chunk_id, freq FROM terms WHERE term=?", (term,)
            ).fetchall()
            for chunk_id, tf in rows:
                chunk_len = self.db.execute(
                    "SELECT tokens FROM chunks WHERE id=?", (chunk_id,)
                ).fetchone()[0] or avg_len
                norm = (tf * (BM25_K1 + 1)) / (
                    tf + BM25_K1 * (1 - BM25_B + BM25_B * chunk_len / avg_len)
                )
                scores[chunk_id] += idf * norm

        if not scores:
            return []

        top_ids = sorted(scores, key=scores.__getitem__, reverse=True)[:top_k]

        results = []
        for cid in top_ids:
            row = self.db.execute(
                "SELECT file,type,name,start,end,content,tokens FROM chunks WHERE id=?",
                (cid,)
            ).fetchone()
            if row:
                results.append({
                    "file": row[0], "type": row[1], "name": row[2],
                    "start": row[3], "end": row[4],
                    "content": row[5], "tokens": row[6],
                    "score": round(scores[cid], 3),
                })
        return results


# ── Indexer ───────────────────────────────────────────────────────────────────
def build_index(project_root: Path):
    db_path   = project_root / ".project-intel" / "rag.db"
    index     = BM25Index(db_path)
    index.clear()

    total_chunks = 0
    total_tokens = 0

    for fpath in sorted(project_root.rglob("*")):
        if fpath.is_dir():
            continue
        if any(skip in fpath.parts for skip in SKIP_DIRS):
            continue
        if fpath.suffix not in INDEX_EXTENSIONS:
            continue
        if fpath.stat().st_size > 200 * 1024:
            continue

        rel = str(fpath.relative_to(project_root))

        if fpath.suffix == ".py":
            chunks = chunk_python_file(fpath, rel)
        else:
            chunks = chunk_text_file(fpath, rel)

        if chunks:
            index.add_chunks(chunks)
            total_chunks += len(chunks)
            total_tokens += sum(c["tokens"] for c in chunks)

    index.finalize()
    print(f"✓ Indexed {total_chunks} chunks | ~{total_tokens:,} tokens total")
    print(f"  Index: {db_path} ({db_path.stat().st_size // 1024} KB)")
    return index


# ── Query formatter ───────────────────────────────────────────────────────────
def format_results(results: list[dict], query: str) -> str:
    """Format RAG results for agent consumption."""
    if not results:
        return f"No relevant chunks found for: {query}"

    total_tokens = sum(r["tokens"] for r in results)
    lines = [
        f"# RAG Results for: '{query}'",
        f"# {len(results)} chunks | ~{total_tokens} tokens\n",
    ]
    for i, r in enumerate(results, 1):
        lines.append(
            f"## [{i}] {r['file']} — {r['name']} "
            f"(lines {r['start']}-{r['end']}, score={r['score']})"
        )
        lines.append(r["content"])
        lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--index",  help="Project root to index")
    p.add_argument("--query",  help="Query string")
    p.add_argument("--top",    type=int, default=TOP_K_DEFAULT)
    p.add_argument("--project",help="Project root for query (default: cwd)")
    p.add_argument("--json",   action="store_true", help="Output JSON")
    args = p.parse_args()

    if args.index:
        build_index(Path(args.index))
        return

    if args.query:
        root    = Path(args.project or ".").resolve()
        db_path = root / ".project-intel" / "rag.db"
        if not db_path.exists():
            print(f"No index found at {db_path}. Run --index first.")
            sys.exit(1)
        idx     = BM25Index(db_path)
        results = idx.query(args.query, top_k=args.top)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(format_results(results, args.query))
        return

    p.print_help()

if __name__ == "__main__":
    main()
