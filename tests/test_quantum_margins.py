"""
Tests for :mod:`src.mathcore.quantum` -- Grover margins and Shor migration.

These two entries are ``attack_surface``: the value is an honest assessment,
so the tests are mostly about what the modules refuse to claim.

**Grover does not simply halve security, and the return value must say so.**
The halving is a query-count bound assuming one coherent machine running
2**(b/2) *successive* error-corrected operations. Grover parallelises badly --
M machines buy sqrt(M) where classical brute force buys M -- so the bound is
conservative rather than predictive, and NIST IR 8547 does not treat AES-128 as
broken. A module reporting only `effective_bits` would be quotable as either
alarmism or complacency with no way for a reader to tell which they had.

**No resource estimate is fabricated.** Logical qubit counts and Toffoli depths
are model-dependent published figures, and transcribing one approximately from
memory is the defect class this repo has shipped three times -- with the extra
property that nobody can check it by running anything. A test asserts no such
number appears.

**The date is never a module constant.** The year a capable machine arrives is
the assumption every migration answer turns on, so it is a required parameter
with no default. A default would let a caller get a verdict without deciding
what they believe.

The arithmetic that *is* here -- Mosca's X + Y > Z -- is checked at the
boundary in both directions, because "already exposed" and "not yet compliant"
are different statements and the inequality is where they separate.
"""

from __future__ import annotations

import inspect
import re

import pytest

from src.mathcore.quantum import grover, shor
from src.mathcore.quantum.grover import (
    GroverAssessment,
    QuantumAssessmentError,
    assess_suite,
    assess_symmetric,
    grover_effective_bits,
    grover_query_cost_bits,
    meets_post_quantum_target,
    weakest_link,
)
from src.mathcore.quantum.shor import (
    SchemeFamily,
    classify_scheme,
    exposed_schemes,
    is_broken_by_shor,
    migration_verdict,
    requires_migration,
)


class TestGroverArithmetic:
    @pytest.mark.parametrize(
        "classical,expected",
        [(128, 64.0), (192, 96.0), (256, 128.0), (512, 256.0), (80, 40.0), (1, 0.5)],
    )
    def test_the_quadratic_speedup_is_a_halving_of_bits(
        self, classical: int, expected: float
    ) -> None:
        assert grover_query_cost_bits(classical) == expected
        assert grover_effective_bits(classical) == expected

    @pytest.mark.parametrize("bad", [0, -1, -256])
    def test_a_non_positive_size_is_refused(self, bad: int) -> None:
        """Not a weak configuration -- a malformed question."""
        with pytest.raises(QuantumAssessmentError, match="positive"):
            grover_query_cost_bits(bad)

    @pytest.mark.parametrize("bad", [128.0, "128", None, True])
    def test_a_non_integer_size_is_refused(self, bad: object) -> None:
        """
        True is included on purpose: bool is a subclass of int, so a naive
        check accepts it as the key size 1.
        """
        with pytest.raises(QuantumAssessmentError, match="integer"):
            grover_query_cost_bits(bad)  # type: ignore[arg-type]


