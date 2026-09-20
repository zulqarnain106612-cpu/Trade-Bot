#!/usr/bin/env python3
"""
One command for every quality gate that is cheap enough to run before pushing.

The gates themselves live in different places for good reasons -- the registry
loader is application code, the traceability check is a generator flag, the
supply-chain scan is a standalone script. What was missing is a single entry
point, because a developer who has to remember five commands runs three of
them, and the two they skip are the ones that would have caught something.

Everything here reads files. Nothing starts the bot, connects to a venue, or
runs the test suite: the full suite belongs in GitHub Actions, where the
machine that decides the pull request lives. These are seconds, and catching a
malformed registry before the push is the difference between one round trip
and three.

    qe_gate.py                       every gate
    qe_gate.py --only registry-schema
    qe_gate.py --plan plan.json      validate a change plan
    qe_gate.py --json                machine-readable, for the hook

Exit codes: 0 every blocking gate passed, 1 a blocking gate failed, 2 the
runner itself could not evaluate. The third is distinct on purpose -- a gate
that cannot run has not said yes, and folding that into "failed" hides a
broken checker behind a failing one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "qe.config.json"
PLAN_SCHEMA_PATH = SKILL_DIR / "schemas" / "change_plan.schema.json"
CONFIG_SCHEMA_PATH = SKILL_DIR / "schemas" / "qe_config.schema.json"

# .claude/skills/quality-engineering/scripts -> repository root
REPO = SKILL_DIR.parent.parent.parent

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}


class GateError(RuntimeError):
    """The runner could not evaluate a gate. Exit 2, never a silent pass."""


@dataclass
class Result:
    gate: str
    passed: bool
    blocking: bool
    detail: str = ""
    skipped: bool = False
    findings: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.skipped:
            return f"[skip] {self.gate}: {self.detail}"
        mark = "ok  " if self.passed else "FAIL"
        suffix = "" if self.blocking else " (advisory)"
        return f"[{mark}] {self.gate}{suffix}" + (f": {self.detail}" if self.detail else "")


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GateError(f"qe.config.json is unreadable: {exc}") from exc
    except ValueError as exc:
        raise GateError(f"qe.config.json is malformed: {exc}") from exc


def _validator(schema_path: Path):
    try:
        from jsonschema import Draft7Validator
    except ImportError as exc:  # pragma: no cover - environment gap
        raise GateError(
            "jsonschema is not installed; `pip install jsonschema` or run the gate in CI"
        ) from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _errors(validator, document: Any, limit: int = 8) -> list[str]:
    out: list[str] = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        where = ".".join(str(part) for part in error.path) or "<root>"
        rule = error.parent.schema.get("title") if error.parent else None
        prefix = f"{where}"
        if rule:
            prefix += f" [{rule}]"
        out.append(f"{prefix}: {error.message}")
        if len(out) >= limit:
            out.append("... further errors suppressed")
            break
    return out


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def gate_registry_schema(config: dict[str, Any]) -> Result:
    """The registry validates against its schema, conditional rules included."""
    registry_path = REPO / config["registry"]["path"]
    schema_path = REPO / config["registry"]["schema"]
    if not registry_path.exists():
        return Result("registry-schema", False, True, f"{registry_path} is missing")

    validator = _validator(schema_path)
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    findings = _errors(validator, document)
    return Result(
        "registry-schema",
        not findings,
        True,
        f"{len(document.get('entries', []))} entries"
        if not findings
        else f"{len(findings)} problem(s)",
        findings=findings,
    )


def gate_registry_loader(config: dict[str, Any]) -> Result:
    """
    What the schema cannot express: a claimed test that is not on disk, a
    dangling depends_on, a cycle.
    """
    loader = REPO / config["registry"]["loader"]
    if not loader.exists():
        return Result("registry-loader", True, True, "loader not present", skipped=True)

    code = (
        f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
        "from src.quality.registry import load_registry\n"
        "r = load_registry()\n"
        "print(len(r.entries))\n"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    if proc.returncode == 0:
        return Result("registry-loader", True, True, f"{proc.stdout.strip()} entries load")
    tail = [line for line in (proc.stderr or "").strip().splitlines() if line.strip()][-3:]
    return Result("registry-loader", False, True, "loader rejected the registry", findings=tail)


def gate_traceability(config: dict[str, Any]) -> Result:
    """The generated document matches the registry it is generated from."""
    problems: list[str] = []
    checked = 0
    for spec in config["registry"]["generated_docs"]:
        generator = REPO / spec["generator"]
        if not generator.exists():
            continue
        checked += 1
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(generator), spec["check_flag"]],
            capture_output=True,
            text=True,
            cwd=REPO,
            check=False,
        )
        if proc.returncode != 0:
            problems.append(f"{spec['doc']} is stale; run: python3 {spec['generator']}")
    if not checked:
        return Result("traceability", True, True, "no generators present", skipped=True)
    return Result("traceability", not problems, True, f"{checked} document(s)", findings=problems)


def gate_json_validity(config: dict[str, Any]) -> Result:
    """
    Every JSON file parses, and the ones with a schema validate against it.

    The parse half looks trivial and is the one that pays: a config with a
    trailing comma is found at startup in production otherwise.
    """
    problems: list[str] = []
    scanned = 0
    for path in sorted(REPO.rglob("*.json")):
        if set(path.parts) & SKIP_DIRS:
            continue
        scanned += 1
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.relative_to(REPO)}: {exc}")
        except OSError as exc:
            problems.append(f"{path.relative_to(REPO)}: unreadable: {exc}")

    # Schema'd pairs: <name>.json validated by <name>.schema.json beside it,
    # plus this skill's own config. A skill that enforces schemas on the
    # repository and ships an unvalidated config of its own is not making an
    # argument it believes.
    pairs: list[tuple[Path, Path]] = [(CONFIG_PATH, CONFIG_SCHEMA_PATH)]
    for schema_path in sorted((REPO / "config").glob("*.schema.json")):
        document_path = schema_path.with_name(schema_path.name.replace(".schema.json", ".json"))
        if document_path.exists():
            pairs.append((document_path, schema_path))

    for document_path, schema_path in pairs:
        try:
            validator = _validator(schema_path)
            document = json.loads(document_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            problems.append(f"{schema_path.relative_to(REPO)}: {exc}")
            continue
        for message in _errors(validator, document, limit=3):
            problems.append(f"{document_path.relative_to(REPO)}: {message}")

    return Result(
        "json-validity",
        not problems,
        True,
        f"{scanned} file(s), {len(pairs)} schema pair(s)",
        findings=problems,
    )


def gate_workflow_gates(config: dict[str, Any]) -> Result:
    """
    Every workflow ends in a gate job that needs every other job.

    Branch protection requires checks by name, and a job outside the gate's
    `needs` can fail while the required check stays green.
    """
    try:
        import yaml
    except ImportError:
        return Result("workflow-gates", True, True, "PyYAML not installed", skipped=True)

    workflow_dir = REPO / ".github" / "workflows"
    if not workflow_dir.exists():
        return Result("workflow-gates", True, True, "no workflows", skipped=True)

    problems: list[str] = []
    checked = 0
    for path in sorted(workflow_dir.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 - a malformed workflow is a finding
            problems.append(f"{path.name}: unparseable: {exc}")
            continue
        # The rule is about *required checks*, so it applies to workflows a
        # pull request can turn red. A scheduled or dispatch-only workflow
        # posts no check on a PR, and demanding a gate there would be a rule
        # that fires on correct configuration -- which is how a rule gets
        # switched off.
        triggers = doc.get("on", doc.get(True))
        if isinstance(triggers, str):
            trigger_names = {triggers}
        elif isinstance(triggers, list | dict):
            trigger_names = {str(t) for t in triggers}
        else:
            trigger_names = set()
        if not trigger_names & {"pull_request", "pull_request_target"}:
            continue

        jobs = doc.get("jobs") or {}
        if "gate" not in jobs:
            problems.append(f"{path.name}: runs on pull requests but has no gate job")
            continue
        checked += 1
        gate = jobs["gate"]
        needs = set(gate.get("needs") or [])
        others = set(jobs) - {"gate"}
        missing = others - needs
        if missing:
            problems.append(f"{path.name}: gate does not need {sorted(missing)}")
        if str(gate.get("if", "")).strip() != "always()":
            problems.append(f"{path.name}: gate is missing `if: always()`")
    return Result("workflow-gates", not problems, True, f"{checked} workflow(s)", findings=problems)


def gate_external(gate_spec: dict[str, Any]) -> Result:
    """A gate implemented by a repository script rather than by this runner."""
    parts = gate_spec["command"].split()
    script = None
    for part in parts:
        if part.endswith(".py"):
            script = REPO / part
            break
    if script is not None and not script.exists():
        if gate_spec.get("optional_if_missing"):
            return Result(
                gate_spec["id"],
                True,
                gate_spec["blocking"],
                f"{script.name} not present",
                skipped=True,
            )
        return Result(gate_spec["id"], False, gate_spec["blocking"], f"{script.name} is missing")

    proc = subprocess.run(  # noqa: S603 - argv from the validated config, no shell
        [sys.executable if part == "python3" else part for part in parts],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()][-4:]
    return Result(
        gate_spec["id"],
        proc.returncode == 0,
        gate_spec["blocking"],
        "" if proc.returncode == 0 else f"exit {proc.returncode}",
        findings=tail if proc.returncode != 0 else [],
    )


BUILTIN = {
    "registry-schema": gate_registry_schema,
    "registry-loader": gate_registry_loader,
    "traceability": gate_traceability,
    "json-validity": gate_json_validity,
    "workflow-gates": gate_workflow_gates,
}


def run_gates(config: dict[str, Any], only: str | None = None) -> list[Result]:
    results: list[Result] = []
    for spec in config["gates"]:
        if only and spec["id"] != only:
            continue
        handler = BUILTIN.get(spec["id"])
        results.append(handler(config) if handler else gate_external(spec))
    if only and not results:
        raise GateError(
            f"unknown gate {only!r}; known: {', '.join(g['id'] for g in config['gates'])}"
        )
    return results


def validate_plan(path: Path) -> list[str]:
    validator = _validator(PLAN_SCHEMA_PATH)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path}: {exc}"]
    return _errors(validator, document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="run one gate by id")
    parser.add_argument("--plan", help="validate a change plan against its schema")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--list", action="store_true", help="list the gates and exit")
    args = parser.parse_args(argv)

    try:
        config = load_config()

        if args.list:
            for spec in config["gates"]:
                flag = "blocking" if spec["blocking"] else "advisory"
                print(f"{spec['id']:<18} {flag:<9} {spec['title']}")
            return 0

        if args.plan:
            problems = validate_plan(Path(args.plan))
            if args.json:
                json.dump({"plan": args.plan, "problems": problems}, sys.stdout)
                sys.stdout.write("\n")
            elif problems:
                print(f"[FAIL] change plan: {len(problems)} problem(s)")
                for problem in problems:
                    print(f"         {problem}")
            else:
                print("[ok  ] change plan validates")
            return 1 if problems else 0

        results = run_gates(config, args.only)
    except GateError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    failed = [r for r in results if r.blocking and not r.passed and not r.skipped]

    if args.json:
        json.dump(
            {
                "passed": not failed,
                "results": [
                    {
                        "gate": r.gate,
                        "passed": r.passed,
                        "blocking": r.blocking,
                        "skipped": r.skipped,
                        "detail": r.detail,
                        "findings": r.findings,
                    }
                    for r in results
                ],
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 1 if failed else 0

    for result in results:
        print(result.line())
        for finding in result.findings:
            print(f"         {finding}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
