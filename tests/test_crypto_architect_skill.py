"""crypto-architect skill: description integrity, reference index, and the
validator's model-lifecycle checks, report contracts and exit codes.

Every validator case runs against a minimal module written to ``tmp_path``
and asserts the finding a reviewer or CI gate would act on, never the
presence of a keyword in the validator itself.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / ".claude" / "skills" / "crypto-architect"
SKILL_MD = SKILL_DIR / "SKILL.md"
VALIDATOR = SKILL_DIR / "scripts" / "validate_arch.py"

# SHA-256 of the exact `description:` key and its YAML scalar. The description
# is the skill's discovery contract; it changes only by deliberate decision.
DESCRIPTION_SHA256 = "4f77269c291258251b44f1de650bc48ca6a6a0c51e10b9a0382f24c1516676b1"


@pytest.fixture(scope="module")
def va():
    spec = importlib.util.spec_from_file_location("validate_arch", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_arch"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _failures(findings, check_substring: str):
    return [f for f in findings if not f.passed and check_substring in f.check]


def _scan(va, root: Path, rel: str, source: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return va.check_patterns(path) + va.check_ast(path)


@pytest.fixture()
def in_tmp(tmp_path, monkeypatch) -> Path:
    # Findings report repo-relative paths, and the scoped checks match on them.
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── Skill document ───────────────────────────────────────────────────────────


def test_description_block_is_byte_identical() -> None:
    frontmatter = SKILL_MD.read_bytes().split(b"---", 2)[1]
    block = re.search(rb"(?ms)^description:.*?(?=^\S|\Z)", frontmatter)
    assert block is not None
    assert hashlib.sha256(block.group(0)).hexdigest() == DESCRIPTION_SHA256


def test_reference_index_matches_reference_files(skill_text: str) -> None:
    indexed = set(re.findall(r"`(references/[\w-]+\.md)`", skill_text))
    on_disk = {f"references/{p.name}" for p in (SKILL_DIR / "references").glob("*.md")}
    assert len(on_disk) == 13
    assert indexed == on_disk


def test_every_repository_path_cited_by_the_skill_exists(skill_text: str) -> None:
    texts = [skill_text] + [
        p.read_text(encoding="utf-8") for p in (SKILL_DIR / "references").glob("*.md")
    ]
    cited = {
        m
        for text in texts
        for m in re.findall(
            r"`((?:src|config|scripts|tests|docs)/[\w./-]+\.(?:py|yaml|json|sh|md))`", text
        )
    }
    assert cited, "the skill no longer cites any repository path"
    # A `scripts/...` path may be the skill's own script, relative to the skill.
    missing = sorted(p for p in cited if not (REPO / p).exists() and not (SKILL_DIR / p).exists())
    assert not missing, f"skill cites paths that do not exist: {missing}"


def test_skill_name_and_validator_path_agree_with_the_gate(skill_text: str) -> None:
    gate = (REPO / "scripts" / "arch_gate.sh").read_text(encoding="utf-8")
    assert ".claude/skills/crypto-architect/scripts/validate_arch.py" in gate
    assert re.search(r"(?m)^name: Crypt-Chain-Engineer$", skill_text)
    assert "scripts/validate_arch.py" in skill_text


# ── Horizon/runtime separation ───────────────────────────────────────────────


def test_scheduling_config_must_not_load_prediction_horizons(va, in_tmp: Path) -> None:
    bad = _scan(va, in_tmp, "src/config.py", 'HORIZONS = open("config/horizons.yaml")\n')
    assert _failures(bad, "Execution scheduling config loads the prediction-horizon config")
    good = _scan(va, in_tmp, "src/config.py", "# horizons.yaml is loaded elsewhere\nX = 1\n")
    assert not _failures(good, "Execution scheduling config")


def test_horizon_loader_must_not_depend_on_execution_timeframes(va, in_tmp: Path) -> None:
    bad = _scan(va, in_tmp, "src/models/horizon_config.py", "from src.config import Timeframe\n")
    assert _failures(bad, "Prediction-horizon loader depends on execution-loop scheduling")


_LOADER = 'def load(s):\n    if set(s) - {"mlp"}:\n        raise ValueError("x")\n'


def _horizons(root: Path, models: str, *, second_id: int = 1) -> Path:
    (root / "src" / "models").mkdir(parents=True, exist_ok=True)
    (root / "src" / "models" / "horizon_config.py").write_text(_LOADER, encoding="utf-8")
    config = root / "config" / "horizons.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "horizons:\n"
        f'  h1:\n    id: 0\n    label: "30s"\n    seconds: 30\n    models: [{models}]\n'
        f'  h2:\n    id: {second_id}\n    label: "2m"\n    seconds: 120\n    models: [mlp]\n',
        encoding="utf-8",
    )
    return config


@pytest.mark.parametrize(
    ("models", "second_id", "ok"),
    [("mlp", 1, True), ("tft", 1, False), ("mlp", 0, False)],
    ids=["trainable", "declared-but-untrainable", "duplicate-id"],
)
def test_declared_horizon_models_must_be_trainable(va, in_tmp, models, second_id, ok) -> None:
    config = _horizons(in_tmp, models, second_id=second_id)
    findings = va.check_horizon_config(config, in_tmp / "src" / "models" / "horizon_config.py")
    (verdict,) = [f for f in findings if f.check.startswith("Declared horizons")]
    assert verdict.passed is ok
    assert verdict.severity == va.Severity.HIGH


def test_unverifiable_horizon_config_is_not_a_pass(va, in_tmp: Path) -> None:
    config = in_tmp / "horizons.yaml"
    config.write_text("horizons:\n  h1: [broken]\n", encoding="utf-8")
    findings = va.check_horizon_config(config, None)
    assert findings and not any(f.passed for f in findings)


# ── Matured labels, provenance, shadow/champion ──────────────────────────────

_LEARNING_OK = """
def mature(db, ts):
    db.execute("UPDATE s SET outcome=?, matured_at=? WHERE due_ts<=? AND outcome IS NULL")
