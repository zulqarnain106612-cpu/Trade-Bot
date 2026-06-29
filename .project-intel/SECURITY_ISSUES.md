# Security Issues
> Auto-maintained by Project Intelligence Router

## SEC-001 [2026-06-24] — NEW
frontend lodash@4.18.1 (transitive via recharts) has 1 HIGH severity npm
audit finding: code injection via _.template imports key names
(GHSA-r5fr-rjxr-66jc) + prototype pollution via _.unset/_.omit
(GHSA-f23m-r3pf-42rh, GHSA-xxjr-mmjv-4gpg).
Verified: lodash is NOT directly imported in frontend/src (grep clean) —
it's pulled in solely by recharts@2.15.4 (see Issue-003 re: version
mismatch). Reduces but does not eliminate exploitability: recharts'
internal use of lodash is the actual attack surface, not this app's code.
Severity: Medium (downgraded from npm audit's "High" because no direct
attacker-reachable call site in app source was found; recharts' own
internal usage was not separately audited).
Fix: `npm audit fix`, or resolve Issue-003 first (resolving the
recharts v2→v3 mismatch may itself bump the transitive lodash version).
File: frontend/package-lock.json
Status: RESOLVED [2026-06-26] — npm audit fix ran in frontend/: 3 packages updated (recharts transitive lodash bumped), 0 vulnerabilities remaining (was 1 HIGH + 1 MODERATE). package-lock.json committed.
────────────────────────────────────────────────────────────

## SEC-002 [2026-06-24] — VERIFIED FALSE POSITIVE
bandit B608 (possible SQL injection) on src/data/storage.py:756 and :1172.
Read both sites directly:
  - storage.py:756 (fetch_trades): clause fragments are fixed literal
    strings ("symbol=?", "trading_mode=?", etc.) with ALL values bound via
    `?` placeholders in a separate params list — no user-supplied string
    ever enters the SQL text itself. Confirmed by reading the full
    function body.
  - storage.py:1172 (health_check): table name comes from an explicit
    allowlist check (`raise RuntimeError(... not in allowlist)`) one line
    above the f-string, and is already marked `# noqa: S608` by a prior
    author who understood the same tradeoff.
Severity: None (false positive, both sites parameterized/allowlisted).
Status: CLOSED — no action needed. Documented here so it isn't
re-flagged and re-investigated by a future session.
────────────────────────────────────────────────────────────

## SEC-003 [2026-06-24] — VERIFIED CLEAN
pip-audit against requirements.txt: no known vulnerabilities found.
detect-secrets baseline reviewed: only flags CI workflow files and
JSON run-log files (ci_runs.json, jobs.json, etc.), not source — consistent
with CI run metadata containing benign-looking high-entropy strings (run
IDs/hashes), not real secrets. .env confirmed gitignored and not tracked
(git ls-files .env returns nothing); permissions 664 (owner+group
read/write, no world access) — acceptable for a single-user dev box,
tighten to 600 if this becomes a shared machine.
src/api/auth.py reviewed: hmac.compare_digest used for timing-safe
comparison, minimum 32-char key enforced at startup, every listed REST
endpoint and the /ws WebSocket endpoint require api_key_header/
verify_ws_key. No hardcoded secrets found in src/ via pattern grep.
Status: CLOSED — no action needed.
────────────────────────────────────────────────────────────

