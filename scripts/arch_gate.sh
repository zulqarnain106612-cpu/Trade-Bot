#!/usr/bin/env bash
# Crypto Architect governance gate.
#
# Single entry point for the architectural-law validator shipped with the
# `crypto-architect` skill, so CI, pre-commit and humans all run the same
# command with the same baseline. Findings recorded in the baseline are
# accepted debt; the gate fails only on newly introduced violations.
#
# Usage:
#   scripts/arch_gate.sh                       # gate src/ against the baseline
#   scripts/arch_gate.sh --sarif out.sarif     # gate + emit SARIF for CI upload
#   scripts/arch_gate.sh --file src/risk/gate.py   # gate a single file
#   scripts/arch_gate.sh --refresh-baseline    # accept current findings
#
# Env overrides: ARCH_SCAN_DIR, ARCH_FAIL_ON, ARCH_MIN_SEVERITY, ARCH_COMPONENT,
#                ARCH_PYTHON (CI sets `python` — the validator is stdlib-only,
#                so the governance job needs no dependency install)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VALIDATOR=".claude/skills/crypto-architect/scripts/validate_arch.py"
BASELINE="config/arch_baseline.json"
SCAN_DIR="${ARCH_SCAN_DIR:-src}"
FAIL_ON="${ARCH_FAIL_ON:-HIGH}"
MIN_SEVERITY="${ARCH_MIN_SEVERITY:-MEDIUM}"
COMPONENT="${ARCH_COMPONENT:-trade-bot}"

if [[ ! -f "$VALIDATOR" ]]; then
  echo "ERROR: validator missing at $VALIDATOR (crypto-architect skill not installed)" >&2
  exit 2
fi

SARIF_OUT=""
TARGET_ARGS=(--dir "$SCAN_DIR")
REFRESH=0
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sarif)
      SARIF_OUT="$2"; shift 2 ;;
    --refresh-baseline)
      REFRESH=1; shift ;;
    --file)
      FILES+=("$2"); shift 2 ;;
    *)
      # Bare paths: pre-commit passes changed filenames positionally.
      FILES+=("$1"); shift ;;
  esac
done

# `uv run` per project policy — never bare python3.
ARCH_PYTHON="${ARCH_PYTHON:-uv run python}"

run_validator() {
  # shellcheck disable=SC2086  # ARCH_PYTHON is an intentional multi-word command
  $ARCH_PYTHON "$VALIDATOR" --component "$COMPONENT" --non-interactive "$@"
}

if [[ $REFRESH -eq 1 ]]; then
  run_validator --dir "$SCAN_DIR" --write-baseline "$BASELINE"
  echo "Review the baseline diff before committing: git diff $BASELINE"
  exit 0
fi

if [[ ${#FILES[@]} -gt 0 ]]; then
  # Per-file mode: the validator takes one file at a time. Scoped LAW checks
  # and the cross-file pass only apply to a full scan, so file mode is a
  # fast pre-commit screen, not a substitute for the CI gate.
  status=0
  for f in "${FILES[@]}"; do
    [[ -f "$f" ]] || continue
    run_validator --file "$f" --baseline "$BASELINE" \
      --min-severity "$MIN_SEVERITY" --fail-on "$FAIL_ON" || status=1
  done
  exit $status
fi

if [[ -n "$SARIF_OUT" ]]; then
  # SARIF is written unconditionally so CI can upload it even on failure.
  set +e
  run_validator "${TARGET_ARGS[@]}" --baseline "$BASELINE" --fail-on "$FAIL_ON" \
    --sarif --output-file "$SARIF_OUT"
  sarif_status=$?
  set -e
  run_validator "${TARGET_ARGS[@]}" --baseline "$BASELINE" --fail-on "$FAIL_ON" \
    --min-severity "$MIN_SEVERITY" || true
  exit $sarif_status
fi

run_validator "${TARGET_ARGS[@]}" --baseline "$BASELINE" \
  --min-severity "$MIN_SEVERITY" --fail-on "$FAIL_ON"
