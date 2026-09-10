"""
The pq_transport stub must never read as a working KEM.

The roadmap calls a placeholder that looks like transport security the most
dangerous single artefact in the repository. These tests hold the line: every
operational method raises, the docstring says plainly it is not a KEM, and the
one honest thing it can do -- validate its declared parameters against FIPS 203
via the new NTT module -- actually works and actually catches drift.
"""

from __future__ import annotations

import pytest

from src.security.pq_transport import PQTransportStub


def test_none_of_the_kem_operations_return_a_value() -> None:
    """keygen/encapsulate/decapsulate raise rather than returning a fake secret."""
    stub = PQTransportStub()
    with pytest.raises((RuntimeError, NotImplementedError)):
        stub.keygen()
    with pytest.raises((RuntimeError, NotImplementedError)):
        stub.encapsulate(b"\x00" * 1184)
    with pytest.raises((RuntimeError, NotImplementedError)):
        stub.decapsulate(b"\x00" * 2400, b"\x00" * 1088)


def test_the_stub_is_not_available_by_default() -> None:
    assert PQTransportStub._AVAILABLE is False


def test_the_docstring_states_plainly_that_it_is_not_a_kem() -> None:
    """The exit-gate requirement, asserted against the actual docstring text."""
    doc = PQTransportStub.__doc__ or ""
    module_doc = __import__("src.security.pq_transport", fromlist=["__doc__"]).__doc__ or ""
    assert "NOT A WORKING KEM" in module_doc.upper()
    assert "inert" in doc.lower() or "not a kem" in doc.lower()


def test_declared_parameters_match_fips_203() -> None:
    """The one operation the stub can perform honestly."""
    PQTransportStub.validate_parameters()  # must not raise
    assert PQTransportStub.PARAMETERS == {"k": 3, "eta1": 2, "eta2": 2, "du": 10, "dv": 4}


def test_parameter_drift_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    If the declared parameters are edited to an under-width noise value, the
    FIPS 203 check fails -- so the stub cannot silently carry weakened
    constants into a future wiring.
    """
    monkeypatch.setattr(
        PQTransportStub, "PARAMETERS", {"k": 3, "eta1": 1, "eta2": 2, "du": 10, "dv": 4}
    )
    with pytest.raises(ValueError, match="narrows the noise distribution"):
        PQTransportStub.validate_parameters()


def test_when_marked_available_the_guard_passes_but_bodies_are_still_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Flipping _AVAILABLE past the availability guard does not conjure an
    implementation: keygen still raises NotImplementedError, so even a
    misconfigured deployment cannot get a fake secret out of the stub.
    """
    monkeypatch.setattr(PQTransportStub, "_AVAILABLE", True)
    stub = PQTransportStub()
    with pytest.raises(NotImplementedError):
        stub.keygen()
