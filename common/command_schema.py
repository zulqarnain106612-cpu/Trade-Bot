"""
COMMAND_EXEC_SCHEMA — JSON Schema for structured, output-capped shell command execution.

Extends the project's PTC pattern to terminal commands. Aligns with the same
json_schema pattern used by EXTRACTION_SCHEMA and RESOLUTION_SCHEMA in kg/.

Works identically in:
  - Local terminal sessions
  - Cloud containers (CLAUDE_CODE_REMOTE=true, provisioned by session-start.sh)

Companion runtime: common/shell_exec.py
Skill:            .claude/skills/command-execution/SKILL.md
"""

from __future__ import annotations

COMMAND_EXEC_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
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
                "error": {"type": ["string", "null"]},
            },
        },
    },
}