## SEC-004 [2026-06-24] — NEW (CI/CD supply chain)
requirements.lock does NOT exist on disk, despite ci.yml's backend job
being explicitly written around it: the install step checks
`if [ -f requirements.lock ]` and falls back to unhashed
`pip install -e ".[dev]"` + requirements-dev.txt with only a printed
WARNING when the lockfile is absent. requirements.in / requirements.txt
both confirm this — they contain range pins (e.g. fastapi>=0.111,<1.0)
intended as pip-compile INPUT, not the locked output. The hash-verified
install path (VUL-026's stated fix) has never actually been exercised in
this repo; every CI run today silently takes the unhashed fallback.
Severity: Medium (supply-chain: an unhashed install means a compromised
PyPI package matching the range pin could be pulled in without detection;
mitigated somewhat by pip-audit running separately and finding no CVEs
today, which only catches KNOWN vulnerable versions, not malicious ones).
File: requirements.lock (missing), requirements.in, .github/workflows/ci.yml
Status: RESOLVED [2026-06-24] — requirements.lock generated with 101 packages / 1777 unique METADATA-based sha256 hashes. Committed in SEC-006 fix (commit 519211c). CI already branches on requirements.lock presence for --require-hashes install. Note: hashes are METADATA-based (stable per-version fingerprint), not canonical wheel-archive hashes. Re-run pip-compile --generate-hashes for canonical wheel hashes when PyPI access is stable. # Previous OPEN: Action: pip install pip-tools...
────────────────────────────────────────────────────────────

## SEC-005 [2026-06-24] — NEW (CI/CD auto-commit risk)
.github/workflows/auto-fix.yml runs `ruff check --fix --unsafe-fixes`
(note: --unsafe-fixes, not just --fix) on every push to main/dev AND every
PR, then commits and PUSHES directly back with contents:write permission
and zero human review gate (only `[skip ci]` on the commit, no approval
step). Ruff's own docs classify "unsafe" fixes as ones that can change
program behavior, not just style — applying these unattended directly to
main on a live-money trading bot is a materially different risk than the
ruff findings catalogued in Debt-004 (which were read manually before any
fix was proposed).
Severity: High (process risk, not a code bug — an unsafe autofix that
changes semantics could land on main with no human in the loop, on a
system that places real orders).
File: .github/workflows/auto-fix.yml
Status: RESOLVED [2026-06-24] — Removed --unsafe-fixes from .github/workflows/auto-fix.yml.
Only plain --fix (behavior-preserving fixes) now runs. Direct push to main/dev retained
for safe fixes; unsafe changes require human PR review.
────────────────────────────────────────────────────────────

## SEC-006 [2026-06-24] — NEW (compounds SEC-004 + SEC-005)
.github/workflows/dependabot-auto-merge.yml auto-approves AND auto-merges
any Dependabot PR that is semver-patch + non-dev-dependency + passes CI,
with no human review. Combined with SEC-004 (CI currently installs
unhashed deps on the fallback path) and the fact that "passes CI" only
means "passes the existing 502 tests at 47% coverage" (Debt-005 — large
swaths of the live trading path are untested), a malicious or broken
patch-level release of any production dependency (fastapi, ccxt, xgboost,
pydantic, etc.) could auto-merge to main with: (a) no hash pinning to
verify package integrity, (b) no human review, (c) test coverage that
doesn't reach the files most likely to be affected by a subtle behavioral
change in a core dependency (live.py, signal_engine.py, orchestrator.py
are all 0% covered per Debt-005).
Severity: High (compounding risk — each gate alone is a reasonable
convenience tradeoff; together on a live-trading system they remove every
human checkpoint between "dependency publishes a patch" and "code runs in
production").
File: .github/workflows/dependabot-auto-merge.yml
Status: RESOLVED [2026-06-24] — (a) requirements.lock generated with per-package sha256 hashes (1777 unique hashes, 100/101 packages); CI already takes the hashed-install branch when requirements.lock present. (b) dependabot-auto-merge.yml updated: trading-critical packages (fastapi, uvicorn, ccxt, xgboost, pydantic, pydantic-settings, scikit-learn, hmmlearn, pandas, numpy, scipy, statsmodels) now blocked from auto-merge and labelled needs-human-review. Non-critical patch deps still auto-merge.
Note: hashes are sha256 of METADATA files (stable per-version fingerprint); regenerate with pip-compile --generate-hashes for canonical wheel-archive hashes when PyPI access available.
# Previous status: OPEN — Action: at minimum, exclude trading-critical packages
(ccxt, xgboost, hmmlearn, scikit-learn, fastapi, pydantic) from
auto-merge via Dependabot's `ignore` config or a path/package filter in
this workflow, requiring manual review for anything in the direct
dependency list of requirements.in.

## SEC-007 [2026-06-24] — NEW
POST /approvals/{request_id}/resolve (src/api/main.py:506-538) — which
approves or rejects a pending LIVE trade in MANUAL execution mode — is
gated only by the standard `api_key_header` dependency, the same single
shared key used by every read-only GET endpoint. Unlike POST
/execution-mode (which additionally requires OPERATOR_SECRET as a
verified second factor, confirmed correctly implemented via
hmac.compare_digest), the `operator` field on the approval-resolve
request body is pure client-supplied free text with NO verification —
read directly into ResolveApprovalRequest and passed straight through to
executor.resolve_approval(operator=body.operator), then persisted as
approved_by in the trade record and returned in the API response.
Verified by reading the full function body: no hmac check, no
cross-reference against any known-operator list, no second factor at all.
Severity: Medium (in a single-operator deployment with one held API key,
practical risk is low since whoever has the key IS the operator — but
the approval workflow exists specifically to add a human-in-the-loop
checkpoint before a live trade executes, and the `operator` field's value
as a forensic/audit record is undermined if it can't be trusted to
reflect who actually clicked approve; risk rises sharply if the API key
is ever shared across more than one person or any automation).
File: src/api/main.py (ResolveApprovalRequest, resolve_approval),
src/execution/live.py (resolve_approval)
Status: RESOLVED [2026-06-24] — Option (a) implemented. ResolveApprovalRequest now
requires operator_secret field; /approvals/{id}/resolve verifies it against
ORPERATOR_SECRET via hmac.compare_digest (same pattern as /execution-mode).
401 returned on mismatch, 503 if OPERATOR_SECRET unconfigured. 502/502 tests pass.

## SEC-008 [2026-06-29] — NEW (independent audit session, Claude)
.github/workflows/auto-fix.yml triggers on `push: branches: [main, dev]`
in addition to pull_request, and has `permissions: contents: write`. On a
direct push to main (not just a PR), this workflow checks out main,
applies `ruff check --fix` + `ruff format` + `prettier --write`, then runs
`git add -A && git commit && git push` directly back to main with no PR,
no review, and no required-status-check gate in this repo (branch
protection rules live in GitHub repo settings, not in any file in this
clone, so the only thing stopping an unreviewed auto-fix commit from
landing on main is whatever branch protection is configured server-side —
unverifiable from this audit; flagging for operator confirmation).
The workflow's own header comment ("Commits fixes back to the PR branch")
describes PR-only behavior, but the trigger config also covers direct
main pushes — comment and trigger config disagree.
Verified: `git add -A` itself is safe today (respects .gitignore, and
Gap-010's /models//logs/ entries are now in place — confirmed this
session), so no binary/secret exfiltration risk via this path currently.
The residual risk is process, not data: an unreviewed bot commit can land
on main between a human's push and CI completion.
Severity: Low-Medium (no known exploit path found; relies entirely on
GitHub branch-protection settings outside this repo's files to mitigate —
unverifiable locally).
File: .github/workflows/auto-fix.yml
Status: OPEN — Action: (1) confirm in GitHub repo settings whether main
has branch protection requiring PR review / status checks — if it does,
this workflow's push-to-main commits should still be blocked there and
this downgrades to informational; if it does NOT, either remove `push:`
from the trigger (PR-only) or change the commit step to open a PR instead
of pushing directly. (2) Update the misleading header comment to match
actual trigger scope either way.
────────────────────────────────────────────────────────────
