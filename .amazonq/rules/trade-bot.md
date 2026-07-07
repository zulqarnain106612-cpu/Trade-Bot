# Amazon Q — Session Protocol

## Session start (mandatory)
```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```
Read its output. That is your complete context. Begin work on NEXT TASK immediately.
'''

## Checkpoint after every meaningful step
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent amazonq \
  --completed "what you just did" --next "exact next action" --files "src/file.py"
```

## Finish
```bash
python3 .project-intel/scripts/handoff.py finish --agent amazonq \
  --completed "summary" --next "TASK-XXX: next task"
bash scripts/claude-commit.sh --msg "type(scope): description [amazonq]"
```

## Output routing (every response)
`<gap>` `<issue>` `<broken>` `<missing>` `<decision>` `<task>` `<risk>` `<security>` `<debt>` → project files
`<chat>` or untagged → chat only

## Never
- Read source files to understand the project — resume.py output is orientation
- Read MODULE_MAP.json, ARCHITECTURE.md, RAW_SCAN.json, rag.db, requirements.lock, package-lock.json
- Push to git — commit only
- Bypass CognitiveEngine validators
- Use print() in src/ — use structlog
