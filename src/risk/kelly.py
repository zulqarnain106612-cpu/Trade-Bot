"""
Kelly position sizing — half-Kelly with hard ceiling.

Kelly (1956) "A New Interpretation of Information Rate", Bell System
Technical Journal 35(4): 917-926.

Implementation follows AFML Ch.10 (López de Prado 2018):
  - Kelly fraction f* = (p·b - q) / b  where b = win/loss ratio
  - Half-Kelly multiplier = 0.5 (spec)
  - Hard ceiling = 0.25 of capital (spec)
  - Position size = f x capital / entry_price, quantised to exchange precision

All sizing functions are pure (no I/O, no side effects) so they are
trivially testable and reusable across paper and live executors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Final

import structlog

from src.config import RiskSettings, get_settings


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants (from spec — never weakened)
# ---------------------------------------------------------------------------

_DEFAULT_MULTIPLIER: Final[float] = 0.5  # half-Kelly
_DEFAULT_CEILING: Final[float] = 0.25  # 25% of capital max


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KellyResult:
    """
    Position sizing outcome for a single trade.

    kelly_fraction   : raw Kelly fraction f* before multiplier/ceiling
    adjusted_fraction: f* x multiplier, capped at ceiling
    capital_usd      : equity used for sizing
    entry_price      : asset entry price
    quantity         : asset units to trade (quantised)
    notional_usd     : quantity x entry_price
    is_capped        : True when ceiling binding (raw x mult > ceiling)
    """

    kelly_fraction: float
    adjusted_fraction: float
    capital_usd: float
    entry_price: float
    quantity: float
    notional_usd: float
    is_capped: bool

    @property
    def position_size_pct(self) -> float:
        """Adjusted fraction expressed as percentage of capital."""
        return self.adjusted_fraction * 100.0


# ---------------------------------------------------------------------------
# Core Kelly formula — Kelly (1956)
# ---------------------------------------------------------------------------


def kelly_fraction(
    win_probability: float,
    win_loss_ratio: float,
) -> float:
    """
    Compute the raw Kelly fraction.

    f* = (p·b - q) / b

    where:
      p = win probability
      q = 1 - p  (loss probability)
      b = win/loss ratio (average win magnitude / average loss magnitude)

    Parameters
    ----------
    win_probability : probability of a winning trade, in (0, 1)
    win_loss_ratio  : ratio of average win to average loss, > 0

    Returns
    -------
    Raw Kelly fraction in [0, 1].  Clipped to [0, 1] — negative Kelly
    (negative edge) returns 0.0, meaning do not bet.

    Raises
    ------
    ValueError : if inputs are outside valid ranges.
    """
    if not 0.0 < win_probability < 1.0:
        raise ValueError(f"win_probability must be in (0, 1), got {win_probability}")
    if win_loss_ratio <= 0.0 or not math.isfinite(win_loss_ratio):
        raise ValueError(f"win_loss_ratio must be a finite number > 0, got {win_loss_ratio}")

    p = win_probability
    q = 1.0 - p
    b = win_loss_ratio
    f_star = (p * b - q) / b

    # Clip to [0, 1] — negative edge → no bet; f* > 1 is theoretically
    # impossible for b > 0 and valid p, but guard defensively.
    return float(max(0.0, min(1.0, f_star)))


def half_kelly_fraction(
    win_probability: float,
    win_loss_ratio: float,
    multiplier: float | None = None,
    ceiling: float | None = None,
    cfg: RiskSettings | None = None,
) -> tuple[float, float, bool]:
    """
    Compute the half-Kelly adjusted fraction with ceiling.

    Parameters
    ----------
    win_probability : P(win) estimated from model output
    win_loss_ratio  : average_win / average_loss from historical trades
    multiplier      : Kelly multiplier (default 0.5 from config)
    ceiling         : maximum fraction (default 0.25 from config)
    cfg             : RiskSettings; loaded from global config if None

    Returns
    -------
    (raw_fraction, adjusted_fraction, is_capped) where:
      raw_fraction      = f* (Kelly formula output)
      adjusted_fraction = min(f* x multiplier, ceiling)
      is_capped         = True when ceiling was binding
    """
    if cfg is None:
        cfg = get_settings().risk

    mult = multiplier if multiplier is not None else cfg.kelly_multiplier
    cap = ceiling if ceiling is not None else cfg.kelly_ceiling

    # VF-025: explicit multiplier/ceiling overrides had no bounds check.
    # No current caller passes these, but the public signature otherwise
    # lets any future caller silently exceed the spec'd half-Kelly
    # multiplier / 0.25 ceiling — CLAUDE.md: "Risk Gates — never weaken."
    if not (0.0 <= mult <= 1.0):
        raise ValueError(f"multiplier must be in [0, 1], got {mult}")
    if not (0.0 <= cap <= 1.0):
        raise ValueError(f"ceiling must be in [0, 1], got {cap}")

    raw = kelly_fraction(win_probability, win_loss_ratio)
    adjusted = raw * mult
    capped = adjusted > cap
    adjusted = min(adjusted, cap)

    log.debug(
        "kelly.computed",
        win_prob=round(win_probability, 4),
        win_loss_ratio=round(win_loss_ratio, 4),
        raw_fraction=round(raw, 4),
        adjusted_fraction=round(adjusted, 4),
        multiplier=mult,
        ceiling=cap,
        is_capped=capped,
    )
    return raw, adjusted, capped


# ---------------------------------------------------------------------------
# Probability-based Kelly — uses XGBoost probabilities directly
# ---------------------------------------------------------------------------


def kelly_from_model_probs(
    p_long: float,
    avg_win_usd: float,
    avg_loss_usd: float,
    direction: int,
    cfg: RiskSettings | None = None,
) -> tuple[float, float, bool]:
    """
    Compute half-Kelly fraction from model output probabilities.

    The XGBoost direction model outputs P(long).  We treat this as the
    win probability for the chosen direction:
      - If direction=1 (long):  win_prob = p_long
      - If direction=0 (short): win_prob = 1 - p_long

    win_loss_ratio is computed from historical trade averages.
    If no trade history is available yet, falls back to a conservative
    win_loss_ratio of 1.0 (equal payoff assumption).

    Parameters
    ----------
    p_long       : XGBoost P(long) for this bar
    avg_win_usd  : average winning trade PnL in USD (> 0)
    avg_loss_usd : average absolute losing trade PnL in USD (> 0)
    direction    : 1 = long, 0 = short
    cfg          : RiskSettings

    Returns
    -------
    (raw_fraction, adjusted_fraction, is_capped)
    """
    if cfg is None:
        cfg = get_settings().risk

    # VF-026: direction silently fell through to the "short" branch for
    # any value other than 1 (e.g. a bug passing -1 or 2) — same defect
    # class already fixed in fetcher.py (VF-014, unknown exchange_id).
    if direction not in (0, 1):
        raise ValueError(f"direction must be 0 (short) or 1 (long), got {direction}")

    # VF-027: a non-finite p_long (corrupted/NaN model output) previously
    # survived `max(0.01, min(0.99, win_prob))` unchanged in *effect* —
    # Python's min(0.99, nan) returns 0.99 because comparisons against NaN
    # are always False, so NaN was silently coerced to *maximum*
    # confidence (0.99) rather than triggering a fail-safe skip — the
    # opposite of this project's fail-safe posture. Reject explicitly and
    # skip the trade instead of sizing a position on garbage input.
    if not math.isfinite(p_long):
        log.error(
            "kelly.invalid_p_long",
            p_long=p_long,
            direction=direction,
            action="returning zero fraction — skip trade, model output non-finite",
        )
        return 0.0, 0.0, False

    win_prob = p_long if direction == 1 else (1.0 - p_long)

    # Guard edge cases from model output noise
    win_prob = max(0.01, min(0.99, win_prob))

    win_loss_ratio = 1.0
    if avg_win_usd > 0.0 and avg_loss_usd > 0.0:
        win_loss_ratio = avg_win_usd / avg_loss_usd

    # VF-028: an extreme avg_win_usd / near-zero avg_loss_usd combination
    # (a data bug, not a real trade) can overflow float64 to `inf`, which
    # then produces `inf/inf = nan` inside kelly_fraction()'s formula and
    # — via the same NaN-vs-literal artifact as VF-027 — would now raise
    # instead of silently returning max aggression, since kelly_fraction
    # was hardened above. Clamp here so a stats-derived ratio can never
    # propagate a non-finite value into the formula in the first place.
    if not math.isfinite(win_loss_ratio):
        win_loss_ratio = 1.0
    # Floor win_loss_ratio at 0.1 to prevent division instability; ceiling
    # at 1000 to prevent float overflow from a corrupted stats input.
    win_loss_ratio = max(0.1, min(win_loss_ratio, 1000.0))

    return half_kelly_fraction(
        win_probability=win_prob,
        win_loss_ratio=win_loss_ratio,
        cfg=cfg,
    )


# ---------------------------------------------------------------------------
# Position sizing — converts fraction to quantity
# ---------------------------------------------------------------------------


def size_position(
    adjusted_fraction: float,
    capital_usd: float,
    entry_price: float,
    amount_precision: float = 8.0,
    min_amount: float = 0.0,
    min_cost: float = 0.0,
    max_position_pct: float | None = None,
    cfg: RiskSettings | None = None,
) -> KellyResult | None:
    """
    Convert a Kelly fraction into a concrete position size.

    Applies a second guard: adjusted_fraction is also capped at
    max_position_size_pct / 100 (spec: 5% of capital max).

    Quantises quantity to exchange amount_precision decimal places.

    Parameters
    ----------
    adjusted_fraction  : half-Kelly output (already capped at ceiling)
    capital_usd        : current equity in USD
    entry_price        : asset price at entry
    amount_precision   : decimal places for quantity (exchange-specific)
    min_amount         : minimum order quantity (exchange-specific)
    min_cost           : minimum order notional USD (exchange-specific)
    max_position_pct   : override max position % (default from config)
    cfg                : RiskSettings

    Returns
    -------
    KellyResult if position meets minimum size requirements, else None.
    None signals the signal engine to skip this trade.

    Raises
    ------
    ValueError : if capital_usd or entry_price are non-positive.
    """
    # VF-029: `<= 0.0` does not catch NaN (NaN comparisons are always
    # False in IEEE-754), so a NaN capital_usd/entry_price previously
    # passed this guard silently instead of raising — same defect class
    # as VF-024/VF-027/VF-028. Explicit finiteness check closes the gap.
    if not math.isfinite(capital_usd) or capital_usd <= 0.0:
        raise ValueError(f"capital_usd must be a finite number > 0, got {capital_usd}")
    if not math.isfinite(entry_price) or entry_price <= 0.0:
        raise ValueError(f"entry_price must be a finite number > 0, got {entry_price}")

    if cfg is None:
        cfg = get_settings().risk

    # VF-030: `max_position_pct or cfg.max_position_size_pct` treated an
    # explicit `max_position_pct=0.0` (a legitimate "no new positions"
    # call) as falsy and silently fell back to the config default —
    # the opposite of the caller's intent. Use the same explicit
    # `is not None` pattern already used for multiplier/ceiling in
    # half_kelly_fraction() above. Also bound the result so a future
    # caller-supplied override can never weaken or invert the
    # position-size risk gate (CLAUDE.md: "Risk Gates — never weaken").
    max_position_pct_resolved = (
        max_position_pct if max_position_pct is not None else cfg.max_position_size_pct
    )
    if not (0.0 <= max_position_pct_resolved <= 100.0):
        raise ValueError(f"max_position_pct must be in [0, 100], got {max_position_pct_resolved}")
    max_pct = max_position_pct_resolved / 100.0

    # Enforce max position size cap on top of Kelly ceiling
    raw_kelly = adjusted_fraction
    final_fraction = min(adjusted_fraction, max_pct)
    is_capped = final_fraction < raw_kelly

    notional_raw = final_fraction * capital_usd
    quantity_raw = notional_raw / entry_price

    # Quantise to exchange precision
    decimal_places = int(amount_precision)
    quantity = _floor_to_precision(quantity_raw, decimal_places)

    if quantity <= 0.0:
        log.debug(
            "kelly.size_zero",
            quantity_raw=quantity_raw,
            decimal_places=decimal_places,
        )
        return None

    notional = quantity * entry_price

    # Check exchange minimums
    if min_amount > 0.0 and quantity < min_amount:
        log.debug(
            "kelly.below_min_amount",
            quantity=quantity,
            min_amount=min_amount,
        )
        return None

    if min_cost > 0.0 and notional < min_cost:
        log.debug(
            "kelly.below_min_cost",
            notional=notional,
            min_cost=min_cost,
        )
        return None

    result = KellyResult(
        kelly_fraction=raw_kelly,
        adjusted_fraction=final_fraction,
        capital_usd=capital_usd,
        entry_price=entry_price,
        quantity=quantity,
        notional_usd=notional,
        is_capped=is_capped,
    )

    log.info(
        "kelly.position_sized",
        fraction=round(final_fraction, 4),
        capital_usd=round(capital_usd, 2),
        entry_price=round(entry_price, 2),
        quantity=quantity,
        notional_usd=round(notional, 2),
        is_capped=is_capped,
    )
    return result


# ---------------------------------------------------------------------------
# Full sizing pipeline — single entry point for executors
# ---------------------------------------------------------------------------


def compute_position_size(
    p_long: float,
    direction: int,
    capital_usd: float,
    entry_price: float,
    avg_win_usd: float = 0.0,
    avg_loss_usd: float = 0.0,
    amount_precision: float = 8.0,
    min_amount: float = 0.0,
    min_cost: float = 0.0,
    regime_scalar: float = 1.0,
    correlation_scalar: float = 1.0,
    notional_cap_usd: float | None = None,
    cfg: RiskSettings | None = None,
) -> KellyResult | None:
    """
    End-to-end position sizing from model output to exchange quantity.

    Called by both paper and live executors.  Returns None if the
    computed size fails any minimum threshold.

    Parameters
    ----------
    p_long         : XGBoost P(long) for this bar
    direction      : 1=long, 0=short
    capital_usd    : current equity in USD
    entry_price    : asset price at entry
    avg_win_usd    : average winning trade PnL USD (0.0 = use default ratio)
    avg_loss_usd   : average losing trade |PnL| USD (0.0 = use default ratio)
    amount_precision: exchange amount decimal places
    min_amount     : exchange minimum order quantity
    min_cost       : exchange minimum order notional USD
    regime_scalar  : GAP-002 — HMM entropy-based confidence scalar in
                     [0, 1], typically RegimePrediction.position_scalar().
                     Multiplies the half-Kelly fraction down when regime
                     classification is uncertain. Defaults to 1.0 (no-op)
                     for backward compatibility with callers that do not
                     pass a regime prediction (e.g. unit tests, back-test
                     harnesses without a fitted detector).
    correlation_scalar : GAP-005/GAP-015 — portfolio correlation scalar in
                     [0, 1] from
                     src.risk.portfolio_correlation.PortfolioCorrelationTracker.correlation_scalar().
                     Multiplies the half-Kelly fraction down when the new
                     symbol is highly correlated with currently open
                     positions (per-symbol Kelly otherwise ignores
                     cross-asset correlation — see Gap-005). Defaults to
                     1.0 (no-op) for backward compatibility with callers
                     that do not pass a correlation tracker (e.g. unit
                     tests, single-symbol back-test harnesses).
    notional_cap_usd : GAP-015 — absolute USD notional cap from
                     src.strategies.position_sizing.recommend_position_notional()
                     (Carver/AFML/Thorp minimum). Applied as a hard ceiling
                     on the quantity after Kelly sizing, implementing
                     Carver (2019) 'whichever method gives the smaller position'.
                     None = no cap (no-op, backward compatible).
    cfg            : RiskSettings

    Returns
    -------
    KellyResult or None.
    """
    if cfg is None:
        cfg = get_settings().risk

    # GAP-002: regime_scalar narrows the position size, never widens it.
    # An out-of-range value (e.g. a future caller bug passing >1.0) must
    # not be able to amplify Kelly beyond the half-Kelly/ceiling spec —
    # same "never weaken a risk gate" posture as the rest of this module.
    if not math.isfinite(regime_scalar):
        log.error("kelly.invalid_regime_scalar", regime_scalar=regime_scalar)
        regime_scalar_clamped = 0.0
    else:
        regime_scalar_clamped = max(0.0, min(1.0, regime_scalar))

    # GAP-005/GAP-015: same fail-safe posture as regime_scalar above —
    # an invalid correlation_scalar clamps to 0.0 (block sizing), never
    # to a value that could widen the position beyond what regime/Kelly
    # already determined.
    if not math.isfinite(correlation_scalar):
        log.error("kelly.invalid_correlation_scalar", correlation_scalar=correlation_scalar)
        correlation_scalar_clamped = 0.0
    else:
        correlation_scalar_clamped = max(0.0, min(1.0, correlation_scalar))

    _raw_frac, adj_frac, _ = kelly_from_model_probs(
        p_long=p_long,
        avg_win_usd=avg_win_usd,
        avg_loss_usd=avg_loss_usd,
        direction=direction,
        cfg=cfg,
    )

    adj_frac = adj_frac * regime_scalar_clamped * correlation_scalar_clamped

    result = size_position(
        adjusted_fraction=adj_frac,
        capital_usd=capital_usd,
        entry_price=entry_price,
        amount_precision=amount_precision,
        min_amount=min_amount,
        min_cost=min_cost,
        cfg=cfg,
    )

    # GAP-015: Carver/AFML/Thorp notional cap — shrink only, never expand.
    if (
        result is not None
        and notional_cap_usd is not None
        and notional_cap_usd > 0.0
        and result.notional_usd > notional_cap_usd
    ):
        # Re-quantise at the capped notional.
        capped_qty_raw = notional_cap_usd / entry_price
        decimal_places = int(amount_precision)
        capped_qty = _floor_to_precision(capped_qty_raw, decimal_places)
        if capped_qty > 0.0:
            log.info(
                "kelly.carver_cap_applied",
                kelly_notional=round(result.notional_usd, 2),
                cap_notional=round(notional_cap_usd, 2),
                capped_qty=capped_qty,
            )
            result = KellyResult(
                kelly_fraction=result.kelly_fraction,
                adjusted_fraction=result.adjusted_fraction,
                capital_usd=result.capital_usd,
                entry_price=result.entry_price,
                quantity=capped_qty,
                notional_usd=round(capped_qty * entry_price, 2),
                is_capped=True,
            )

    return result


# ---------------------------------------------------------------------------
# Historical win/loss statistics helper
# ---------------------------------------------------------------------------


def compute_win_loss_stats(
    pnl_series: list[float],
) -> tuple[float, float, float]:
    """
    Compute win probability, average win, and average loss from PnL history.

    Parameters
    ----------
    pnl_series : list of realised PnL values (positive = win, negative = loss)

    Returns
    -------
    (win_probability, avg_win_usd, avg_loss_usd)

    If fewer than 50 trades available, returns conservative defaults:
    (0.5, 1.0, 1.0) — equal-probability, equal-payoff assumption.
    """
    if len(pnl_series) < 50:  # NEW-010: raised from 10 — 10-trade window is luck-dominated
        return 0.5, 1.0, 1.0

    wins = [p for p in pnl_series if p > 0.0]
    losses = [abs(p) for p in pnl_series if p < 0.0]

    if not wins or not losses:
        return 0.5, 1.0, 1.0

    win_prob = len(wins) / len(pnl_series)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)

    return win_prob, avg_win, avg_loss


# ---------------------------------------------------------------------------
# Internal utility
# ---------------------------------------------------------------------------


def _floor_to_precision(value: float, decimal_places: int) -> float:
    """
    Floor value to decimal_places using Decimal arithmetic.

    SCAN3-014: the previous implementation used math.floor(value * 10**n) / 10**n
    which has IEEE 754 representation artifacts for large decimal_places (e.g. n=8
    on BTC quantities). Decimal(str(value)) round-trips through the string
    representation, matching human-readable precision and avoiding binary artifacts.
    This is the same approach used internally by ccxt for quantity quantization.

    Examples
    --------
    >>> _floor_to_precision(0.123456789, 6)
    0.123456
    >>> _floor_to_precision(0.1, 8)
    0.1
    """
    if decimal_places < 0:
        raise ValueError(f"decimal_places must be >= 0, got {decimal_places}")
    q = Decimal(10) ** -decimal_places
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))
