"""
COMMAND_EXEC_SCHEMA — JSON Schema for structured, output-capped shell command execution.

Extends the project's PTC pattern to terminal commands. Aligns with the same
json_schema pattern used by EXTRACTION_SCHEMA and RESOLUTION_SCHEMA in kg/.

don't Work identically in:
  - Local terminal sessions, always use github cloud action Workflows for review, test, build
  - Cloud containers (CLAUDE_CODE_REMOTE=false, provisioned by session-start.sh)

Companion runtime: common/shell_exec.py
Skill:            .claude/skills/command-execution/SKILL.md
"""

from __future__ import annotations

import re

SCHEMA_VERSION = "1.1.0"

# Environment names always kept when env.inherit is False, so that a command
# with a scrubbed environment can still resolve binaries and behave
# deterministically instead of failing in a way that looks like a bug.
ENV_MINIMAL_BASE: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")

# Patterns whose match means the command is irreversible or hard to reverse.
# Matched against the raw command string, case-insensitively. This list is
# deliberately conservative: a false positive costs one explicit
# confirm_destructive=True, a false negative costs data.
DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]",  # rm -rf / rm -f / rm -r -f
    r"\brmdir\b",
    r"\bshred\b",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\b[^|]*\bof=",
    r"\btruncate\b",
    r">\s*/dev/(sd|nvme|vd|hd)",
    r"\bgit\s+push\b[^|]*--force(?!-with-lease)",
    r"\bgit\s+push\b[^|]*\s-f\b",
    r"\bgit\s+reset\b[^|]*--hard",
    r"\bgit\s+clean\b[^|]*-[a-zA-Z]*[dfx]",
    r"\bgit\s+branch\b[^|]*\s-D\b",
    r"\bDROP\s+(TABLE|DATABASE|COLLECTION|INDEX|SCHEMA)\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bDELETE\s+FROM\b",
    r"\bdrop(Database|Collection|Index)\s*\(",
    r"\bdeleteMany\s*\(",
    r"\bremove\s*\(\s*\{\s*\}\s*\)",
    r"\bkubectl\s+delete\b",
    r"\bterraform\s+destroy\b",
    r"\bdocker\s+(rm|rmi|volume\s+rm|system\s+prune)\b",
    r"\bnpm\s+unpublish\b",
    r"\baws\s+s3\s+rm\b",
    r"\baws\s+s3\s+rb\b",
)

# Patterns whose match means the command changes state but reversibly.
MUTATING_PATTERNS: tuple[str, ...] = (
    r"\bgit\s+(commit|push|merge|rebase|checkout|switch|apply|stash|tag|am)\b",
    r"\b(pip|pip3|uv|poetry|npm|pnpm|yarn|cargo|go)\s+(install|add|remove|sync|get)\b",
    r"\bmk(dir|temp)\b",
    r"\b(cp|mv|ln|touch|chmod|chown|install)\b",
    r"\b(tee|sed)\b[^|]*\s-i\b",
    r"\btee\b",
    r">>?\s*[^&|\s]",
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|CREATE\s+(TABLE|INDEX|DATABASE))\b",
    r"\b(insertOne|insertMany|updateOne|updateMany|replaceOne|createIndex)\s*\(",
    r"\bdocker\s+(run|build|compose\s+up)\b",
    r"\bkubectl\s+(apply|create|patch|scale)\b",
)