class TestTheHalvingCarriesItsCaveat:
    """
    The honesty requirement, enforced through the return type.

    `effective_bits` alone is quotable as "AES-128 is broken", which NIST does
    not say. The depth figure is what makes the bound conservative, so it
    travels with the number rather than living in prose a caller can skip.
    """

    def test_the_assessment_reports_the_sequential_depth(self) -> None:
        assessment = assess_symmetric(128)
        assert assessment.sequential_depth_bits == 64.0
        assert assessment.query_cost_bits == 64.0
        assert assessment.effective_bits == 64.0

    def test_the_depth_field_exists_and_is_not_optional(self) -> None:
        """
        Structural: a future commit trimming the dataclass to just
        effective_bits would remove the caveat and pass every arithmetic test
        in this file.
        """
        fields = GroverAssessment.__dataclass_fields__
        assert "sequential_depth_bits" in fields
        assert "query_cost_bits" in fields

    def test_the_docstrings_state_the_parallelisation_penalty(self) -> None:
        """
        The reason the bound is conservative is the reason a reader needs. If
        it is not written down, the next person to touch this reports the
        halving as a prediction.
        """
        source = inspect.getsource(grover)
        assert "parallelis" in source
        assert "sqrt(M)" in source or "sqrt(N)" in source
        assert "8547" in source, "the standard that declines to treat AES-128 as broken"

    def test_aes_128_misses_a_128_bit_target_and_aes_256_meets_it(self) -> None:
        """The concrete claim the registry entry makes about the recommendation."""
        assert not meets_post_quantum_target(128)
        assert meets_post_quantum_target(256)
        assert assess_symmetric(128).shortfall_bits == 64.0
        assert assess_symmetric(256).shortfall_bits == 0.0

    def test_the_target_is_the_callers_to_set(self) -> None:
        """This module checks a policy; it does not decide one."""
        assert meets_post_quantum_target(128, target_bits=64)
        assert not meets_post_quantum_target(128, target_bits=65)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_target_is_refused(self, bad: int) -> None:
        with pytest.raises(QuantumAssessmentError, match="target_bits"):
            assess_symmetric(128, bad)


class TestSuiteAssessment:
    def test_a_suite_is_assessed_in_one_call(self) -> None:
        results = assess_suite({"aes-gcm": 256, "hmac-sha256": 256, "legacy-aes": 128})
        assert set(results) == {"aes-gcm", "hmac-sha256", "legacy-aes"}
        assert results["legacy-aes"].meets_target is False
        assert results["aes-gcm"].meets_target is True

    def test_the_weakest_link_is_the_answer_the_risk_asks_for(self) -> None:
        """
        The entry's risk is "migrating asymmetric primitives while leaving
        128-bit symmetric keys in place leaves the weakest link untouched" --
        a property of the suite, invisible while each primitive is examined
        alone.
        """
        name, assessment = weakest_link({"aes": 256, "session-hmac": 128, "sha512": 512})
        assert name == "session-hmac"
        assert assessment.effective_bits == 64.0
        assert not assessment.meets_target

    def test_the_weakest_link_is_deterministic_on_a_tie(self) -> None:
        """Otherwise the answer depends on dict ordering and changes silently."""
        for _ in range(3):
            assert weakest_link({"b": 128, "a": 128, "c": 256})[0] == "a"

    def test_an_empty_suite_is_refused(self) -> None:
        with pytest.raises(QuantumAssessmentError, match="nothing to assess"):
            assess_suite({})
        with pytest.raises(QuantumAssessmentError, match="nothing to assess"):
            weakest_link({})


class TestSchemeClassification:
    @pytest.mark.parametrize(
        "name,family",
        [
            ("rsa", SchemeFamily.FACTORING),
            ("RSA-PSS", SchemeFamily.FACTORING),
            ("ecdsa", SchemeFamily.ELLIPTIC_CURVE_DLP),
            ("secp256k1", SchemeFamily.ELLIPTIC_CURVE_DLP),
            ("ed25519", SchemeFamily.ELLIPTIC_CURVE_DLP),
            ("bls12-381", SchemeFamily.ELLIPTIC_CURVE_DLP),
            ("ffdh", SchemeFamily.FINITE_FIELD_DLP),
            ("dsa", SchemeFamily.FINITE_FIELD_DLP),
            ("aes-gcm", SchemeFamily.SYMMETRIC),
            ("sha256", SchemeFamily.HASH),
            ("ml-kem", SchemeFamily.LATTICE),
            ("dilithium", SchemeFamily.LATTICE),
            ("classic-mceliece", SchemeFamily.CODE_BASED),
            ("slh-dsa", SchemeFamily.HASH_BASED_SIGNATURE),
        ],
    )
    def test_named_schemes_map_to_their_hard_problem(self, name: str, family: SchemeFamily) -> None:
        assert classify_scheme(name) == family

    @pytest.mark.parametrize(
        "spelling", ["ML-KEM", "ml_kem", "mlkem", "  ml-kem  ", "Ml-Kem", "ML KEM"]
    )
    def test_spelling_variants_resolve_to_the_same_family(self, spelling: str) -> None:
        """
        The same primitive is spelled differently across specifications and
        config files. A migration audit that missed one because of a hyphen
        would report a clean suite -- which is the failure, not a cosmetic bug.
        """
        assert classify_scheme(spelling) == SchemeFamily.LATTICE

    def test_an_unknown_name_is_unknown_not_guessed(self) -> None:
        assert classify_scheme("frobnicate-512") == SchemeFamily.UNKNOWN

    @pytest.mark.parametrize("bad", ["", "   ", None, 7])
    def test_a_missing_name_is_refused(self, bad: object) -> None:
        with pytest.raises(QuantumAssessmentError, match="scheme name"):
            classify_scheme(bad)  # type: ignore[arg-type]


