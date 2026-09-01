#!/usr/bin/env python3
"""
Crypto Architect Validation Script v3.0
SARIF output, Law 12-13 patterns, fixed Severity ordering, TypeScript/Go/Rust
pattern analysis, cross-file checks, conformal gates, --output-file support.

Usage:
  python scripts/validate_arch.py --component <name> [options]

Options:
  --component NAME   Component name (required)
  --check LAW...     Law IDs to check (default: all)
  --file PATH        Single source file to scan
  --dir PATH         Directory to scan recursively
  --non-interactive  Skip design checklist (CI mode)
  --json             Output JSON report (machine-readable)
  --sarif            Output SARIF v2.1 (GitHub Advanced Security compatible)
  --output-file PATH Write report to file instead of stdout
  --min-severity S   Minimum severity to report: CRITICAL|HIGH|MEDIUM (default: MEDIUM)
  --fail-on S        Exit 1 if any finding >= severity: CRITICAL|HIGH|MEDIUM (default: HIGH)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Severity ──────────────────────────────────────────────────────────────────

_SEV_ORDER = ["INFO", "MEDIUM", "HIGH", "CRITICAL"]


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    INFO = "INFO"

    def _rank(self) -> int:
        return _SEV_ORDER.index(self.value)

    def __lt__(self, other: "Severity") -> bool:
        return self._rank() < other._rank()

    def __le__(self, other: "Severity") -> bool:
        return self._rank() <= other._rank()

    def __gt__(self, other: "Severity") -> bool:
        return self._rank() > other._rank()

    def __ge__(self, other: "Severity") -> bool:
        return self._rank() >= other._rank()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Severity):
            return self._rank() == other._rank()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class Finding:
    law: str
    check: str
    passed: bool
    severity: Severity
    evidence: str
    file: Optional[str] = None
    line: Optional[int] = None
    suppressed: bool = False

    @property
    def fingerprint(self) -> str:
        """Stable identity of a finding, for baseline matching.

        Deliberately excludes the line number and the evidence text so that
        moving code within a file does not resurrect an accepted finding.
        """
        return f"{self.law}|{self.check}|{self.file or '-'}"

    def to_dict(self) -> dict:
        return {
            "law": self.law,
            "check": self.check,
            "passed": self.passed,
            "severity": self.severity.value,
            "evidence": self.evidence,
            "file": self.file,
            "line": self.line,
            "suppressed": self.suppressed,
        }


@dataclass
class ValidationReport:
    component: str
    findings: list[Finding] = field(default_factory=list)

    def apply_baseline(self, fingerprints: set[str]) -> int:
        """Mark known-accepted findings as suppressed; return how many matched.

        The gate then fails only on findings introduced after the baseline was
        written, which is what makes it usable on an existing codebase.
        """
        count = 0
        for f in self.findings:
            if not f.passed and f.fingerprint in fingerprints:
                f.suppressed = True
                count += 1
        return count

    def open_failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.passed and not f.suppressed]

    def fingerprints(self) -> list[str]:
        return sorted({f.fingerprint for f in self.findings if not f.passed})

    def passed(self, fail_threshold: Severity = Severity.HIGH) -> bool:
        return not any(f.severity >= fail_threshold for f in self.open_failures())

    def stats(self) -> dict:
        by_severity: dict[str, int] = {}
        fails = self.open_failures()
        for f in fails:
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
        return {
            "total_checks": len(self.findings),
            "failures": len(fails),
            "suppressed": sum(1 for f in self.findings if f.suppressed),
            "by_severity": by_severity,
        }

    def print_text(
        self,
        min_severity: Severity = Severity.MEDIUM,
        fail_threshold: Severity = Severity.HIGH,
    ) -> None:
        w = 72
        print(f"\n{'=' * w}")
        print(f"COMPONENT: {self.component}  |  Crypto Architect v3")
        print(f"{'=' * w}")
        printed = 0
        for f in self.open_failures():
            if f.severity >= min_severity:
                loc = f"  [{f.file}:{f.line}]" if f.file and f.line else ""
                print(f"[FAIL][{f.severity.value:<8}][{f.law}] {f.check}{loc}")
                print(f"       ↳ {f.evidence}")
                printed += 1
        if printed == 0:
            print(f"  (no findings at or above {min_severity.value} severity)")
        s = self.stats()
        result_str = "PASS" if self.passed(fail_threshold) else "FAIL"
        print(f"{'=' * w}")
        print(
            f"RESULT: {result_str} | Checks: {s['total_checks']} | "
            f"Failures: {s['failures']} | By severity: {s['by_severity']}"
        )
        print(f"{'=' * w}\n")

    def to_dict(self, fail_threshold: Severity = Severity.HIGH) -> dict:
        return {
            "component": self.component,
            "passed": self.passed(fail_threshold),
            "stats": self.stats(),
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_sarif(self) -> dict:
        """SARIF v2.1.0 format for GitHub Advanced Security."""
        rules: list[dict] = []
        seen_rule_ids: set[str] = set()
        results: list[dict] = []

        sev_sarif = {
            Severity.CRITICAL: "error",
            Severity.HIGH: "error",
            Severity.MEDIUM: "warning",
            Severity.INFO: "note",
        }

        for f in self.open_failures():
            rule_id = f"{f.law}-{re.sub(r'[^a-z0-9]', '-', f.check.lower())[:40]}"
            if rule_id not in seen_rule_ids:
                seen_rule_ids.add(rule_id)
                rules.append(
                    {
                        "id": rule_id,
                        "name": f.check,
                        "shortDescription": {"text": f"{f.law}: {f.check}"},
                        "defaultConfiguration": {"level": sev_sarif.get(f.severity, "warning")},
                        "properties": {
                            "security-severity": {
                                Severity.CRITICAL: "9.0",
                                Severity.HIGH: "7.0",
                                Severity.MEDIUM: "4.0",
                                Severity.INFO: "1.0",
                            }.get(f.severity, "4.0")
                        },
                    }
                )

            location: dict = {
                "message": {"text": f.evidence},
            }
            if f.file and f.line:
                location["physicalLocation"] = {
                    "artifactLocation": {"uri": f.file},
                    "region": {"startLine": f.line},
                }

            results.append(
                {
                    "ruleId": rule_id,
                    "level": sev_sarif.get(f.severity, "warning"),
                    "message": {"text": f"{f.check}: {f.evidence}"},
                    "locations": [location],
                }
            )

        return {
            "version": "2.1.0",
            "$schema": ("https://json.schemastore.org/sarif-2.1.0.json"),
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "crypto-architect-validator",
                            "version": "3.0.0",
                            "rules": rules,
                        }
                    },
                    "results": results,
                    "properties": {
                        "component": self.component,
                        "passed": self.passed(),
                    },
                }
            ],
        }


# ── Pattern-based checks (all languages) ─────────────────────────────────────

# Each entry: (law, regex, description, severity)
# Use # arch-ignore on a source line to suppress pattern hits.
FORBIDDEN: list[tuple[str, str, str, Severity]] = [
    # LAW6 — secrets
    (
        "LAW6",
        r"os\.environ\[.*?(KEY|SECRET|PASS|TOKEN|MNEMONIC)",
        "Secret accessed from env var — use Vault",
        Severity.CRITICAL,
    ),
    (
        "LAW6",
        r'private_key\s*=\s*["\']',
        "Private key hardcoded as string literal",
        Severity.CRITICAL,
    ),
    ("LAW6", r'api_secret\s*=\s*["\']', "API secret hardcoded", Severity.CRITICAL),
    (
        "LAW6",
        r'password\s*=\s*["\'](?!.{0,3}#.*placeholder)',
        "Password hardcoded",
        Severity.CRITICAL,
    ),
    ("LAW6", r'mnemonic\s*=\s*["\']', "BIP39 mnemonic hardcoded", Severity.CRITICAL),
    # LAW3 — execution bypasses
    (
        "LAW3",
        r"skip_risk\s*=\s*True|skip_risk\s*=\s*true",
        "Risk gate bypass flag",
        Severity.CRITICAL,
    ),
    (
        "LAW3",
        r"bypass_risk|bypass_validation|skip_validation",
        "Risk/validation bypass detected",
        Severity.CRITICAL,
    ),
    # LAW5 — TLS
    (
        "LAW5",
        r"verify\s*=\s*False|verify:\s*false|InsecureSkipVerify\s*:\s*true",
        "TLS verification disabled",
        Severity.CRITICAL,
    ),
    (
        "LAW5",
        r"requests\.(get|post|put|delete)\(.*verify=False",
        "HTTP request with TLS verification disabled",
        Severity.CRITICAL,
    ),
    # LAW7 — secret leakage in logs
    (
        "LAW7",
        r"logger?\.(info|debug|warning|error|warn)\(.*?(secret|api_key|private_key|password|mnemonic)",
        "Potential secret in log output",
        Severity.HIGH,
    ),
    (
        "LAW7",
        r"print\(.*?(private_key|api_secret|password|mnemonic)",
        "Potential secret in print statement",
        Severity.HIGH,
    ),
    # LAW9 — model without confidence
    (
        "LAW9",
        r"model\.predict\((?!.*confidence)",
        "Model prediction without confidence check",
        Severity.HIGH,
    ),
    # LAW10 — order without wash guard
    (
        "LAW10",
        r"submit_order\((?!.*wash)",
        "Order submission without wash-trade guard (verify guard is upstream)",
        Severity.MEDIUM,
    ),
    # LAW6 — .env pattern
    (
        "LAW6",
        r"\.env(?:ironment)?\b.*\bsecret",
        "Secret referenced via .env pattern",
        Severity.HIGH,
    ),
    # LAW6 — blind signing.
    # Anchored on signing APIs, not a bare `sign(`: the loose form matched
    # every `np.sign()` in the feature and risk code and buried real hits.
    (
        "LAW6",
        r"\b(sign_transaction|signTransaction|sign_tx|sign_raw|sign_message|"
        r"signMessage|sign_typed_data|signTypedData|eth_sign|personal_sign|"
        r"sign_and_send|signAndSend)\s*\((?!.*decode|.*display|.*human_readable|.*show)",
        "Potential blind signing — ensure transaction decoded before signing",
        Severity.HIGH,
    ),  # arch-ignore
    # LAW12 — weak key exchange / deprecated crypto
    (
        "LAW12",
        r"DH(?:E|_anon)?\b|DHE_RSA|TLS_RSA_WITH",
        "Non-PFS or export-grade cipher suite reference",
        Severity.HIGH,
    ),
    (
        "LAW12",
        r"MD5|SHA1(?!_\d)|sha1\b",
        "Deprecated hash function (MD5/SHA1) — use SHA-256 or SHA-3",
        Severity.HIGH,
    ),  # arch-ignore
    (
        "LAW12",
        r"RSA(?:_PKCS1_v15|_OAEP)?\b.*key_size\s*=\s*(?:512|1024|2048)",
        "RSA key < 3072 bits is insufficient post-2030",
        Severity.MEDIUM,
    ),
    # LAW13 — adversarial resilience
    (
        "LAW13",
        r"slippage\s*=\s*(?:0\.[5-9][0-9]|[1-9]\d*)",
        "Slippage > 50bps — possible sandwich-attack magnet (review justification)",
        Severity.MEDIUM,
    ),
    (
        "LAW13",
        r"flash_crash_halt|circuit_breaker",
        "Flash crash halt or circuit breaker reference — verify it is wired correctly",
        Severity.INFO,
    ),
]

# Each entry: (law, regex, description, severity, scope)
#
# `scope` is a regex matched against the POSIX path of the file being scanned.
# A required pattern is only demanded of files inside its own domain — asking
# every module in a large codebase for a VaR computation produces one finding
# per file and drowns the real ones. Scope regexes are intentionally narrow:
# they name the modules that own the law, so a missing pattern is a genuine
# architectural gap rather than an artefact of the scan.
REQUIRED: list[tuple[str, str, str, Severity, str]] = [
    (
        "LAW3",
        r"idempotency_key|idempotency|idem_key",
        "Idempotency key on order submission",
        Severity.CRITICAL,
        r"execution/(order_manager|router|live|base)\.py$",
    ),
    (
        "LAW7",
        r"correlation_id|trace_id",
        "Correlation/trace ID in log events",
        Severity.HIGH,
        r"execution/(order_manager|router)\.py$|engine/orchestrator\.py$",
    ),
    (
        "LAW1",
        r"var_|cvar_|value_at_risk|expected_shortfall|VaR|CVaR",
        "VaR/CVaR computation present",
        Severity.CRITICAL,
        r"risk/gates?\.py$",
    ),
    (
        "LAW4",
        r"confidence[_\s]*(threshold|gate|check|>|>=)|conf_threshold",
        "Confidence threshold gating",
        Severity.HIGH,
        r"engine/(signal_engine|orchestrator)\.py$",
    ),
    (
        "LAW9",
        r"model_registry|registry\.get|ModelRegistry",
        "Model registry lookup",
        Severity.HIGH,
        r"engine/signal_engine\.py$|models/model_registry\.py$",
    ),
    (
        "LAW10",
        r"wash_trade|is_wash|wash_guard|check_wash",
        "Wash trade detection",
        Severity.HIGH,
        r"execution/(order_manager|router)\.py$",
    ),
    (
        "LAW12",
        r"ML[_-]KEM|ML[_-]DSA|SLH[_-]DSA|Dilithium|Kyber|pqc_|post_quantum",
        "PQC algorithm reference or migration marker",
        Severity.MEDIUM,
        r"ecc/|security/",
    ),
    (
        "LAW13",
        r"flash_crash|market_halt|halt_new_entries|ADL_rank|socialized_loss",
        "Flash crash / market halt handling",
        Severity.HIGH,
        r"risk/gates?\.py$|risk/strategy_kill_switch\.py$",
    ),
    (
        "LAW4",
        r"conformal|coverage_guarantee|prediction_interval",
        "Conformal prediction coverage for sizing models",
        Severity.MEDIUM,
        r"risk/kelly\.py$|models/model_registry\.py$",
    ),
]


def _is_pattern_definition_line(line: str) -> bool:
    """Heuristic: skip lines that are defining regex patterns in this file itself."""
    stripped = line.strip()
    return stripped.startswith(('("LAW', "(r'", '(r"', '("IO', "# ── Pattern"))


def _rel(path: Path) -> str:
    """Path as reported in findings — repo-relative when possible.

    SARIF and inline PR annotations need a locatable URI; `path.name` alone is
    ambiguous in a codebase with dozens of `__init__.py` / `base.py` files.
    """
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


def check_patterns(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    rel = _rel(path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
    except Exception as exc:
        return [Finding("IO", f"Read {path}", False, Severity.HIGH, str(exc))]

    for law, pattern, desc, severity in FORBIDDEN:
        for i, line in enumerate(lines, 1):
            if "# arch-ignore" in line:
                continue
            if _is_pattern_definition_line(line):
                continue
            if re.search(pattern, line, re.IGNORECASE):
                findings.append(
                    Finding(
                        law=law,
                        check=desc,
                        passed=False,
                        severity=severity,
                        evidence=f"Line {i}: {line.strip()[:120]}",
                        file=rel,
                        line=i,
                    )
                )

    for law, pattern, desc, severity, scope in REQUIRED:
        if not re.search(scope, rel):
            continue
        if not re.search(pattern, source, re.IGNORECASE):
            findings.append(
                Finding(
                    law=law,
                    check=desc,
                    passed=False,
                    severity=severity,
                    evidence=f"Required pattern '{pattern}' missing in {rel}",
                    file=rel,
                )
            )
        else:
            findings.append(
                Finding(
                    law=law,
                    check=desc,
                    passed=True,
                    severity=severity,
                    evidence="Found",
                    file=rel,
                )
            )

    return findings


# ── AST-based checks (Python only) ───────────────────────────────────────────


class ASTChecker(ast.NodeVisitor):
    def __init__(self, filepath: str) -> None:
        self.filepath = filepath
        self.findings: list[Finding] = []

    def _f(
        self, law: str, check: str, passed: bool, severity: Severity, evidence: str, node: ast.AST
    ) -> None:
        self.findings.append(
            Finding(
                law=law,
                check=check,
                passed=passed,
                severity=severity,
                evidence=evidence,
                file=self.filepath,
                line=getattr(node, "lineno", None),
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        func_str = ast.unparse(node.func) if hasattr(ast, "unparse") else ""

        # HTTP request without timeout
        if re.search(r"requests\.(get|post|put|delete|patch)", func_str):
            kwarg_names = {kw.arg for kw in node.keywords}
            if "timeout" not in kwarg_names:
                self._f(
                    "LAW5",
                    "HTTP request missing timeout",
                    False,
                    Severity.MEDIUM,
                    f"Call: {ast.unparse(node)[:80]}",
                    node,
                )

        # hashlib.md5 or hashlib.sha1 — deprecated  # arch-ignore
        if func_str in ("hashlib.md5", "hashlib.sha1", "md5", "sha1"):  # arch-ignore
            self._f(
                "LAW12",
                "Deprecated hash function in use",
                False,
                Severity.HIGH,
                f"Use hashlib.sha256 or hashlib.sha3_256 instead: {func_str}",
                node,
            )

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self._f(
                "LAW7",
                "Bare except clause swallows all exceptions",
                False,
                Severity.MEDIUM,
                "Use 'except Exception as e:' and log the error",
                node,
            )
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._f(
            "LAW3",
            "assert used for validation (stripped with -O flag)",
            False,
            Severity.MEDIUM,
            "Replace with explicit if/raise for production validation",
            node,
        )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        name_lower = node.name.lower()
        body_src = ast.unparse(node) if hasattr(ast, "unparse") else ""

        # Order functions without idempotency
        if any(kw in name_lower for kw in ("send_order", "submit_order", "place_order")):
            if "idempotency" not in body_src.lower():
                self._f(
                    "LAW3",
                    f"Order function '{node.name}' lacks idempotency",
                    False,
                    Severity.CRITICAL,
                    "Add idempotency_key parameter and enforce deduplication",
                    node,
                )

        # Signing functions without decode/display guard
        if any(kw in name_lower for kw in ("sign_tx", "sign_transaction", "sign_order")):
            has_decode = any(
                kw in body_src.lower() for kw in ("decode", "display", "human_readable", "show_tx")
            )
            if not has_decode:
                self._f(
                    "LAW6",
                    f"Signing function '{node.name}' missing transaction decode step",
                    False,
                    Severity.HIGH,
                    "Decode and display transaction before signing (anti-blind-signing)",
                    node,
                )

        # Risk functions missing VaR/CVaR
        if any(kw in name_lower for kw in ("risk_check", "compute_risk", "pre_trade_risk")):
            if not re.search(r"var_|cvar_|VaR|CVaR|value_at_risk", body_src, re.IGNORECASE):
                self._f(
                    "LAW1",
                    f"Risk function '{node.name}' missing VaR/CVaR computation",
                    False,
                    Severity.CRITICAL,
                    "Add VaR(95%) and CVaR computation; see risk.md",
                    node,
                )

        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Global(self, node: ast.Global) -> None:
        self._f(
            "LAW2",
            f"Global variable mutation: {', '.join(node.names)}",
            False,
            Severity.MEDIUM,
            "Shared mutable globals create race conditions; use message passing",
            node,
        )
        self.generic_visit(node)


def check_ast(path: Path) -> list[Finding]:
    if path.suffix != ".py":
        return []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            Finding("AST", "Python parse error", False, Severity.HIGH, str(exc), file=_rel(path))
        ]
    checker = ASTChecker(filepath=_rel(path))
    checker.visit(tree)
    return checker.findings


# ── Cross-file checks ─────────────────────────────────────────────────────────


def cross_file_checks(paths: list[Path]) -> list[Finding]:
    """
    Checks that require visibility across multiple files simultaneously.
    Examples: idempotency defined in utils but missing at call sites.
    """
    findings: list[Finding] = []

    sources: dict[str, str] = {}
    for p in paths:
        try:
            # Keyed by relative path: `p.name` collides across packages
            # (`base.py`, `__init__.py`) and silently drops file contents
            # from the combined text these checks scan.
            sources[_rel(p)] = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # An unreadable file is not a law violation, but staying silent
            # about it means the scan quietly covers less than it claims.
            print(f"warning: could not read {_rel(p)}: {exc}", file=sys.stderr)

    combined = "\n".join(sources.values())

    # If order submission exists anywhere, idempotency must appear somewhere
    has_submit = bool(re.search(r"submit_order|send_order|place_order", combined, re.IGNORECASE))
    has_idem = bool(re.search(r"idempotency_key|idempotency|idem_key", combined, re.IGNORECASE))
    if has_submit and not has_idem:
        findings.append(
            Finding(
                law="LAW3",
                check="Cross-file: order submission exists but no idempotency key found",
                passed=False,
                severity=Severity.CRITICAL,
                evidence="Add idempotency_key to all order submission paths (cross-file scan)",
            )
        )

    # PQC roadmap: if any new key infra present, PQC marker should appear
    has_key_gen = bool(
        re.search(r"generate_key|create_key|KeyPair|key_ceremony", combined, re.IGNORECASE)
    )
    has_pqc = bool(
        re.search(r"pqc_|ML[_-]KEM|ML[_-]DSA|post_quantum|pqc_roadmap", combined, re.IGNORECASE)
    )
    if has_key_gen and not has_pqc:
        findings.append(
            Finding(
                law="LAW12",
                check="Cross-file: key generation found but no PQC roadmap marker",
                passed=False,
                severity=Severity.MEDIUM,
                evidence=(
                    "Add pqc_roadmap reference or hybrid key logic near key generation; "
                    "see Law 12 and ecc-crypto.md"
                ),
            )
        )

    return findings


# ── Design-level checklist ────────────────────────────────────────────────────

DESIGN_CHECKS: dict[str, list[tuple[str, Severity]]] = {
    "LAW1": [
        ("VaR (95% and 99%) computed per position before order submission?", Severity.CRITICAL),
        ("CVaR/Expected Shortfall computed alongside VaR?", Severity.CRITICAL),
        ("Hard drawdown limit defined per strategy with circuit breaker?", Severity.CRITICAL),
        ("Kelly Criterion with fractional multiplier (0.25–0.5) enforced?", Severity.HIGH),
        ("Correlation matrix + HHI concentration check enforced?", Severity.HIGH),
        ("Liquidation cascade stress-test (incl. Dec 2024 Hyperliquid scenario)?", Severity.HIGH),
        ("Margin buffer ≥ 2× maintenance margin headroom enforced?", Severity.HIGH),
        ("Greeks tracked for options/structured positions; net delta capped?", Severity.HIGH),
    ],
    "LAW2": [
        ("Each strategy isolated in separate process or container?", Severity.HIGH),
        ("No shared mutable state between strategies?", Severity.HIGH),
        ("Strategy supervisor monitors and quarantines failed strategies?", Severity.HIGH),
        ("Strategy interface: signal/size/risk_check/on_fill/on_reject/on_halt?", Severity.MEDIUM),
        ("Back-pressure handling defined for feed lag?", Severity.MEDIUM),
        ("Resource quotas (CPU, memory, order rate, notional) enforced?", Severity.MEDIUM),
        ("Cross-strategy consolidated position view for portfolio risk?", Severity.HIGH),
    ],
    "LAW3": [
        (
            "Order path: Signal→Risk→Sizing→Validation→Exchange→Audit all present?",
            Severity.CRITICAL,
        ),
        ("Idempotency key on every order submission?", Severity.CRITICAL),
        (
            "Order state machine: PENDING/SUBMITTED/PARTIAL/FILLED/REJECTED/CANCELLED?",
            Severity.HIGH,
        ),
        ("Orphaned order reconciliation loop defined and scheduled?", Severity.HIGH),
        ("Fat-finger guard: reject if notional > N × recent average?", Severity.HIGH),
        ("OCO pairs placed on entry fill; partial fill triggers adjustment?", Severity.MEDIUM),
        ("Transaction decoded/displayed before signing (anti-blind-signing)?", Severity.CRITICAL),
    ],
    "LAW4": [
        ("Confidence threshold defined and enforced before execution?", Severity.CRITICAL),
        ("Ensemble disagreement above threshold suppresses trade?", Severity.HIGH),
        ("Signal staleness expiry enforced (never trade on stale signal)?", Severity.CRITICAL),
        ("Model drift detection triggers strategy suspension?", Severity.HIGH),
        ("Confidence calibration: ECE < 0.05 required?", Severity.HIGH),
        ("Conformal prediction coverage guarantee for sizing models?", Severity.MEDIUM),
        ("Regime mismatch gate: strategy only executes in compatible regime?", Severity.MEDIUM),
    ],
    "LAW5": [
        ("Rate limit budget tracked before hitting limit (not after)?", Severity.HIGH),
        ("Exponential backoff + jitter on retries (max 3 attempts)?", Severity.HIGH),
        ("WebSocket reconnect triggers state reconciliation via REST?", Severity.CRITICAL),
        ("Exchange downtime: system enters SAFE mode?", Severity.CRITICAL),
        ("Sequence number gaps trigger immediate reconciliation?", Severity.HIGH),
        ("Exchange health scoring: route to secondary on threshold breach?", Severity.MEDIUM),
    ],
    "LAW6": [
        ("Private keys in HSM or Vault (never in app config)?", Severity.CRITICAL),
        ("API secrets fetched from Vault at runtime (not baked in)?", Severity.CRITICAL),
        ("Key rotation schedule defined and tested?", Severity.HIGH),
        ("Emergency revocation path exists and tested quarterly?", Severity.HIGH),
        ("IP allowlist applied to all exchange API keys?", Severity.HIGH),
        ("MPC/threshold signing for wallets > 10% of capital?", Severity.HIGH),
        ("Transaction decoded/displayed before signing on ALL signing paths?", Severity.CRITICAL),
    ],
    "LAW7": [
        ("Structured logs: timestamp/component/event/payload/correlation_id?", Severity.HIGH),
        ("Metrics: latency p50/p95/p99, throughput, error rate, fill rate?", Severity.HIGH),
        ("Alerting on: drawdown breach, feed lag, auth failure, anomalous orders?", Severity.HIGH),
        ("Dead man's switch heartbeat defined?", Severity.HIGH),
        ("Distributed tracing: correlation_id propagated full order lifecycle?", Severity.MEDIUM),
        ("SLO error budgets defined per component?", Severity.MEDIUM),
        ("eBPF or equivalent kernel-level tracing for latency-critical components?", Severity.INFO),
    ],
    "LAW8": [
        ("Latency budget defined per strategy type?", Severity.HIGH),
        ("Per-stage latency histogram (p50/p95/p99) tracking?", Severity.HIGH),
        ("Latency regression defined as deployment blocker?", Severity.HIGH),
        ("Lock-free data structures in hot path (no mutex)?", Severity.HIGH),
        ("QUIC/HTTP3 evaluated for applicable exchange connections?", Severity.INFO),
    ],
    "LAW9": [
        ("Every production model has a model registry entry?", Severity.CRITICAL),
        ("Feature drift (PSI) monitored; PSI > 0.25 halts model?", Severity.CRITICAL),
        ("Prediction drift (KL divergence) monitored?", Severity.HIGH),
        ("Champion/challenger: shadow mode before promotion?", Severity.HIGH),
        ("SHAP values logged per prediction in audit trail?", Severity.HIGH),
        ("Model kill-switch tested?", Severity.CRITICAL),
        ("Automated retraining pipeline with OOS validation gate?", Severity.HIGH),
        ("LLM-generated signals: adversarial input validation + same drift gates?", Severity.HIGH),
        ("Conformal prediction coverage validated for sizing models?", Severity.MEDIUM),
    ],
    "LAW10": [
        ("Wash trade detection active pre-submission?", Severity.CRITICAL),
        ("Best execution benchmark logged per order?", Severity.HIGH),
        ("Trade reporting pipeline connected to regulatory repository?", Severity.HIGH),
        ("AML/sanctions screening on withdrawals (including hop analysis)?", Severity.CRITICAL),
        ("Position limits checked against regulatory thresholds?", Severity.HIGH),
        ("Audit trail retention: 7 years, encrypted, tamper-evident?", Severity.HIGH),
        ("MiCA CASP registration / asset whitelist enforced for EU ops?", Severity.HIGH),
        ("DORA ICT incident classification and reporting window defined?", Severity.HIGH),
        ("Travel Rule VASP verification for transfers > $3,000?", Severity.HIGH),
    ],
    "LAW11": [
        ("RTO defined per component; hot standby for order manager + risk engine?", Severity.HIGH),
        ("RPO defined: order state ≤ 5s, position state ≤ 1s?", Severity.HIGH),
        ("Runbooks for: exchange down, DB failure, vault unreachable, feed loss?", Severity.HIGH),
        ("Graceful shutdown: SIGTERM cancels orders, flushes audit log?", Severity.HIGH),
        ("Chaos engineering / game day scheduled quarterly?", Severity.MEDIUM),
        ("Multi-region replication for audit log and position state?", Severity.MEDIUM),
        ("Bybit-style UI spoofing vector included in threat model?", Severity.HIGH),
    ],
    "LAW12": [
        ("PQC migration roadmap artifact exists (inventory + phases + deadlines)?", Severity.HIGH),
        ("New key infrastructure uses hybrid classical+PQC scheme?", Severity.HIGH),
        ("HSM vendor PQC upgrade path verified (ML-KEM/ML-DSA firmware support)?", Severity.MEDIUM),
        ("Long-lived secrets (VPN, vault transport) prioritized in PQC migration?", Severity.HIGH),
        ("ML-KEM or X25519MLKEM768 evaluated for new transport key exchange?", Severity.MEDIUM),
        (
            "Deprecated hashes (MD5, SHA1) absent from all cryptographic paths?",
            Severity.CRITICAL,
        ),  # arch-ignore
        ("RSA keys ≥ 3072 bits wherever classical RSA still required?", Severity.HIGH),
    ],
    "LAW13": [
        ("Flash crash detection threshold defined and halt protocol in runbook?", Severity.HIGH),
        ("Spoofed order book layer detection in signal preprocessing?", Severity.MEDIUM),
        ("Momentum ignition suppression rule defined for thin-book conditions?", Severity.MEDIUM),
        ("Social engineering dual-authorization with 24h cooling period?", Severity.HIGH),
        ("Exchange-specific socialized loss / ADL scenario in risk model?", Severity.HIGH),
        ("Market halt protocol: flatten delta on remaining open venues?", Severity.HIGH),
        ("LLM/ML signal poisoning detection (confidence spike + anomalous OB)?", Severity.HIGH),
        ("DEX own-impact abort: transaction cancelled if impact > 2× expected?", Severity.MEDIUM),
    ],
}


def run_interactive_checks(laws: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for law in laws:
        if law not in DESIGN_CHECKS:
            print(f"Unknown law: {law}. Valid: {sorted(DESIGN_CHECKS.keys())}")
            continue
        print(f"\n── {law} Checks ──")
        for check, severity in DESIGN_CHECKS[law]:
            answer = input(f"  [{severity.value}] {check} [y/N]: ").strip().lower()
            passed = answer == "y"
            findings.append(
                Finding(
                    law=law,
                    check=check,
                    passed=passed,
                    severity=severity,
                    evidence="" if passed else "Not confirmed — resolve before proceeding",
                )
            )
    return findings


# ── Directory scan ────────────────────────────────────────────────────────────

SCAN_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".go", ".rs", ".mjs"}

# Vendored, generated and cache trees: never our architecture to govern, and
# node_modules alone would multiply scan time by orders of magnitude.
SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".git",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
}


def scan_directory(dirpath: Path) -> list[Finding]:
    findings: list[Finding] = []
    paths: list[Path] = []
    for path in sorted(dirpath.rglob("*")):
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix in SCAN_EXTENSIONS and path.is_file():
            paths.append(path)
            findings.extend(check_patterns(path))
            findings.extend(check_ast(path))
    findings.extend(cross_file_checks(paths))
    return findings


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crypto Architect Validation v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--component", required=True)
    parser.add_argument("--check", nargs="*", default=list(DESIGN_CHECKS.keys()))
    parser.add_argument("--file", type=Path)
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--sarif", action="store_true", help="Output SARIF v2.1 (GitHub Advanced Security)"
    )
    parser.add_argument("--output-file", type=Path, help="Write report to file instead of stdout")
    parser.add_argument(
        "--min-severity",
        default="MEDIUM",
        choices=[s.value for s in Severity],
    )
    parser.add_argument(
        "--fail-on",
        default="HIGH",
        choices=[s.value for s in Severity],
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="JSON file of accepted finding fingerprints; matches are suppressed",
    )
    parser.add_argument(
        "--write-baseline",
        type=Path,
        help="Write current failures as a baseline file and exit 0",
    )
    args = parser.parse_args()

    min_sev = Severity(args.min_severity)
    fail_sev = Severity(args.fail_on)

    report = ValidationReport(component=args.component)

    if args.file:
        if not args.file.exists():
            print(f"ERROR: File not found: {args.file}", file=sys.stderr)
            sys.exit(2)
        report.findings.extend(check_patterns(args.file))
        report.findings.extend(check_ast(args.file))

    if args.dir:
        if not args.dir.is_dir():
            print(f"ERROR: Not a directory: {args.dir}", file=sys.stderr)
            sys.exit(2)
        report.findings.extend(scan_directory(args.dir))

    if not args.non_interactive:
        report.findings.extend(run_interactive_checks(args.check))
    elif not args.file and not args.dir:
        print("ERROR: --non-interactive requires --file or --dir", file=sys.stderr)
        sys.exit(2)

    if args.write_baseline:
        args.write_baseline.write_text(
            json.dumps(
                {
                    "component": args.component,
                    "generated_by": "validate_arch.py v3.0",
                    "fingerprints": report.fingerprints(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"Baseline written: {args.write_baseline} "
            f"({len(report.fingerprints())} accepted findings)"
        )
        sys.exit(0)

    if args.baseline:
        if not args.baseline.exists():
            print(f"ERROR: Baseline not found: {args.baseline}", file=sys.stderr)
            sys.exit(2)
        known = set(json.loads(args.baseline.read_text(encoding="utf-8")).get("fingerprints", []))
        report.apply_baseline(known)

    if args.sarif:
        output = json.dumps(report.to_sarif(), indent=2)
    elif args.json:
        output = json.dumps(report.to_dict(fail_threshold=fail_sev), indent=2)
    else:
        import io

        buf = io.StringIO()
        _stdout = sys.stdout
        sys.stdout = buf
        report.print_text(min_severity=min_sev, fail_threshold=fail_sev)
        sys.stdout = _stdout
        output = buf.getvalue()

    if args.output_file:
        args.output_file.write_text(output, encoding="utf-8")
    else:
        print(output)

    sys.exit(0 if report.passed(fail_threshold=fail_sev) else 1)


if __name__ == "__main__":
    main()