# Secrets that must never reach an LLM context. Each pattern captures the
# whole secret-bearing token; _redact() replaces the match with a marker that
# preserves shape (so a reader can tell "a token was here") without value.
REDACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(gh[pousr]_[A-Za-z0-9]{16,})", "GITHUB_TOKEN"),
    (r"\b(github_pat_[A-Za-z0-9_]{20,})", "GITHUB_PAT"),
    (r"\b(AKIA[0-9A-Z]{16})", "AWS_ACCESS_KEY_ID"),
    (r"\b(ASIA[0-9A-Z]{16})", "AWS_STS_KEY_ID"),
    (r"\b(sk-[A-Za-z0-9_\-]{20,})", "OPENAI_KEY"),
    (r"\b(sk-ant-[A-Za-z0-9_\-]{20,})", "ANTHROPIC_KEY"),
    (r"\b(xox[abprs]-[A-Za-z0-9\-]{10,})", "SLACK_TOKEN"),
    (r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})", "JWT"),
    (r"(mongodb(?:\+srv)?://[^:@/\s]+:)[^@/\s]+(@)", "MONGO_PASSWORD"),
    (r"(https?://[^:@/\s]+:)[^@/\s]+(@)", "URL_PASSWORD"),
    (
        r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)",
        "PRIVATE_KEY",
    ),
    (
        r"((?i:\b[A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY)[A-Za-z0-9_]*)\s*[=:]\s*)(\S*)(\s|$)",
        "ENV_SECRET",
    ),
    (r"\b(xprv[A-Za-z0-9]{50,})", "BIP32_XPRV"),
    (r"\b([5KL][1-9A-HJ-NP-Za-km-z]{50,51})\b", "BTC_WIF_KEY"),
)