class TestWhatShorBreaks:
    @pytest.mark.parametrize(
        "name",
        [
            "rsa",
            "rsa-oaep",
            "dsa",
            "ffdh",
            "elgamal",
            "ecdh",
            "ecdsa",
            "ed25519",
            "secp256k1",
            "schnorr",
            "x25519",
            "bls12-381",
        ],
    )
    def test_every_deployed_asymmetric_primitive_falls(self, name: str) -> None:
        assert is_broken_by_shor(name)

    @pytest.mark.parametrize(
        "name",
        [
            "aes",
            "aes-gcm",
            "chacha20-poly1305",
            "sha256",
            "sha3-256",
            "blake2b",
            "ml-kem",
            "kyber",
            "ml-dsa",
            "falcon",
            "classic-mceliece",
            "slh-dsa",
        ],
    )
    def test_symmetric_hash_and_post_quantum_schemes_do_not(self, name: str) -> None:
        assert not is_broken_by_shor(name)

    def test_an_unknown_scheme_is_treated_as_broken(self) -> None:
        """
        The safe direction. An unclassified name is more likely a deployed
        asymmetric primitive somebody forgot than a post-quantum scheme nobody
        added to the table, so an audit surfaces it rather than passing it.
        """
        assert is_broken_by_shor("some-internal-handshake-v2")

    def test_key_size_does_not_change_the_answer(self) -> None:
        """
        Shor's cost is polynomial in the key length, so a bigger key buys a
        constant factor and not a margin. Nothing in the API takes a size,
        which is how that is expressed.
        """
        signature = inspect.signature(is_broken_by_shor)
        assert list(signature.parameters) == ["name"]