def train(db):
    return db.execute("SELECT f FROM s WHERE outcome IS NOT NULL ORDER BY matured_at")
def evaluate(db):
    evaluation_id = db.execute("SELECT schema_hash, candidate_model_version, prediction_model_version, candidate_prediction_return")
    return "CONTINUE_SHADOW", evaluation_id
def rollback_promotion(db):
    return None
"""


def test_matured_label_safeguards_pass_when_in_code(va, in_tmp: Path) -> None:
    findings = _scan(va, in_tmp, "src/models/horizon_learning.py", _LEARNING_OK)
    lifecycle = [
        f for f in findings if f.law == "LAW9" and f.file == "src/models/horizon_learning.py"
    ]
    assert lifecycle and all(f.passed for f in lifecycle)


def test_labelling_before_maturity_is_reported(va, in_tmp: Path) -> None:
    leaking = _LEARNING_OK.replace("WHERE due_ts<=? AND outcome IS NULL", "WHERE outcome IS NULL")
    findings = _scan(va, in_tmp, "src/models/horizon_learning.py", leaking)
    assert _failures(findings, "Horizon labels mature before they are learned from")


def test_comment_mention_does_not_satisfy_a_safeguard(va, in_tmp: Path) -> None:
    commented = _LEARNING_OK.replace("WHERE due_ts<=? AND outcome IS NULL", "WHERE outcome IS NULL")
    commented = "# due_ts<= is checked somewhere\n" + commented
    findings = _scan(va, in_tmp, "src/models/horizon_learning.py", commented)
    assert _failures(findings, "Horizon labels mature before they are learned from")


def test_missing_provenance_is_reported(va, in_tmp: Path) -> None:
    findings = _scan(
        va, in_tmp, "src/models/horizon_learning.py", _LEARNING_OK.replace("schema_hash, ", "")
    )
    assert _failures(findings, "Candidate evaluation records model and feature-schema provenance")


def test_promotion_without_shadow_evaluation_is_reported(va, in_tmp: Path) -> None:
    bad = _scan(va, in_tmp, "src/x.py", "def act(r, m):\n    r.promote_shadow(m)\n")
    assert _failures(bad, "without evaluation evidence")
    good = _scan(
        va,
        in_tmp,
        "src/x.py",
        "def act(r, m):\n    ready, _ = r.evaluate_shadow(m)\n    if ready:\n        r.promote_shadow(m)\n",
    )
    assert not _failures(good, "without evaluation evidence")


def test_activation_without_rollback_is_reported(va, in_tmp: Path) -> None:
    findings = _scan(
        va,
        in_tmp,
        "src/x.py",
        "def act(s, c):\n    if s._evaluation_passes(c):\n        s.activate_candidate(c)\n",
    )
    assert _failures(findings, "without rollback path")


def test_permissive_promotion_default_is_reported(va, in_tmp: Path) -> None:
    findings = _scan(
        va, in_tmp, "src/x.py", "def deploy(model, auto_promote=True):\n    return model\n"
    )
    assert _failures(findings, "Permissive promotion default")


# ── Retraining recommendation vs verified control ────────────────────────────


def test_discarded_drift_result_is_reported(va, in_tmp: Path) -> None:
    bad = _scan(va, in_tmp, "src/x.py", "def tick(g, h, c):\n    g.check_drift(h, c)\n")
    assert _failures(bad, "Drift detection result discarded")
    good = _scan(
        va,
        in_tmp,
        "src/x.py",
        "def tick(g, h, c, s):\n    if g.check_drift(h, c):\n        s.suspend(h)\n",
    )
    assert not _failures(good, "Drift detection result discarded")


def test_constant_drift_status_written_to_storage_is_reported(va, in_tmp: Path) -> None:
    findings = _scan(
        va, in_tmp, "src/x.py", "def tick(db):\n    db.write_metric(drift_detected=False)\n"
    )
    assert _failures(findings, "Drift status persisted as a constant")


# ── Report contracts, exit codes, baseline ───────────────────────────────────


def _main(va, monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["validate_arch.py", "--component", "t", *argv])
    with pytest.raises(SystemExit) as exc:
        va.main()
    return int(exc.value.code)


def test_exit_code_follows_fail_on_threshold(va, in_tmp: Path, monkeypatch) -> None:
    src = in_tmp / "m.py"
    src.write_text("def f():\n    assert True\n", encoding="utf-8")  # MEDIUM only
    out = in_tmp / "r.json"
    args = ("--non-interactive", "--file", str(src), "--json", "--output-file", str(out))
    assert _main(va, monkeypatch, *args, "--fail-on", "MEDIUM") == 1
    assert json.loads(out.read_text())["passed"] is False
    assert _main(va, monkeypatch, *args, "--fail-on", "HIGH") == 0
    assert json.loads(out.read_text())["passed"] is True


def test_sarif_verdict_agrees_with_exit_code(va, in_tmp: Path, monkeypatch) -> None:
    src = in_tmp / "m.py"
    src.write_text("def f():\n    assert True\n", encoding="utf-8")
    out = in_tmp / "r.sarif"
    code = _main(
        va,
        monkeypatch,
        "--non-interactive",
        "--file",
        str(src),
        "--sarif",
        "--output-file",
        str(out),
        "--fail-on",
        "MEDIUM",
    )
    sarif = json.loads(out.read_text())
    assert sarif["version"] == "2.1.0"
    assert code == 1 and sarif["runs"][0]["properties"]["passed"] is False


def test_empty_scan_is_not_a_pass(va, in_tmp: Path, monkeypatch) -> None:
    empty = in_tmp / "empty"
    empty.mkdir()
    assert _main(va, monkeypatch, "--non-interactive", "--dir", str(empty)) == 1


def test_malformed_baseline_is_an_input_error(va, in_tmp: Path, monkeypatch) -> None:
    src = in_tmp / "m.py"
    src.write_text("X = 1\n", encoding="utf-8")
    baseline = in_tmp / "b.json"
    baseline.write_text("{not json", encoding="utf-8")
    assert (
        _main(va, monkeypatch, "--non-interactive", "--file", str(src), "--baseline", str(baseline))
        == 2
    )


def test_baseline_suppresses_known_findings_without_hiding_new_ones(va) -> None:
    known = va.Finding("LAW6", "known", False, va.Severity.CRITICAL, "e", file="a.py")
    new = va.Finding("LAW6", "new", False, va.Severity.CRITICAL, "e", file="a.py")
    report = va.ValidationReport("t", [known, new])
    report.apply_baseline({known.fingerprint})
    assert known.suppressed and not new.suppressed
    assert report.passed() is False
    report.findings.remove(new)
    assert report.passed() is True