COMMAND_EXEC_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://trade-bot.local/schemas/command-execution/1.1.0.json",
    "title": "CommandExecution",
    "description": (
        "Structured declaration of a shell command. "
        "Enforces output caps and retry policy before result enters context. "
        "filter_mode=jq requires jq on PATH; use regex/fields instead on cloud containers."
    ),
    "type": "object",
    "required": ["command", "output_policy"],
    "additionalProperties": False,
    "properties": {
        "command": {
            "type": "string",
            "minLength": 1,
            "description": "Exact shell command string to execute.",
        },
        "schema_version": {
            "type": "string",
            "const": SCHEMA_VERSION,
            "default": SCHEMA_VERSION,
            "description": (
                "Version of COMMAND_EXEC_SCHEMA this declaration targets. "
                "Omitted declarations are treated as SCHEMA_VERSION. A future "
                "major bump lets shell_exec.run() reject stale declarations "
                "instead of silently misreading them."
            ),
        },
        "purpose": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": (
                "Why this command is being run, in one line. Audit trail: it "
                "is logged with the command hash so a later reader can tell "
                "intent from effect without re-deriving it from the shell."
            ),
        },
        "classification": {
            "type": "string",
            "enum": ["read_only", "mutating", "destructive"],
            "default": "read_only",
            "description": (
                "Effect class. read_only: no filesystem/network/state change. "
                "mutating: reversible change (writes a file, installs a dep). "
                "destructive: irreversible or hard to reverse (rm, DROP, "
                "DELETE, force-push, dd, mkfs, truncate). destructive is never "
                "retried and requires confirm_destructive=True. If the declared "
                "value is weaker than what classify() detects in the command "
                "string, shell_exec.run() refuses to execute -- misdeclaring "
                "is a hard error, not a warning."
            ),
        },
        "confirm_destructive": {
            "type": "boolean",
            "default": False,
            "description": (
                "Explicit authorization for classification='destructive'. "
                "Must be set by a human-authorized caller; shell_exec.run() "
                "refuses destructive commands without it."
            ),
        },
        "timeout_s": {
            "type": "integer",
            "minimum": 1,
            "maximum": 900,
            "default": 120,
            "description": (
                "Per-attempt wall-clock timeout. On expiry the whole process "
                "group is SIGKILLed. Declared here (not only as a run() kwarg) "
                "so a declaration is self-contained and reproducible."
            ),
        },
        "cwd": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Working directory for the command. Must exist and be a "
                "directory at execution time, else the declaration is "
                "rejected before anything runs. Defaults to the caller's cwd."
            ),
        },
        "env": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "Deterministic child environment. Without this the child "
                "inherits every secret in os.environ; with it, only the "
                "allowlist survives. Reproducibility and secret containment."
            ),
            "properties": {
                "inherit": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "True: start from os.environ. False: start from an "
                        "empty environment plus ENV_MINIMAL_BASE (PATH, HOME, "
                        "LANG, TZ) so the command can still find binaries."
                    ),
                },
                "allowlist": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 64,
                    "description": (
                        "When present, only these names are passed through "
                        "from the inherited environment. Everything else is "
                        "dropped, secrets included."
                    ),
                },
                "overrides": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "maxProperties": 64,
                    "description": (
                        "Explicit name->value pairs applied last. Never put a "
                        "secret literal here: the declaration is logged."
                    ),
                },
            },
        },
        "output_policy": {
            "type": "object",
            "required": ["max_lines"],
            "additionalProperties": False,
            "properties": {
                "max_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                    "description": (
                        "Hard cap on lines returned. Excess is dropped; "
                        "truncated=True is set in result. Never set > 100 "
                        "without justification -- tighten filter_expr instead."
                    ),
                },
                "stream": {
                    "type": "string",
                    "enum": ["stdout", "stderr", "both"],
                    "default": "stdout",
                    "description": "Which stream(s) to capture.",
                },
                "filter_mode": {
                    "type": "string",
                    "enum": ["none", "head", "tail", "grep", "regex", "jq", "fields"],
                    "default": "none",
                    "description": (
                        "Reduction applied BEFORE max_lines cap:\n"
                        "  none   — raw lines fed to cap\n"
                        "  head   — first N lines  (filter_expr = N as string)\n"
                        "  tail   — last N lines   (filter_expr = N as string)\n"
                        "  grep   — keep lines containing filter_expr (literal)\n"
                        "  regex  — keep lines matching filter_expr (re pattern)\n"
                        "  jq     — jq query (requires jq binary; avoid on cloud)\n"
                        "  fields — JSON-per-line: keep only listed keys (comma-sep)"
                    ),
                },
                "filter_expr": {
                    "type": "string",
                    "default": "",
                    "description": "Expression for filter_mode. Ignored when filter_mode=none.",
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 262144,
                    "default": 65536,
                    "description": (
                        "Hard cap on bytes returned, applied after max_lines. "
                        "max_lines alone is not a bound: a single minified "
                        "bundle, a base64 blob or a no-newline log line can be "
                        "megabytes and still be one line. Excess is cut and "
                        "result.bytes_truncated is set."
                    ),
                },
                "redact": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": (
                        "Secret masking applied to filtered output before it "
                        "is returned. On by default: command output routinely "
                        "contains tokens (git remotes with PATs, AWS keys, "
                        "connection strings) and this module's whole purpose "
                        "is that its return value enters an LLM context."
                    ),
                    "properties": {
                        "enabled": {"type": "boolean", "default": True},
                        "extra_patterns": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "maxItems": 32,
                            "description": (
                                "Additional Python regexes to mask, on top of "
                                "REDACTION_PATTERNS. Each must compile; an "
                                "invalid regex rejects the declaration."
                            ),
                        },
                    },
                },
                "on_empty": {
                    "type": "string",
                    "enum": ["ok", "error"],
                    "default": "ok",
                    "description": "Treat empty filtered output as error (triggers retry if configured).",
                },
            },
        },
        "retry_policy": {
            "type": "object",
            "additionalProperties": False,
            "description": "Optional bounded retry. Omit entirely if no retry needed.",
            "properties": {
                "max_attempts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 1,
                },
                "retry_on": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["nonzero_exit", "empty_output", "pattern_absent"],
                    },
                    "minItems": 1,
                    "description": "Conditions that trigger a retry attempt.",
                },
                "pattern_absent": {
                    "type": "string",
                    "description": (
                        "Required when retry_on includes 'pattern_absent'. "
                        "Retry if this regex is not found in filtered output."
                    ),
                },
                "delay_s": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 30,
                    "default": 1,
                    "description": "Seconds to wait between attempts.",
                },
            },
        },
        # result is populated by shell_exec.run() — never set by caller.
        # Declared here so callers can validate response shape.
        "result": {
            "type": "object",
            "readOnly": True,
            "additionalProperties": False,
            "properties": {
                "exit_code": {"type": "integer"},
                "filtered_output": {"type": "string"},
                "truncated": {"type": "boolean"},
                "attempt_count": {"type": "integer"},
                "bytes_truncated": {"type": "boolean"},
                "timed_out": {"type": "boolean"},
                "duration_s": {"type": "number"},
                "classification": {"type": "string"},
                "redactions_applied": {"type": "integer"},
                "command_sha256": {"type": "string"},
                "started_at": {"type": "string"},
                "error": {"type": ["string", "null"]},
            },
        },
    },
}