class TestMoscaInequality:
    """
    X + Y > Z, checked at the boundary in both directions.

    "Already exposed" and "not yet compliant" are different statements, and the
    inequality is exactly where they separate -- so an off-by-one in the
    comparison changes an urgent finding into a comfortable one.
    """

    def test_exposed_when_lifetime_plus_migration_exceeds_the_date(self) -> None:
        verdict = migration_verdict(
            "ecdsa",
            confidentiality_years=10,
            migration_years=5,
            years_until_capable_machine=12,
        )
        assert verdict.exposed
        assert verdict.years_of_exposure == 3.0
        assert "already-exposed" in verdict.reason

    def test_not_exposed_with_headroom(self) -> None:
        verdict = migration_verdict(
            "ecdsa",
            confidentiality_years=2,
            migration_years=3,
            years_until_capable_machine=12,
        )
        assert not verdict.exposed
        assert verdict.years_of_exposure == 0.0
        assert "headroom" in verdict.reason

    @pytest.mark.parametrize(
        "x,y,z,exposed",
        [
            (5, 5, 10, False),  # X + Y == Z exactly: not exposed
            (5, 5, 9.999, True),  # a hair over
            (5, 5, 10.001, False),  # a hair under
            (0, 0, 0, False),  # degenerate but well-formed
            (1, 0, 0, True),
        ],
    )
    def test_the_boundary_is_strict_inequality(
        self, x: float, y: float, z: float, exposed: bool
    ) -> None:
        assert (
            requires_migration(
                "rsa",
                confidentiality_years=x,
                migration_years=y,
                years_until_capable_machine=z,
            )
            is exposed
        )

    def test_a_scheme_shor_does_not_break_is_never_exposed_this_way(self) -> None:
        """
        The arithmetic is only meaningful for a primitive that is going to
        fall. Applying it to AES would produce an alarming number about a
        mechanism that does not threaten AES.
        """
        verdict = migration_verdict(
            "aes-gcm",
            confidentiality_years=100,
            migration_years=100,
            years_until_capable_machine=1,
        )
        assert not verdict.exposed
        assert verdict.years_of_exposure == 0.0
        assert "does not break" in verdict.reason
        assert "grover" in verdict.reason.lower(), "and it should say where to look instead"

    def test_the_verdict_records_its_inputs(self) -> None:
        """
        An assessment whose assumptions are not attached to it cannot be
        re-examined when the estimate of Z moves.
        """
        verdict = migration_verdict(
            "rsa",
            confidentiality_years=7,
            migration_years=2,
            years_until_capable_machine=15,
        )
        assert verdict.confidentiality_years == 7.0
        assert verdict.migration_years == 2.0
        assert verdict.years_until_capable_machine == 15.0
        assert verdict.family is SchemeFamily.FACTORING

    @pytest.mark.parametrize(
        "field", ["confidentiality_years", "migration_years", "years_until_capable_machine"]
    )
    def test_a_negative_duration_is_refused(self, field: str) -> None:
        kwargs = {
            "confidentiality_years": 1,
            "migration_years": 1,
            "years_until_capable_machine": 1,
        }
        kwargs[field] = -1
        with pytest.raises(QuantumAssessmentError, match="negative"):
            migration_verdict("rsa", **kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field", ["confidentiality_years", "migration_years", "years_until_capable_machine"]
    )
    def test_a_non_numeric_duration_is_refused(self, field: str) -> None:
        kwargs = {
            "confidentiality_years": 1,
            "migration_years": 1,
            "years_until_capable_machine": 1,
        }
        kwargs[field] = "soon"
        with pytest.raises(QuantumAssessmentError, match="must be a number"):
            migration_verdict("rsa", **kwargs)  # type: ignore[arg-type]

    def test_a_boolean_duration_is_refused(self) -> None:
        """bool passes isinstance(x, int); True would silently mean one year."""
        with pytest.raises(QuantumAssessmentError, match="must be a number"):
            migration_verdict(
                "rsa",
                confidentiality_years=True,  # type: ignore[arg-type]
                migration_years=1,
                years_until_capable_machine=1,
            )


class TestTheDateIsNeverAssumedForYou:
    def test_years_until_capable_machine_has_no_default(self) -> None:
        """
        The assumption the entire answer turns on. A default would let a caller
        obtain a verdict without ever deciding what they believe, and there is
        no consensus value to supply.
        """
        for func in (migration_verdict, requires_migration):
            parameter = inspect.signature(func).parameters["years_until_capable_machine"]
            assert parameter.default is inspect.Parameter.empty, func.__name__
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_no_year_constant_is_baked_into_the_module(self) -> None:
        """
        A module-level 2030 or 2035 would be an assumption dressed as a
        library value, which is how it stops being questioned.
        """
        for name, value in vars(shor).items():
            if name.startswith("_") or not isinstance(value, (int, float)):
                continue
            assert not (1990 <= value <= 2200), f"{name}={value} looks like a hardcoded year"


