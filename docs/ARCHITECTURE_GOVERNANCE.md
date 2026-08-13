# Architecture Governance

The `crypto-architect` skill is the design authority for this repository. It
ships 13 architectural laws, 13 on-demand domain references, and a static
validator, and it is wired into the same three places every other quality
control in this repo lives: the agent's own workflow, pre-commit, and CI.

## Layout

| Path | Role |
|---|---|
| `.claude/skills/crypto-architect/SKILL.md` | The 13 laws, red-flag table, architect workflow |
| `.claude/skills/crypto-architect/references/*.md` | Domain deep-dives, loaded on demand |
| `.claude/skills/crypto-architect/scripts/validate_arch.py` | Static law validator (stdlib only) |
| `scripts/arch_gate.sh` | Single entry point used by CI, pre-commit, humans |
| `config/arch_baseline.json` | Accepted pre-existing findings |

## Wiring

- **Claude sessions** — the skill auto-activates on any crypto/trading/risk/key
  task from its `description`. `CLAUDE.md` declares it as outranking
  convenience, and points at the gate.
- **Code review** — `.claude/agents/code-reviewer.md` runs the gate and applies
  the red-flag table; `.github/workflows/claude-code-review.yml` passes the same
  instruction to the PR reviewer.
- **pre-commit** — `crypto-architect` hook, full-tree scan, triggered by any
  `src/**.py` change.
- **CI** — `architecture` job in `ci.yml`. Stdlib-only, so it needs no
  dependency install and finishes in seconds. Emits SARIF, uploads it to GitHub
  code scanning under the `crypto-architect` category, and archives it as an
  artifact.

## Running it

```bash
scripts/arch_gate.sh                          # gate src/ against the baseline
scripts/arch_gate.sh --sarif arch.sarif       # gate + SARIF (what CI runs)
scripts/arch_gate.sh src/risk/gate.py         # fast single-file screen
scripts/arch_gate.sh --refresh-baseline       # re-accept current findings
ARCH_FAIL_ON=MEDIUM scripts/arch_gate.sh      # tighten the threshold
```

The gate fails on HIGH and CRITICAL findings. Suppress a genuine false positive
with `# noqa:arch` on the offending line — never by loosening a pattern.

## The baseline is debt, not approval

`config/arch_baseline.json` records the 58 findings that existed when the gate
was installed, so the gate can fail on *new* violations without blocking on
pre-existing ones. Four of them are CRITICAL and are real, unresolved risks in
a system that moves real money:

| Law | Finding | Location |
|---|---|---|
| LAW3 | No idempotency key on any order-submission path | `src/execution/{base,live,order_manager,router}.py` |
| LAW3 | `place_order_with_fsm` takes no idempotency key | `src/execution/order_manager.py:41` |
| LAW1 | Risk score computed without VaR/CVaR | `src/risk/cognitive_engine.py:456` |
| LAW10 | No wash-trade guard anywhere in the codebase | `src/execution/{order_manager,router}.py` |

Also outstanding at HIGH: no `correlation_id` propagation in the order path
(LAW7), no flash-crash/market-halt handling in the risk gates (LAW13), no
confidence-threshold gate in `src/engine/signal_engine.py` (LAW4), and
deprecated hash functions in use (LAW12).

Closing any of these means deleting its line from the baseline in the same
commit. Adding lines to the baseline is a deliberate decision that belongs in
`DECISION_LOG.md`, not a way to turn a red gate green.

## Validator changes made during wiring

The v3 validator as shipped applied every `REQUIRED` pattern to every file, so
a full `src/` scan produced 1,210 CRITICAL/HIGH findings — one per file for
each law — which is not gateable. Wiring it required four fixes:

1. `REQUIRED` entries gained a `scope` regex, so a law's required pattern is
   demanded only of the modules that own that law. 1,210 → 33.
2. The blind-signing pattern was anchored to actual signing APIs; the bare
   `sign(` form matched every `np.sign()` in the feature and risk code.
3. Findings report repo-relative paths, not `path.name` — `base.py` is
   ambiguous, and SARIF needs a locatable URI.
4. `cross_file_checks` keyed its source map by filename, silently dropping
   same-named files (`__init__.py`, `base.py`) from the combined scan text.

Baseline support (`--baseline`, `--write-baseline`) and directory exclusions
(`node_modules`, caches, `.venv`) were added for the same reason.
