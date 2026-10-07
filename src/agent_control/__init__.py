"""
Agent execution control -- durable task state for Claude Code sessions.

A session's narrative is not evidence. This package records what a task
requested, what was implemented, what was verified, what was committed and
what was delivered as data on disk, so that:

* a fresh process -- after compaction, interruption or a new session --
  reconstructs the same state from disk rather than from memory;
* "complete" is a conclusion the completion validator draws from Git and
  recorded evidence, never a flag someone set;
* guarded Git mutations happen only after an explicit, recorded
  authorization and a pre-mutation checkpoint;
* every branch audit is qualified by the SHA it audited, so a branch that
  moves afterwards is reported STALE instead of silently "done".

Modules:

* ``model``       -- task, evidence, checkpoint and audit records; transition tables
* ``store``       -- the on-disk manifest store (git common dir, atomic, locked)
* ``gitstate``    -- read-only Git state collection and parsing
* ``git_safety``  -- guarded Git operation detection and authorization
* ``branch_audit`` -- branch/worktree inventory, ledger and reconciliation
* ``completion``  -- the completion predicate engine
* ``hooks``       -- Stop / PreCompact / SessionStart / PostToolUse entry points
* ``cli``         -- ``scripts/agent_control.py``

Layering (GOV-018): foundation. Imports nothing from ``src`` and only
``common.command_schema`` (redaction) from outside it.

Registry: GOV-060 .. GOV-065 (config/quality_registry.json).
"""
