"""
Quantum-safe transport stub — Kyber-768 / ML-KEM (Part II §9.5).

⚠️  THIS IS NOT A WORKING KEM. It performs no key encapsulation, derives no
shared secret, and provides no confidentiality against any adversary, quantum
or classical. Every operational method raises rather than returning a value,
so it cannot be mistaken at runtime for transport security — calling it fails
loudly instead of returning a fake secret. It exists to document the migration
path and to pin the FIPS 203 parameters (see ``validate_parameters``), nothing
more. Do not wire it into a handshake until its body is replaced by a vetted
ML-KEM implementation (liboqs-python or equivalent).

NOT for immediate deployment — infrastructure stub + documentation.

Quantum threat context:
  - Classical elliptic-curve key exchange (X25519) is broken by Shor's algorithm
    on a Cryptographically Relevant Quantum Computer (CRQC)
  - HNDL risk: attacker stores Trade-Bot API traffic today, decrypts when CRQC exists
  - Consensus timeline for CRQC: ~2030-2040 (NIST PQC round 3 / FIPS 203)

Kyber-768 parameters:
  n=256, k=3, q=3329
  Security: IND-CCA2 under Module-LWE in QROM
  Key sizes: ek=1184 bytes, dk=2400 bytes, ct=1088 bytes, ss=32 bytes

Migration path for Trade-Bot:
  1. Deploy this stub (no behavioral change, only documents intent)
  2. Add kyber-py or liboqs-python when CRQC risk becomes imminent
  3. Run hybrid X25519+Kyber-768 for 2 years (transition period)
  4. Drop X25519 when hybrid confidence is established

This file is intentionally inert — it documents the integration path
without adding a new runtime dependency before it is needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class KyberKeyPair:
    """Placeholder for a Kyber-768 key pair."""

    encapsulation_key: bytes  # ek, 1184 bytes
    decapsulation_key: bytes  # dk, 2400 bytes


@dataclass
class KyberCiphertext:
    ciphertext: bytes  # ct, 1088 bytes
    shared_secret: bytes  # ss, 32 bytes


class PQTransportStub:
    """
    Stub for Kyber-768 key encapsulation — inert by construction.

    Not a KEM: keygen/encapsulate/decapsulate all raise. The one thing it can
    honestly do is confirm its declared parameters are the real FIPS 203
    ML-KEM-768 set, via :meth:`validate_parameters`, so a future wiring starts
    from checked constants rather than copied ones.

    When liboqs-python is available, replace the body of each method
    with oqs.KeyEncapsulation("Kyber768") calls.
    """

    _AVAILABLE = False  # flip to True when liboqs is installed

    def keygen(self) -> KyberKeyPair:
        self._assert_available()
        raise NotImplementedError

    def encapsulate(self, encapsulation_key: bytes) -> KyberCiphertext:
        self._assert_available()
        raise NotImplementedError

    def decapsulate(self, decapsulation_key: bytes, ciphertext: bytes) -> bytes:
        self._assert_available()
        raise NotImplementedError

    def _assert_available(self) -> None:
        if not self._AVAILABLE:
            raise RuntimeError(
                "Kyber-768 not yet wired: install liboqs-python and set "
                "PQTransportStub._AVAILABLE = True. See src/security/pq_transport.py."
            )

    # The declared Kyber-768 / ML-KEM-768 parameters, kept here so the stub and
    # any eventual implementation agree on constants.
    PARAMETERS = {"k": 3, "eta1": 2, "eta2": 2, "du": 10, "dv": 4}

    @classmethod
    def validate_parameters(cls) -> None:
        """
        Confirm this stub's declared parameters are the real FIPS 203 set.

        The one operation the stub can perform honestly: it cannot encapsulate,
        but it can prove the constants it would use are ML-KEM-768's, by
        checking them through :func:`src.mathcore.fields.ntt.validate_kem_parameters`.
        Raises if they have drifted. This is deliberately not a substitute for
        the real thing -- validated parameters on an inert stub still encrypt
        nothing -- but it means a future wiring starts from checked constants.
        """
        from src.mathcore.fields.ntt import validate_kem_parameters

        validate_kem_parameters("ML-KEM-768", cls.PARAMETERS)

    @staticmethod
    def is_quantum_threat_imminent() -> bool:
        """
        Heuristic: returns True if env flag PQ_THREAT_ACTIVE is set.

        Operators set this flag to trigger migration when CRQC timeline
        compresses. Checked by the orchestrator to warn via alerts.
        """
        return os.environ.get("PQ_THREAT_ACTIVE", "").lower() in ("1", "true")
