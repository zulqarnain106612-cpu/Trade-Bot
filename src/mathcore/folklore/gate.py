"""
The folklore gate: refuse to trade on a pattern the registry marks as folklore
unless it is backed by evidence that would survive scrutiny.

Owns the enforcement half of every ``folklore`` registry entry. Those entries
-- Fibonacci retracement, Elliott waves, Gann angles, stock-to-flow, rainbow
charts, astro-trading, meme-supply numerology -- are documented as folklore so
no one mistakes them for signal. This module turns that documentation into a
runtime refusal: :func:`assert_not_folklore` raises when a strategy tries to
register such a feature.

The refusal is not absolute, and that is the point. A folklore label says "no
evidence has cleared the bar", not "this may never be tested". If a caller has
genuinely done the work -- held out an out-of-sample window, counted how many
hypotheses they tried, and corrected the significance for that count -- the
gate lets the feature through, because at that point it is no longer folklore
by assertion. What the gate forbids is the shortcut: promoting numerology to a
live signal on a hunch, or by editing one field in the registry (which
``src/mathcore/registry.py`` separately refuses).

:class:`ValidationEvidence` is built so that shortcut cannot be taken. It
cannot be constructed at all without all three ingredients, and it is not
*sufficient* unless the significance survives the multiple-testing correction.
A p-value of 0.04 from one of fifty tried indicators is noise, and the gate
says so.

References: the ``folklore`` entries in ``config/math_registry.json``; Harvey,
Liu & Zhu, *...and the Cross-Section of Expected Returns* (2016) on
multiple-testing in finance; Bailey & Lopez de Prado, *The Deflated Sharpe
Ratio* (2014).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..registry import MathRegistry, RegistryError, load_registry

__all__ = [
    "MULTIPLE_TESTING_CORRECTIONS",
    "FolkloreError",
    "RegistryError",
    "ValidationEvidence",
    "assert_not_folklore",
]

# The corrections this gate recognises. Each maps a raw p-value and a hypothesis
# count to a corrected p-value. Both are conservative in the count, which is the
# direction that matters: the gate should be hard to fool, not easy to pass.
MULTIPLE_TESTING_CORRECTIONS = frozenset({"bonferroni", "sidak"})

# The corrected-significance bar. Deliberately stricter than the ritual 0.05:
# a folklore feature is presumed noise, so the evidence to overturn that has to
# be strong, not marginal.
_ALPHA = 0.01


class FolkloreError(RuntimeError):
    """Raised when a folklore feature is used without sufficient evidence."""


@dataclass(frozen=True)
class ValidationEvidence:
    """
    The evidence a folklore feature needs before it may be traded.

    Cannot be constructed without all three of the ingredients the roadmap
    names, and each is validated on construction rather than trusted:

    * ``out_of_sample_days`` -- the length of a held-out window the feature was
      *not* fitted on. Zero means the feature was only ever seen in-sample,
      which proves nothing, so it is refused.
    * ``hypotheses_tried`` -- how many features/indicators were tested to find
      this one. One is the honest floor; the danger is a caller who tried fifty
      and reports the winner as if it were the only test.
    * ``correction`` -- the multiple-testing correction applied, which must be
      one this gate knows how to compute.
    * ``raw_p_value`` -- the uncorrected significance.

    :attr:`is_sufficient` is the property the gate checks: the *corrected*
    p-value must clear the bar. Construction validates the inputs; sufficiency
    is a separate, stronger question, so an honestly-built but weak piece of
    evidence exists and is simply not enough.
    """

    out_of_sample_days: int
    hypotheses_tried: int
    correction: str
    raw_p_value: float

    def __post_init__(self) -> None:
        if self.out_of_sample_days <= 0:
            raise ValueError(
                "out_of_sample_days must be positive; evidence seen only "
                "in-sample proves nothing about live performance"
            )
        if self.hypotheses_tried < 1:
            raise ValueError(
                "hypotheses_tried must be at least 1; a search that tried no "
                "hypotheses found nothing to validate"
            )
        if self.correction not in MULTIPLE_TESTING_CORRECTIONS:
            raise ValueError(
                f"correction must be one of {sorted(MULTIPLE_TESTING_CORRECTIONS)}, "
                f"got {self.correction!r}; an uncorrected p-value over many "
                "hypotheses is exactly the error this gate exists to stop"
            )
        if not 0.0 <= self.raw_p_value <= 1.0:
            raise ValueError(f"raw_p_value must be in [0, 1], got {self.raw_p_value}")

    @property
    def corrected_p_value(self) -> float:
        """
        The raw p-value adjusted for the number of hypotheses tried.

        Bonferroni multiplies by the count; Sidak uses ``1-(1-p)**m``. Both are
        capped at 1. This is what turns "0.04, but from fifty tries" into a
        number that reflects how easily noise produces it.
        """
        m = self.hypotheses_tried
        if self.correction == "bonferroni":
            return min(1.0, self.raw_p_value * m)
        # sidak
        return 1.0 - (1.0 - self.raw_p_value) ** m

    @property
    def is_sufficient(self) -> bool:
        """Whether the corrected significance clears the bar."""
        return self.corrected_p_value < _ALPHA


def assert_not_folklore(
    feature_id: str,
    evidence: ValidationEvidence | None = None,
    *,
    registry: MathRegistry | None = None,
) -> None:
    """
    Refuse to register ``feature_id`` if the registry marks it folklore and no
    sufficient evidence is supplied.

    ``feature_id`` is a registry entry id. If the entry is not folklore, this
    returns silently -- the gate only guards the folklore verdict. If it is
    folklore and ``evidence`` is missing or insufficient, this raises
    :class:`FolkloreError` with a message naming the entry and stating exactly
    what evidence would be required, so the failure teaches rather than merely
    blocks.

    An unknown ``feature_id`` is a programming error, not a folklore hit, so the
    registry's own :class:`RegistryError` propagates rather than being reported
    as folklore -- a typo in a feature name must not read as "validated".

    ``registry`` is injectable for testing; by default the project registry is
    loaded.
    """
    registry = registry if registry is not None else load_registry()
    entry = registry.get(feature_id)  # RegistryError on an unknown id

    if entry.verdict != "folklore":
        return

    if evidence is not None and evidence.is_sufficient:
        return

    required = (
        "sufficient ValidationEvidence: a held-out out-of-sample window, the "
        "count of hypotheses tried, and a multiple-testing correction whose "
        f"corrected p-value is below {_ALPHA}"
    )
    if evidence is None:
        reason = "no validation evidence was supplied"
    else:
        reason = (
            f"the evidence is insufficient (corrected p-value "
            f"{evidence.corrected_p_value:.4g} is not below {_ALPHA})"
        )
    raise FolkloreError(
        f"{feature_id!r} ({entry.name}) is marked folklore in the registry and "
        f"may not be used as a live signal: {reason}. Required: {required}. "
        f"Why it is folklore: {entry.risk_if_misused or entry.role}"
    )
