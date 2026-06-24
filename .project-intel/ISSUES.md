# Issues & Bugs
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known bugs before implementing

## Issue-001 [2026-06-23]
DIAGNOSTICS.md reported ruff and pyright not installed in CI environment.
Affects: CI linting gates may pass without actual lint checks.
Severity: Medium. Action: Verify ruff installed in CI venv (see pyproject.toml — ruff IS configured).
Status: NEEDS VERIFICATION
────────────────────────────────────────────────────────────

## Issue-001 — RESOLVED (verified 2026-06-24)
DIAGNOSTICS.md claimed ruff/pyright not installed in CI venv.
Verified: ruff IS installed (.venv/bin/ruff 0.15.17, 254 findings across
the repo — see TECH_DEBT Debt-004). pyright, mypy, bandit, semgrep
genuinely NOT installed in .venv despite being referenced in
.pre-commit-config.yaml / pyproject.toml / README CI badge. bandit and
pip-audit were installed ad hoc this session to get real signal; mypy/
pyright/semgrep are still absent.
Status: PARTIALLY RESOLVED — ruff claim corrected, mypy/pyright/semgrep
gap is new and real. See ISSUES-002.
────────────────────────────────────────────────────────────

## Issue-002 [2026-06-24] — NEW
mypy, pyright, and semgrep are configured in pyproject.toml / 
.pre-commit-config.yaml / CI workflow but are NOT installed in the local
.venv. Type-checking and SAST configured-but-absent means a contributor
relying on "CI: mypy · bandit · semgrep" (per README and the architecture
SVG) gets no actual local signal before push: it relies entirely on CI
running with a fresh install, which has not been verified in this audit
(no CI run inspected, only local .venv state).
Severity: Medium. File: .venv, pyproject.toml, .pre-commit-config.yaml
Status: OPEN — Action: `pip install -r requirements-dev.txt` (verify it
includes mypy/pyright/semgrep) or document the gap.
────────────────────────────────────────────────────────────

## Issue-003 [2026-06-24] — NEW
Frontend dependency drift: package.json declares "recharts": "^3.8.1" and
package-lock.json agrees (^3.8.1), but the resolved/installed package in
node_modules is recharts@2.15.4. `npm ls recharts` reports it explicitly
as invalid ("recharts@2.15.4 invalid: \"^3.8.1\" from the root project").
App.jsx imports LineChart/Line/XAxis/YAxis/CartesianGrid/Tooltip/
ResponsiveContainer/ReferenceLine from recharts — v2→v3 has breaking API
changes; current behavior is running against an untested/mismatched major
version relative to what's declared.
Severity: Medium (works today on v2 API surface used, but any v3-specific
behavior assumed in code, or any future `npm install` reconciling to true
v3, can silently break the dashboard).
File: frontend/package.json, frontend/package-lock.json, frontend/node_modules
Status: OPEN — Action: `cd frontend && rm -rf node_modules && npm install`,
then visually verify the equity-curve/chart components render correctly
against the resolved v3 API (axis/tooltip prop names changed between
recharts v2 and v3).
────────────────────────────────────────────────────────────

## Issue-004 [2026-06-24] — NEW
src/diagnostics/runtime_monitor.py docstring claims "Attempt safe
auto-recovery (restart stalled tasks, flush caches)" and
_on_task_done() logs action="monitor_offline — restart required" on a
crashed monitored task — but no restart logic exists anywhere in the
file. Verified by reading every function: the only concrete remediation
action taken automatically is gc.collect() on the memory-critical probe.
Tick-stall and task-crash detection both only log + alert; no supervisor
restarts the stalled/crashed task or coroutine. A human reading the
docstring or the "restart required" log line could reasonably (and
incorrectly) assume the system self-heals from a crashed background task.
Severity: Medium (operational/documentation mismatch — could delay
incident response if an operator assumes auto-recovery is handling a
crashed orchestrator task when it is not).
File: src/diagnostics/runtime_monitor.py
Status: OPEN — Action: either implement actual task restart (e.g. via a
supervisor that recreates the asyncio.Task from its original coroutine
factory) or correct the docstring/log message to say "alert only — no
auto-restart" so operators don't have a false sense of self-healing.
────────────────────────────────────────────────────────────

## Issue-005 [2026-06-24] — NEW
frontend/src/App.jsx line 9 comment: "SSRF guard: only allow http/https
to explicit hostnames — reject file://, internal IPs, etc." — but the
regex (_ALLOWED_API_RE = /^https?:\/\/[a-zA-Z0-9._-]+(:\d+)?$/) does NOT
actually exclude internal/private IPs or the cloud metadata address
(169.254.169.254) — all are valid matches for [a-zA-Z0-9._-]+. Verified
this is a BUILD-TIME check on import.meta.env.VITE_API_URL (set by
whoever runs `npm run build`), not a runtime check against user-supplied
input, so it's not exploitable as a classic user-triggered SSRF — its
real job is catching operator misconfiguration (malformed/non-http
schemes) at build time, which it does correctly. The comment overstates
what the regex defends against.
Severity: Low (comment/documentation accuracy only, not a real
vulnerability — no exploitable path found, since the value is
operator-controlled at build time, not attacker-controlled at runtime).
File: frontend/src/App.jsx line 9-12
Status: OPEN — Action: correct the comment to "build-time config
sanity check — rejects malformed/non-http(s) VITE_API_URL values" rather
than calling it an SSRF guard, to avoid a future auditor assuming SSRF is
mitigated here.

## Issue-002 [2026-06-24] — CORRECTED (2026-06-24, same session)
Original framing was imprecise. Corrected root cause, verified directly:
.pre-commit-config.yaml correctly declares ruff/mypy/bandit/trufflehog/
semgrep hooks (each pre-commit hook manages its own isolated tool install
via its repo's rev pin — NOT dependent on the project .venv, so "not
installed in .venv" was the wrong frame). The REAL gap: `pre-commit
install` was never run in this repo — .git/hooks/pre-commit does not
exist on disk. `pre-commit` itself IS available (/usr/bin/pre-commit,
system install), but with no git hook registered, NONE of the 5
configured checks (ruff, mypy, bandit, trufflehog, semgrep) have ever run
locally on any commit in this repo's history. They may still run in CI
(ci.yml/security.yml were verified to invoke some of these directly,
independent of pre-commit) — but the local first-line-of-defense layer
that .pre-commit-config.yaml was written to provide has been silently
inactive this entire time.
Severity: Medium (defense-in-depth gap — CI workflows partially cover the
same tools, so this isn't the only check, but every commit reaching CI
has already skipped the fast local gate that's supposed to catch issues
before push).
File: .git/hooks/ (missing pre-commit hook), .pre-commit-config.yaml
Status: OPEN — Action: run `pre-commit install` once in this clone (and
document it in README/setup script as a required setup step — checked
setup_dev.ps1 does not currently call it either, see follow-up).
