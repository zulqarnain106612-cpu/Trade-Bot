"""
Quantum-safe transport stub — Kyber-768 / ML-KEM (Part II §9.5).

NOT for immediate deployment — infrastructure stub + documentation.

Quantum threat context:
  - ECDH/X25519 broken by Shor's algorithm on a Cryptographically Relevant QC (CRQC)
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
    Stub for Kyber-768 key encapsulation.

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

    @staticmethod
    def is_quantum_threat_imminent() -> bool:
        """
        Heuristic: returns True if env flag PQ_THREAT_ACTIVE is set.

        Operators set this flag to trigger migration when CRQC timeline
        compresses. Checked by the orchestrator to warn via alerts.
        """
        return os.environ.get("PQ_THREAT_ACTIVE", "").lower() in ("1", "true")
