# Issues & Bugs
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known bugs before implementing

## Issue-001 [2026-06-23]
DIAGNOSTICS.md reported ruff and pyright not installed in CI environment.
Affects: CI linting gates may pass without actual lint checks.
Severity: Medium. Action: Verify ruff installed in CI venv (see pyproject.toml — ruff IS configured).
Status: NEEDS VERIFICATION
────────────────────────────────────────────────────────────
