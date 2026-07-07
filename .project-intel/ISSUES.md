# Issues & Bugs
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known bugs before implementing

## Issue-001 [2026-06-23]
DIAGNOSTICS.md reported ruff and pyright not installed in CI environment.
Affects: CI linting gates may pass without actual lint checks.
Severity: Medium. Action: Verify ruff installed in CI venv (see pyproject.toml — ruff IS configured).
Status: VERIFIED [2026-07-06] — ruff 0.4.4 confirmed in .venv. mypy and pyright
are NOT in .venv and NOT installed globally. bandit/semgrep also absent.
CI runs ruff only (via pre-commit). mypy/pyright remain optional dev tools;
no CI gate depends on them — acceptable for current maturity. No action needed.
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

## Issue-003 [2026-06-24] — RESOLVED (verified 2026-06-29, independent audit session)
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
VERIFIED 2026-06-29: frontend/package.json declares "recharts": "^3.8.1"; frontend/node_modules/recharts/package.json resolves to "version": "3.8.1" — declared and installed versions now match. `npm audit --omit=dev` reports 0 vulnerabilities (also closes SEC-001's residual lodash transitive concern). Closing.
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
Status: RESOLVED [2026-07-05] — Docstring corrected to "Alert only — no auto-restart (manual operator intervention required for crashed tasks)"; log action updated to "monitor_offline — alert only, manual restart required". No behavioral change — documentation now matches implementation.
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
Status: RESOLVED [2026-07-05] — Comment corrected to "Build-time config sanity check: rejects malformed/non-http(s) VITE_API_URL values (operator misconfiguration guard — does NOT block internal IPs)."

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
Status: RESOLVED [2026-07-05] — `pre-commit install` run; .git/hooks/pre-commit now exists.

## Issue-006 [2026-06-29] — NEW (audit session, Amazon Q)
src/diagnostics/runtime_monitor.py documents "auto-recovery" behavior that does not exist
(originally flagged as Issue-004). This has been re-verified as still open as of 2026-06-29.
A secondary, related issue is now also logged here:

The RuntimeMonitor's _on_task_done() callback logs action="monitor_offline — restart required"
when a monitored asyncio.Task crashes, but there is no supervisor loop, no coroutine factory
registry, and no asyncio.create_task() call anywhere in the monitor that would recreate a
dead task. If the orchestrator's main tick loop crashes (e.g. due to an unhandled exception
in the ccxt fetch path or signal engine), the bot silently stops trading with no self-healing
and no alert beyond a structlog entry that operators may not be watching.

The risk is compounded by orchestrator.py's 10% test coverage (Debt-009) — the crash paths
that would trigger this non-recovery have essentially no regression protection.

Severity: Medium-High (operational safety — a crashed tick loop means no new signals, no
position monitoring, no stop-loss execution. Positions stay open indefinitely with no oversight.)
File: src/diagnostics/runtime_monitor.py, src/engine/orchestrator.py
Status: RESOLVED [2026-07-05] — Docstring + log message corrected (option b, minimum viable fix). See Issue-004 resolution. Task-factory supervisor (option a) remains a future enhancement if needed.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────

## Issue-007 [2026-07-06] — CRITICAL BUG (discovered during coverage work)
LiveExecutor._extract_fee() called at live.py:408 (close_position) and live.py:699
(_place_and_record) but method was NEVER DEFINED anywhere in the class or its base.
Every real order placed or closed would raise AttributeError at exchange-interaction time —
i.e. the bot could place an order but crash before recording the position, leaking cash.

Root cause: method was likely planned but never implemented; coverage gap (27%) hid it.
File: src/execution/live.py
Status: RESOLVED [2026-07-06] — Implemented _extract_fee() with ccxt unified fee
structure parsing (fees list, fee single-dict fallback, quote-currency filter,
_LIVE_FEE_FALLBACK for missing data). 32 new tests validate the paths that call it.
Discovered by: Claude [claude] during Debt-009 coverage work
────────────────────────────────────────────────────────────

## Issue-008 [2026-07-07] — NEW (audit session, Amazon Q)
src/diagnostics/runtime_monitor.py module docstring (line 2) still reads:
  "Runtime Monitor — continuous async health diagnostics with auto-healing."
Issue-004 and Issue-006 were both resolved by correcting the body docstring
(line 8: "Alert only — no auto-restart") and the log message (line 160:
"monitor_offline — alert only, manual restart required"). However the
module-level one-liner on line 2 was NOT updated and still claims "auto-healing".
A contributor reading only the module summary (e.g. via IDE hover, pydoc, or
the MODULE_MAP.json description field) gets the old misleading claim.
Severity: Low (documentation accuracy — the body docstring is correct; only
the one-liner summary is stale).
File: src/diagnostics/runtime_monitor.py line 2
Status: OPEN — Action: change line 2 to:
  "Runtime Monitor — continuous async health diagnostics (alert-only, no auto-restart)."
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────

## Issue-009 [2026-07-07] — NEW (audit session, Amazon Q)
tests/test_context_builder.py::test_summarize_source_file_uses_compact_ast_summary
is the 1 failing test in the suite (confirmed: 899 passed, 1 failed, 1 skipped).
Root cause: summarize_source_file() in .project-intel/scripts/context_builder.py
filters symbols by query relevance (line ~115: `if not terms or any(term in name_l
for term in terms)`). When query="compute", only the `compute` function matches;
`Worker` class does not match and is omitted from the output. The test asserts
`"worker" in summary.lower()` which fails because Worker is counted in
"1 additional symbols omitted" but not shown.
The function's intent is to show relevant symbols first, but the test expects ALL
top-level symbols to appear when there are only 2 (a reasonable expectation for
small files). The fix is to always show all symbols when total count <= 4, or
always include non-matching symbols after the relevant ones up to the 6-symbol cap.
Severity: Medium (CI gate failure — this test fails on every run, polluting CI
output and masking real failures).
File: .project-intel/scripts/context_builder.py (~line 115-132),
      tests/test_context_builder.py line 14
Status: OPEN — Action: in summarize_source_file(), when len(relevant) < 6 and
len(definitions) > len(relevant), append remaining definitions up to the 6-symbol
cap so small files always show all symbols.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────
