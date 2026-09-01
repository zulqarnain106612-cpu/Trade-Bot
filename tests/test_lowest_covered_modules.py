"""The weakest-covered files in the tree, and the arms that made them weakest.

Grouped by module rather than by theme: each block drives the specific guard
the coverage report named, so the file stops being the one a per-file floor
would trip on first.
"""

from __future__ import annotations

import importlib
import threading
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# src/models/gru.py — the unconditioned initial hidden state
# ---------------------------------------------------------------------------


def test_the_gru_head_runs_without_a_regime_vector():
    """No regime means a zero initial state, not a crash."""
    torch = pytest.importorskip("torch")

    from src.models.gru import GRUHead

    head = GRUHead(input_size=4, hidden_size=8, num_layers=2, regime_dim=6, d_model=5)
    head.eval()
    with torch.no_grad():
        out = head(torch.randn(3, 7, 4))

    assert out.shape == (3, 5)
    assert torch.isfinite(out).all()


def test_a_regime_vector_changes_the_gru_output():
    torch = pytest.importorskip("torch")

    from src.models.gru import GRUHead

    head = GRUHead(input_size=4, hidden_size=8, num_layers=2, regime_dim=6, d_model=5)
    head.eval()
    x = torch.randn(3, 7, 4)
    with torch.no_grad():
        bare = head(x)
        conditioned = head(x, torch.ones(3, 6))

    assert not torch.allclose(bare, conditioned)


# ---------------------------------------------------------------------------
# src/security/pq_transport.py — the "liboqs is installed" arm
# ---------------------------------------------------------------------------


def test_the_kyber_stub_refuses_while_liboqs_is_absent():
    from src.security.pq_transport import PQTransportStub

    stub = PQTransportStub()
    with pytest.raises(RuntimeError, match="liboqs"):
        stub.encapsulate(b"")


def test_the_kyber_stub_reports_unimplemented_once_liboqs_is_present(monkeypatch):
    """With the flag flipped the guard passes and the real gap shows through."""
    from src.security.pq_transport import PQTransportStub

    monkeypatch.setattr(PQTransportStub, "_AVAILABLE", True)
    stub = PQTransportStub()
    for call in (
        lambda: stub.encapsulate(b"ek"),
        lambda: stub.decapsulate(b"dk", b"ct"),
    ):
        with pytest.raises(NotImplementedError):
            call()


# ---------------------------------------------------------------------------
# src/tuning/state.py — the Bayesian proposer, and the lock race
# ---------------------------------------------------------------------------


def test_the_bayesian_strategy_selects_the_bayesian_proposer():
    """proposer_strategy is read once at import, so the module is reloaded."""
    from src.config import get_settings
    from src.tuning.bayesian_proposer import BayesianProposer

    settings = get_settings()
    with patch.object(settings.self_tuning, "proposer_strategy", "bayesian"):
        state = importlib.reload(importlib.import_module("src.tuning.state"))
        try:
            assert isinstance(state.proposer, BayesianProposer)
        finally:
            importlib.reload(state)


def test_a_lock_created_by_another_thread_is_reused():
    """The double-checked guard must not replace a lock it just lost the race for."""
    from src.tuning.state import _PauseState

    flag = _PauseState()
    flag._lock = None
    winner = __import__("asyncio").Lock()

    class _RacingGuard:
        def __enter__(self):
            # a second thread got here first and published its lock
            flag._lock = winner
            return self

        def __exit__(self, *_exc):
            return False

    flag._init_guard = _RacingGuard()

    assert flag._get_lock() is winner


def test_the_lock_is_created_once_and_shared():
    from src.tuning.state import _PauseState

    flag = _PauseState()
    seen = []

    def _grab():
        seen.append(flag._get_lock())

    threads = [threading.Thread(target=_grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(lock) for lock in seen}) == 1


# ---------------------------------------------------------------------------
# src/ecc/ecdsa_scan.py — the DER scanner's rejection paths
# ---------------------------------------------------------------------------


def _der(r: bytes, s: bytes) -> bytes:
    body = b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(s)]) + s
    return b"\x30" + bytes([len(body)]) + body


def test_a_truncated_der_signature_is_rejected():
    """The length byte promises more than the buffer holds."""
    from src.ecc.ecdsa_scan import _parse_der_signature

    good = _der(b"\x11" * 4, b"\x22" * 4)
    assert _parse_der_signature(good) is not None

    # the r length byte promises four bytes the buffer does not have, so the
    # walk past it runs off the end
    truncated = b"\x30\x0c\x02\x08" + b"\x11" * 4 + b"\x02\x02\x22\x22"
    assert _parse_der_signature(truncated) is None


@pytest.mark.parametrize(
    "der",
    [
        b"\x30\x08\x03\x02\x11\x11\x02\x02\x22\x22",  # r is not an INTEGER
        b"\x30\x08\x02\x02\x11\x11\x03\x02\x22\x22",  # s is not an INTEGER
        b"\x31\x08\x02\x02\x11\x11\x02\x02\x22\x22",  # not a SEQUENCE
        b"\x30\x02\x02",  # too short to hold a signature at all
    ],
)
def test_malformed_der_signatures_are_rejected(der):
    from src.ecc.ecdsa_scan import _parse_der_signature

    assert _parse_der_signature(der) is None


def test_a_der_header_running_past_the_end_of_the_buffer_is_skipped():
    """0x30 with a length that overruns must not be read as a signature."""
    from src.ecc.ecdsa_scan import extract_ecdsa_signatures

    raw = b"\x30\x40" + b"\x00" * 12  # claims 0x40 bytes, holds 12
    assert extract_ecdsa_signatures(raw.hex()) == []


def test_a_valid_signature_with_no_pubkey_after_it_is_skipped():
    """Without the 33-byte compressed key there is nothing to attribute it to."""
    from src.ecc.ecdsa_scan import extract_ecdsa_signatures

    raw = _der(b"\x11" * 4, b"\x22" * 4) + b"\x01" + b"\x99" * 40
    assert extract_ecdsa_signatures(raw.hex()) == []


def test_repeated_sightings_of_one_r_stop_being_retained():
    """A single r recurring is the weakness; storing every later one adds nothing."""
    from src.ecc.ecdsa_scan import _MAX_SIGS_PER_R, ECDSAScanner

    scanner = ECDSAScanner()
    r = b"\x11" * 4
    for i in range(_MAX_SIGS_PER_R + 4):
        raw = _der(r, bytes([i + 1]) * 4) + b"\x01" + b"\x02" + b"\x77" * 32
        scanner.scan_transaction(raw.hex())

    assert len(scanner._r_registry) == 1
    (entry,) = scanner._r_registry.values()
    assert len(entry) == _MAX_SIGS_PER_R


# ---------------------------------------------------------------------------
# src/ecc/schnorr_taproot.py — a witness that is neither key-path nor script-path
# ---------------------------------------------------------------------------


def test_a_taproot_input_with_an_unusable_witness_counts_as_neither_path():
    """A single witness item of the wrong length is not a key-path spend."""
    from src.ecc.schnorr_taproot import parse_taproot_block

    p2tr_hex = "5120" + "aa" * 32
    txs = [
        {
            "vin": [
                {
                    "txinwitness": ["ff" * 10],  # one item, not a 64-byte signature
                    "prevout": {"scriptPubKey": {"hex": p2tr_hex}},
                }
            ]
        }
    ]

    info = parse_taproot_block(txs)

    # counted as a Taproot input, but attributed to neither spend path
    assert info.key_path_spends == 0
    assert info.script_path_spends == 0
