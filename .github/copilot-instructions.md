# GitHub Copilot — Trade Bot

## AUTOMATIC SESSION INIT — run this before processing ANY message, every time
Do not greet. Do not ask questions. Do not read any file. First action on every session:
```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```
Read its output. That IS your complete context. Then immediately act on NEXT TASK shown.
If already run this session, skip and continue work.

## Checkpoint / Finish
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent copilot \
  --completed "what you did" --next "exact next step" --files "src/file.py"

python3 .project-intel/scripts/handoff.py finish --agent copilot \
  --completed "summary" --next "TASK-XXX: next task"

bash scripts/claude-commit.sh --msg "type(scope): description [copilot]"
```

## Output routing (every response, no exceptions)
`<gap>` `<issue>` `<broken>` `<missing>` `<decision>` `<task>` `<risk>` `<security>` `<debt>` → project files (never repeat in chat)
`<chat>` or untagged → chat only

## Never
- go for (simple, partial, minimal, demo, assumpted, incomplete) approaches
- Read source files to understand the project — resume.py output is orientation
- Use .project-intel/scripts/smart_read.py <path> or .project-intel/scripts/context_builder.py --files <path> when file-specific context is genuinely required
- Read: MODULE_MAP.json (291KB), ARCHITECTURE.md (160KB), RAW_SCAN.json (87KB), rag.db (8.9MB), package-lock.json (230KB), requirements.lock (173KB), GAPS.md (34KB), SESSION_STATE.json (11KB)
- Push to git — commit only
- Bypass CognitiveEngine validators
- Use print() in src/ — use structlog
- Hardcode secrets or credentials
- Write gaps/issues as plain chat — use XML tags
