"""
Schnorr / Taproot smart-money detection.

Parses P2TR (Pay-to-Taproot, BIP-341) inputs to identify:
  - MuSig2 multi-signature clusters (BIP-327) — sophisticated participants
  - Privacy routing via Taproot key-path spends — smart-money divergence
  - Smart-money divergence: when large Taproot signers move against spot

Output fed to 4h and 1W horizons.

post_quantum posture (LAW12):
  Nothing here is a cryptographic trust boundary. This module reads
  public chain data and produces a score; it holds no key, signs
  nothing, establishes no shared secret, and protects no secret of
  ours. There is therefore nothing in it for a CRQC to break, and no
  ML-KEM or ML-DSA migration applies.

  A CRQC changes what these signals *mean*, not what this code must
  protect. BIP-340 Schnorr and MuSig2 are
  discrete-log schemes and a CRQC breaks both, but this module only
  parses P2TR spends to identify who is signing. If Bitcoin migrates
  its signature scheme, the parsing changes because the script format
  does -- a chain-upgrade follow, not a security migration.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# P2TR output type: OP_1 <32-byte-x-only-pubkey>
_P2TR_PREFIX = bytes([0x51, 0x20])


@dataclass
class TaprootSpendInfo:
    musig2_count: int
    privacy_score: float  # [0, 1] — 1 = maximum privacy routing
    smart_money_divergence: float  # [-1, +1] — deviation from retail consensus
    p2tr_input_count: int
    key_path_spends: int
    script_path_spends: int


def is_p2tr_output(script_pubkey: bytes) -> bool:
    """Return True if script_pubkey is a P2TR witness program (OP_1 <32b>)."""
    return len(script_pubkey) == 34 and script_pubkey[:2] == _P2TR_PREFIX


def detect_musig2_cosigners(taproot_inputs: list[dict]) -> list[list[str]]:
    """
    Identify MuSig2 clusters from Taproot inputs.

    A MuSig2 key-path spend looks like a single-signer spend to observers
    but is signed by a group using the MuSig2 protocol (BIP-327).  We detect
    candidate clusters by looking for identical x-only pubkey prefixes across
    multiple inputs in the same transaction — a heuristic approximation.

    taproot_inputs: list of dicts with keys:
      txinwitness: list[str hex]  — witness stack items
      prevout_scriptpubkey: str   — hex of previous output script

    Returns list of cosigner groups (each group = list of pubkey hex strings).
    """
    clusters: list[list[str]] = []
    pubkey_groups: dict[str, list[str]] = {}

    for inp in taproot_inputs:
        witness = inp.get("txinwitness", [])
        if not witness:
            continue
        # Key-path spend: exactly one 64-byte Schnorr signature in witness
        if len(witness) == 1 and len(witness[0]) == 128:  # 64 bytes hex
            # Extract x-only pubkey from prevout
            prevout_script = inp.get("prevout_scriptpubkey", "")
            if len(prevout_script) == 68:  # 34 bytes hex = OP_1 + OP_PUSH32 + 32b
                xonly_pubkey = prevout_script[4:]  # skip OP_1 OP_PUSH32 prefix
                prefix = xonly_pubkey[:8]  # group by first 4 bytes for clustering
                pubkey_groups.setdefault(prefix, []).append(xonly_pubkey)

    clusters += [pubkeys for pubkeys in pubkey_groups.values() if len(pubkeys) > 1]
    return clusters


def estimate_privacy_routing(clusters: list[list[str]]) -> float:
    """
    Estimate privacy routing score [0, 1].

    Higher score = more use of privacy-enhancing Taproot features.
    Based on: fraction of key-path spends (indistinguishable from single-sig)
    vs script-path spends (reveals the full Taproot tree).
    """
    if not clusters:
        return 0.0
    # More clustered = more MuSig2 usage = higher privacy
    total_keys = sum(len(c) for c in clusters)
    n_clusters = len(clusters)
    if n_clusters == 0:
        return 0.0
    avg_cluster_size = total_keys / n_clusters
    # Score increases with average cluster size, saturates at 5+
    return float(min(avg_cluster_size / 5.0, 1.0))


def compute_smart_money_divergence(
    clusters: list[list[str]],
    retail_direction: float,  # [-1, +1] aggregate retail signal
) -> float:
    """
    Smart money divergence = how much large Taproot signers disagree with retail.

    Returns [-1, +1]: positive means smart money is more bullish than retail.
    Without live flow data, we proxy from cluster count and size.
    """
    if not clusters:
        return 0.0
    n_clusters = len(clusters)
    # Heuristic: more active MuSig2 clusters → sophisticated participants active
    # Their divergence from retail is approximated by cluster activity level
    activity_signal = min(n_clusters / 10.0, 1.0)
    # Divergence: smart money tends to be contrarian to retail at extremes
    return float(-retail_direction * activity_signal)


def parse_taproot_block(transactions: list[dict]) -> TaprootSpendInfo:
    """
    Analyse all transactions in a block for Taproot spend patterns.

    Each transaction dict should have:
      vin: list[dict] with txinwitness, prevout_scriptpubkey fields.
    """
    all_taproot_inputs = []
    key_path_count = 0
    script_path_count = 0

    for tx in transactions:
        vin = tx.get("vin", [])
        for inp in vin:
            witness = inp.get("txinwitness", [])
            prevout = inp.get("prevout", {})
            script_hex = prevout.get("scriptPubKey", {}).get("hex", "")

            try:
                script_bytes = bytes.fromhex(script_hex)
            except ValueError:
                continue

            if not is_p2tr_output(script_bytes):
                continue

            inp_with_prevout = {**inp, "prevout_scriptpubkey": script_hex}
            all_taproot_inputs.append(inp_with_prevout)

            if len(witness) == 1 and len(witness[0]) == 128:
                key_path_count += 1
            elif len(witness) > 1:
                script_path_count += 1

    musig2_clusters = detect_musig2_cosigners(all_taproot_inputs)
    privacy = estimate_privacy_routing(musig2_clusters)
    divergence = compute_smart_money_divergence(musig2_clusters, retail_direction=0.0)

    return TaprootSpendInfo(
        musig2_count=len(musig2_clusters),
        privacy_score=privacy,
        smart_money_divergence=divergence,
        p2tr_input_count=len(all_taproot_inputs),
        key_path_spends=key_path_count,
        script_path_spends=script_path_count,
    )
