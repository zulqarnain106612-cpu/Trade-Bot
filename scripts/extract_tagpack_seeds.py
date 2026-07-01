"""GAP-015 on-chain pipeline, phase 1: extract exchange/miner seed addresses.

Source: GraphSense TagPacks (https://github.com/graphsense/graphsense-tagpacks),
free, open-source (CC-BY-SA per their LICENSE), maintained by the GraphSense
core team + academic contributors. This is label data only -- no chain-scale
compute needed, so it runs fine on constrained hardware.

Scope decision (see DECISION_LOG.md "Intelligence feature wiring -- bounded
on-chain seed tracking"): full CIOH multi-hop entity clustering requires
GraphSense's own production cluster (14-core/320GB master + 8x 6-core/256GB
workers per their docs) -- infeasible on this host. Instead we track direct
(1-hop) on-chain flow to/from these labeled addresses. This under-counts
flow that passes through unlabeled intermediate hops before reaching an
exchange, so confidence is intentionally lower than a full-cluster figure --
callers must not treat this as equivalent to Glassnode/CryptoQuant coverage.

Run: python3 scripts/extract_tagpack_seeds.py <path-to-cloned-tagpacks-repo>
Output: src/intelligence/onchain/seed_addresses.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

# Only BTC: PRIMARY_SYMBOL=BTC/USDT (.env). Pulling ETH/other-chain seeds
# would be wasted scope for a BTC-only strategy and adds free-tier API load
# for no signal value.
TARGET_CURRENCY = "BTC"


def extract_pack(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not doc or "tags" not in doc:
        return []

    pack_category = doc.get("category", "unknown")
    pack_confidence = doc.get("confidence", "unknown")
    pack_actor = doc.get("actor")
    pack_lastmod = doc.get("lastmod")

    out: list[dict[str, Any]] = []
    for tag in doc["tags"]:
        if tag.get("currency") != TARGET_CURRENCY:
            continue
        out.append(
            {
                "address": tag["address"],
                "label": tag.get("label", ""),
                "actor": tag.get("actor", pack_actor),
                "category": pack_category,
                "tagpack_confidence": pack_confidence,
                "lastmod": tag.get("lastmod", pack_lastmod),
                "source_file": path.name,
            }
        )
    return out


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: extract_tagpack_seeds.py <tagpacks-repo-path>", file=sys.stderr)
        sys.exit(1)

    repo = Path(sys.argv[1])
    packs_dir = repo / "packs"
    if not packs_dir.is_dir():
        print(f"error: {packs_dir} not found", file=sys.stderr)
        sys.exit(1)

    seeds: list[dict[str, Any]] = []
    for yaml_file in sorted(packs_dir.glob("*.yaml")):
        seeds.extend(extract_pack(yaml_file))

    # De-dupe by address (same address can appear in >1 pack, e.g. cross-listed).
    by_address: dict[str, dict[str, Any]] = {}
    for s in seeds:
        addr = s["address"]
        if addr not in by_address:
            by_address[addr] = s
        # else: keep first-seen; a proper merge (multiple actors/labels per
        # address) is a real edge case but out of scope for phase 1.

    unique_seeds = sorted(by_address.values(), key=lambda s: (s["category"], s["actor"] or ""))

    out_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "intelligence"
        / "onchain"
        / "seed_addresses.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "schema_version": 1,
                "source": "https://github.com/graphsense/graphsense-tagpacks",
                "source_license": "CC-BY-SA-4.0 (see upstream repo LICENSE)",
                "currency": TARGET_CURRENCY,
                "extraction_method": "direct 1-hop label tracking, NOT full "
                "multi-hop CIOH clustering (see module docstring)",
                "seed_count": len(unique_seeds),
                "addresses": unique_seeds,
            },
            f,
            indent=2,
        )

    by_category: dict[str, int] = {}
    for s in unique_seeds:
        by_category[s["category"]] = by_category.get(s["category"], 0) + 1

    print(f"Wrote {len(unique_seeds)} unique {TARGET_CURRENCY} seed addresses to {out_path}")
    for cat, count in sorted(by_category.items()):
        print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
