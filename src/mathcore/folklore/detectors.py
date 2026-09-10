"""
Name-based detection of folklore features, so a strategy cannot register one
under a euphemism.

The :func:`~src.mathcore.folklore.gate.assert_not_folklore` gate keys on a
registry entry id. But a strategy author writes a *feature name* --
``"fib_618_retracement"``, ``"elliott_wave_count"``, ``"s2f_ratio"`` -- and the
job here is to map that name to the registry entry it really is, so the gate
can fire. Without this step a folklore feature slips through simply by not
being called by its registry id.

The matching is keyword-based and deliberately broad on the folklore side: it
is better to challenge a borderline name and make the author pass the gate (or
rename) than to let ``golden_ratio_entry`` through because it did not say
"fibonacci". A false positive costs a conversation; a false negative costs the
thing the whole phase exists to prevent.

Feature names are normalised -- ``_`` and ``-`` become spaces -- before
matching, so a word boundary means what it should: ``s2f`` is found in
``s2f_ratio`` (Python's ``\\b`` treats ``_`` as a word character and would
miss it otherwise). :func:`assert_feature_not_folklore` is the one call a
feature-registration path makes: it detects, and if the name maps to folklore
it runs the gate.
"""

from __future__ import annotations

import re

from .gate import ValidationEvidence, assert_not_folklore

__all__ = [
    "FOLKLORE_PATTERNS",
    "assert_feature_not_folklore",
    "detect_folklore",
]

# Each registry folklore id, with the keyword patterns that name it in the wild.
# Matched case-insensitively against the separator-normalised name (see
# _normalise), so \b behaves at underscores and hyphens.
FOLKLORE_PATTERNS: dict[str, tuple[str, ...]] = {
    "fibonacci-retracement": (
        r"\bfib(?:onacci)?\b",
        r"\bgolden ratio\b",
        r"\b0?\.618\b",
        r"\b1\.618\b",
    ),
    "elliott-wave": (r"\belliott\b", r"\bwave count\b", r"\bimpulse wave\b"),
    "gann-methods": (r"\bgann\b", r"\bsquare of nine\b"),
    "stock-to-flow": (r"\bstock to flow\b", r"\bs2fx?\b"),
    "rainbow-and-power-law-charts": (
        r"\brainbow chart\b",
        r"\bpower law (?:band|price|chart)\b",
    ),
    "astro-lunar-trading": (
        r"\blunar\b",
        r"\bastro\w*\b",
        r"\bzodiac\b",
        r"\bmoon phase\b",
        r"\bplanetary\b",
    ),
    "meme-tokenomics-numbers": (
        r"\bmeme\b",
        r"\bvanity address\b",
        r"\btokenomics magic\b",
        r"\b(?:69|420) (?:meme|moon|token|magic)\b",
    ),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    entry_id: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for entry_id, patterns in FOLKLORE_PATTERNS.items()
}


def _normalise(feature_name: str) -> str:
    """Lower-case and turn ``_``/``-`` into spaces, so ``\\b`` sees real gaps."""
    return re.sub(r"[_\-]+", " ", feature_name).strip()


def detect_folklore(feature_name: str) -> str | None:
    """
    Return the folklore registry id ``feature_name`` matches, or ``None``.

    The first entry with a matching keyword wins; the patterns are disjoint by
    construction, so order does not change the answer. ``None`` means the name
    triggers no folklore pattern -- which is not a clean bill of health, only
    the absence of a name-based match. A folklore feature deliberately
    mislabelled to dodge every keyword is beyond what name detection can catch,
    and this docstring says so rather than implying detection is exhaustive.
    """
    normalised = _normalise(feature_name)
    for entry_id, patterns in _COMPILED.items():
        if any(pattern.search(normalised) for pattern in patterns):
            return entry_id
    return None


def assert_feature_not_folklore(
    feature_name: str,
    evidence: ValidationEvidence | None = None,
) -> None:
    """
    Detect whether ``feature_name`` names a folklore feature and, if so, gate it.

    This is the single call a feature-registration path makes. A name that maps
    to no folklore entry passes silently; one that maps to a folklore entry is
    handed to :func:`~src.mathcore.folklore.gate.assert_not_folklore` with the
    resolved id, so the same evidence bar applies whether the caller used the
    registry id or a plausible alias.
    """
    entry_id = detect_folklore(feature_name)
    if entry_id is None:
        return
    assert_not_folklore(entry_id, evidence)