class TestNoFabricatedResourceEstimates:
    """
    The constraint that kept this module small.

    Logical qubit counts and Toffoli depths are model-dependent published
    figures. Transcribing one approximately from memory is the defect class
    this repo has shipped three times, and here nobody could check it by
    running anything -- there is no machine and a simulator says nothing about
    a 256-bit curve. So the numbers are absent, and their absence is asserted.
    """

    @staticmethod
    def _code_only(module: object) -> str:
        """
        Source with the module docstring removed.

        The docstring *names* these quantities in order to explain why they are
        absent, which is the documentation working. Scanning it flagged this
        module's own justification -- the same shape as a governance rule
        matching the prose that describes it. The check belongs on executable
        code.
        """
        source = inspect.getsource(module)  # type: ignore[arg-type]
        doc = getattr(module, "__doc__", None)
        return source.replace(doc, "") if doc else source

    def test_no_qubit_or_gate_count_is_claimed(self) -> None:
        code = self._code_only(shor).lower()
        forbidden = (
            "logical_qubits",
            "physical_qubits",
            "toffoli",
            "gate_count",
            "circuit_depth",
            "qubit_count",
        )
        for token in forbidden:
            assert token not in code, (
                f"{token!r} suggests a transcribed resource estimate; these are "
                "model-dependent published figures and cannot be checked here"
            )

    def test_the_module_says_why_the_numbers_are_absent(self) -> None:
        assert "fabricated constant" in shor.__doc__

    def test_no_large_bare_numeric_literal_sits_in_the_source(self) -> None:
        """
        A resource estimate would arrive as a big literal. Reference years in
        comments are fine; a 4-or-more-digit number in executable code is the
        shape to catch.
        """
        source = inspect.getsource(shor)
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        # Strip the docstring, where citations legitimately carry years.
        code = code.split('"""', 2)[-1]
        suspicious = [n for n in re.findall(r"\b\d{4,}\b", code) if n not in {"25519", "12381"}]
        assert not suspicious, f"unexplained large literals: {suspicious}"

    def test_neither_module_implements_a_quantum_algorithm(self) -> None:
        """
        Scope. L1 of the architecture laws is verification only; these modules
        assess, and a simulator here would be a claim about 256-bit security
        that a 30-qubit simulation cannot make.
        """
        for module in (grover, shor):
            source = inspect.getsource(module)
            for token in ("def simulate", "def run_grover", "def run_shor", "qiskit", "cirq"):
                assert token not in source, f"{token} in {module.__name__}"


class TestExposedSchemesReport:
    def test_it_ranks_worst_first(self) -> None:
        """A migration plan needs a first item."""
        verdicts = exposed_schemes(
            {"ecdsa": 10, "ecdh": 1, "rsa": 20},
            migration_years=3,
            years_until_capable_machine=12,
        )
        assert [v.scheme for v in verdicts] == ["rsa", "ecdsa"]
        assert verdicts[0].years_of_exposure == 11.0

    def test_unexposed_schemes_are_omitted(self) -> None:
        verdicts = exposed_schemes(
            {"ecdh": 1, "aes-gcm": 50},
            migration_years=1,
            years_until_capable_machine=30,
        )
        assert verdicts == []

    def test_the_lifetime_belongs_to_the_secret_not_the_scheme(self) -> None:
        """
        The same ECDSA protects a session that matters for minutes and a
        custody key that matters for a decade, so the lifetime is per entry.
        """
        verdicts = exposed_schemes(
            {"ecdsa": 30, "ecdh": 0.1},
            migration_years=2,
            years_until_capable_machine=10,
        )
        assert [v.scheme for v in verdicts] == ["ecdsa"]

    def test_ties_break_deterministically(self) -> None:
        verdicts = exposed_schemes(
            {"rsa": 10, "ecdsa": 10, "dsa": 10},
            migration_years=5,
            years_until_capable_machine=12,
        )
        assert [v.scheme for v in verdicts] == ["dsa", "ecdsa", "rsa"]

    def test_an_empty_suite_is_refused(self) -> None:
        with pytest.raises(QuantumAssessmentError, match="nothing to assess"):
            exposed_schemes({}, migration_years=1, years_until_capable_machine=1)