# --------------------------------------------------------------------------
# Policy helpers. Pure functions over the vocabulary above; no I/O, no
# subprocess. shell_exec.run() is the only intended caller, but they are
# importable so that tests and the PreToolUse hook classify identically --
# one implementation, not three that drift.
# --------------------------------------------------------------------------

_DESTRUCTIVE_RE = [re.compile(p, re.IGNORECASE) for p in DESTRUCTIVE_PATTERNS]
_MUTATING_RE = [re.compile(p, re.IGNORECASE) for p in MUTATING_PATTERNS]
_REDACT_RE = [(re.compile(p), name) for p, name in REDACTION_PATTERNS]

_RANK: dict[str, int] = {"read_only": 0, "mutating": 1, "destructive": 2}


def classify(command: str) -> str:
    """
    Detect the effect class of a raw command string.

    Returns "destructive", "mutating" or "read_only". Detection is textual and
    therefore an under-approximation of what a shell can do: `bash evil.sh`
    looks read-only. It exists to catch the declared-vs-actual mismatch that
    a careless caller produces, not to sandbox a hostile one. Never treat a
    "read_only" verdict as proof a command is safe.
    """
    for rx in _DESTRUCTIVE_RE:
        if rx.search(command):
            return "destructive"
    for rx in _MUTATING_RE:
        if rx.search(command):
            return "mutating"
    return "read_only"


def rank(classification: str) -> int:
    """Order effect classes so 'is the declaration weaker than reality' is a <."""
    try:
        return _RANK[classification]
    except KeyError:  # pragma: no cover - schema enum prevents this
        raise ValueError(f"unknown classification: {classification!r}") from None


def redact(text: str, extra_patterns: list[str] | None = None) -> tuple[str, int]:
    """
    Mask secrets in command output.

    Returns (masked_text, replacement_count). Each match becomes
    "[REDACTED:<NAME>]", preserving the fact that a secret was present without
    its value. Patterns with two capture groups (URL credentials, PEM blocks)
    keep their delimiters so the surrounding structure stays readable.

    This is a defence in depth, not a guarantee: a secret in a format no
    pattern describes will pass through. The primary control is env.allowlist,
    which stops the secret reaching the child process at all.
    """
    count = 0
    patterns = list(_REDACT_RE)
    for raw in extra_patterns or []:
        patterns.append((re.compile(raw), "CUSTOM"))

    for rx, name in patterns:
        marker = f"[REDACTED:{name}]"

        def _sub(m: re.Match[str], _marker: str = marker) -> str:
            nonlocal count
            count += 1
            groups = m.groups()
            if len(groups) >= 2:
                return f"{groups[0]}{_marker}{groups[-1]}"
            return _marker

        text = rx.sub(_sub, text)
    return text, count


def build_env(spec: dict | None) -> dict[str, str] | None:
    """
    Materialise the child environment from an `env` declaration.

    Returns None when no spec is given, which makes shell_exec pass env=None to
    Popen and inherit the parent environment unchanged -- the pre-1.1.0
    behaviour, so existing declarations are unaffected.
    """
    import os

    if not spec:
        return None

    if spec.get("inherit", True):
        base = dict(os.environ)
    else:
        base = {k: os.environ[k] for k in ENV_MINIMAL_BASE if k in os.environ}

    allowlist = spec.get("allowlist")
    if allowlist is not None:
        keep = set(allowlist) | set(ENV_MINIMAL_BASE)
        base = {k: v for k, v in base.items() if k in keep}

    base.update(spec.get("overrides", {}))
    return base
