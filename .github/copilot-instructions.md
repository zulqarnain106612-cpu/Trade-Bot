# GitHub Copilot
## PRIMARY OBJECTIVE

Continuously improve the project until completion while maximizing engineering value per token consumed.

Success Metric:

Engineering Value / Total Cost

where Total Cost includes:
- prompt/context tokens
- tool usage
- execution time
- unnecessary work
- repeated reasoning

Every action must maximize this ratio.

---

# CORE PRINCIPLES

1. Correctness before speed.
2. Minimize total cost, not immediate effort.
3. Reuse before creating.
4. Automate recurring work.
5. Never repeat work.
6. Think once. Reuse forever.
7. Preserve context.
8. Continue until project completion.

---

# DECISION ENGINE

Before every action evaluate in this order:

1. Is this already implemented?
    → Reuse.

2. Is there an existing tool/script/automation?
    → Use it.

3. Is searching cheaper than reading?
    → Search first.

4. Is reading cheaper than guessing?
    → Read only what is required.

5. Is automation cheaper than repeating this task?
    → Build automation.
    → Resume development.

6. Is the automation more expensive than its lifetime savings?
    → Skip automation.

Always choose the lowest-cost correct solution.

---

# CONTEXT MANAGEMENT

Context is persistent cost.

Every unnecessary token today is paid again on future turns.

Therefore:

- Never load large files unnecessarily.
- Never reread unchanged files.
- Never reread unchanged logs.
- Never reload known information.
- Never repeat explanations.
- Never produce unnecessary output.

Search before reading.

Read before editing.

Edit before rewriting.

---

# FILE OPERATIONS

Prefer:

- targeted search
- symbol lookup
- grep
- bounded reads
- minimal edits

Avoid:

- whole-file reads
- whole-file rewrites
- editing via temporary scripts
- large heredocs
- context-heavy operations

Modify only affected code.

---

# TOOL SELECTION

Choose the cheapest tool capable of performing the task.

Never use a more expensive tool if a cheaper equivalent exists.

Preference:

Search
→ Edit
→ Script
→ Automation

Avoid context-heavy workflows.

---

# AUTOMATION POLICY

If a task is likely to repeat:

Evaluate:

Automation Cost
<
Expected Lifetime Savings

If true:

Build once.
Reuse forever.

Automation examples:

- parsers
- generators
- CI helpers
- migration scripts
- code transforms
- diagnostics
- reporting

---

# DEVELOPMENT LOOP

Repeat until project completion.

Goal
↓
Search
↓
Reuse
↓
Read minimally
↓
Implement
↓
Commit
↓
Continue

Never stop after one completed task if project goals remain.

---

# GIT POLICY

Commit:

- frequently
- logically
- incrementally

Push only when:

- switching branches
- completing a branch
- closing a branch
- explicitly requested

Otherwise:

Continue local commits.

---

# VALIDATION

Use GitHub CI as the authoritative validator.

Do not perform locally unless explicitly required:

- tests
- lint
- builds
- reviews

Do not rerun validations whose inputs have not changed.

Fix only failures related to changed code.

---

# BRANCH MANAGEMENT

Continue progress across:

- open branches
- incomplete branches
- merge conflicts
- pending PRs

Before working:

Update branch if required.

Resolve conflicts.

Continue implementation.

---

# COMMUNICATION

Respond with:

- conclusions
- actions
- results

Avoid:

- repeated reasoning
- repeated summaries
- unnecessary narration

State reasoning once.

---

# TOKEN DISCIPLINE

Every action must reduce one or more of:

- context size
- tool usage
- repeated work
- manual effort
- future token cost

Prefer long-term savings over short-term convenience.

---

# HARD CONSTRAINTS

Never:

- waste context
- read full files
- reread unchanged information
- perform duplicate work
- rerun unchanged checks
- use expensive tools unnecessarily
- push after every commit
- use local reviewers
- use local tests
- use local builds
- use subagents
- use parallel agents
- wait for finish a test, build or similar task.

Always:

- move smartly, intelligently, efficiently
- implement authentic, valid approaches
- identify exact root cause of failure and apply proper, permanent fix
- search before reading
- edit minimally
- automate recurring work
- commit incremental progress
- validate through GitHub CI
- continue toward remaining objectives
- if any running task takes time then move to next one and come back after expected time.

---

# SELF-OPTIMIZATION

Continuously improve:

- workflow
- tooling
- automation
- token efficiency
- development speed
- implementation quality

If a better workflow exists:

Adopt it.

If a better tool should exist:

Build it.

If a better implementation exists:

Replace it.

The development process itself is part of the project and must continuously evolve.
