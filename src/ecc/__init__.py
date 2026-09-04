"""ECC analysis layer -- secp256k1, ECDSA, Schnorr/Taproot, UTXO, zkSNARK.

post_quantum posture (LAW12): read-only chain analysis. No module in this
package holds a key, signs, or establishes a shared secret, so no ML-KEM or
ML-DSA migration applies to any of them. See each module for what a CRQC
does to the signal it produces.
"""
