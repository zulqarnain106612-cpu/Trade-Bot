# Findings to carry into the next plan

These are verified discoveries outside the current aggregate-coverage repair. Preserve them as follow-up work rather than expanding this PR into repository-wide governance or cleanup changes.

## 1. Ruleset bypass requires an explicit governance decision

The active `main: all checks green` ruleset requires CI, Security, CodeQL, code-owner review, and review-thread resolution. Its API snapshot also exposes a repository-role bypass actor with `bypass_mode: always` and reports that the connected account can bypass the ruleset. The branch-protection endpoint returned HTTP 403 to the installed GitHub integration, so traditional branch-protection settings were not independently verified.

Next-plan action: inspect the effective rules in the repository owner UI or an appropriately authorized API session; decide whether the broad bypass should be removed or constrained to a documented break-glass path; test enforcement without merging a deliberately failing PR.

## 2. Local worktree inventory needs a preservation-first reconciliation

The primary checkout has a modified `.claude/launch.json`, an untracked `.vscode/launch.json`, and untracked files under `build/lib/src/models/`. Additional sibling worktrees exist for audit, drag/drop, GUI runtime, and terminal work. Several `/tmp` worktrees are marked prunable because their gitdir points to a missing location.

Next-plan action: inspect ownership, branch reachability, and whether generated files contain user work before proposing cleanup. Do not run `git clean`, delete worktrees, or discard launch/build artifacts as part of routine reconciliation.

## 3. The approved CI-notice route depends on working public/API access

`gh auth status` reports no logged-in GitHub host, and neither `GITHUB_TOKEN` nor `GH_TOKEN` was present in the inspected shell. The repository policy only permits CI diagnostic consumption through `python3 scripts/local_checks.py prepare`, which fetches the single notice whose hidden SHA marker exactly matches the open PR head.

Next-plan action: verify that this supported notice path works on a fresh PR without widening CI-log access or pulling arbitrary comments into agent context; if authentication or API rate limits prevent it, establish a narrowly scoped, approved credential path rather than reading workflow logs directly.
