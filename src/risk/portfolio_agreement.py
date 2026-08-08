"""
Portfolio-agreement scalar — turns strategy disagreement into a size ceiling.

src/engine/strategy_portfolio.py polls every registered strategy and resolves
their votes, but an evaluation nothing consumes is decoration. This module is
the consumer: it maps "what does the rest of the book think of the trade the
signal engine wants to place?" onto a multiplicative scalar in (0, 1].

The scalar is **shrink-only**, by construction and by test. It can never
exceed 1.0, so the portfolio agreeing with the incumbent is worth exactly
nothing in size — it only removes a reduction that disagreement would have
imposed. That asymmetry is deliberate and matches how the macro overlay and
both correlation scalars already behave: Kelly is a ceiling, not a target,
and a second opinion is grounds for betting less, never for betting more.
Letting agreement scale a position up would make a correlated cluster of
strategies — which is what a portfolio of momentum-adjacent families tends to
be in a trending market — bid its own size up precisely when its members are
most likely to be wrong together.

Three regimes of disagreement, in increasing severity:

* **Opposed.** The portfolio's resolved direction is the opposite of the
  trade. Shrunk hardest — other families are looking at the same market and
  reaching the opposite conclusion.
* **Conflicted.** The portfolio has no coherent direction of its own (the
  resolver's agreement ratio fell below its threshold). Shrunk, because a
  book that cannot agree with itself is not confirmation of anything.
* **Aligned but thin.** The portfolio agrees, but on weak conviction. Left
  alone: weak agreement is still agreement, and shrinking it would penalise
  the ordinary case where non-incumbent families are simply quiet.

Abstentions never count as dissent. A family with no data feed reports no
opinion, and treating that as disagreement would make every unwired feed a
silent, permanent size reduction — the exact failure mode the portfolio's
explicit-abstention design exists to prevent.

Authority: Carver (2019) Systematic Trading Ch.11 — diversification reduces
required size, it does not license more of it; López de Prado (2018) AFML
Ch.16 on treating heterogeneous strategies as independent evidence.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@runtime_checkable
class _Resolution(Protocol):
    agreement_ratio: float


@runtime_checkable
class EvaluationView(Protocol):
    """
    The part of a portfolio evaluation this module reads.

    Structural rather than a direct import of
    ``src.engine.strategy_portfolio.PortfolioEvaluation``: risk must not
    depend on the engine layer, and the engine already depends on risk
    (gates, Kelly, correlation). Importing the concrete type here would
    close that cycle for the sake of three attributes.
    """

    @property
    def direction(self) -> int: ...

    @property
    def conflict(self) -> bool: ...

    @property
    def voting_ids(self) -> tuple[str, ...]: ...

    @property
    def resolution(self) -> _Resolution: ...


# Applied when the portfolio's resolved direction opposes the trade. The
# floor is deliberately well above zero: the incumbent is the only family
# with out-of-sample validation, so the rest of the book gets to reduce its
# size substantially but never to veto it outright. A veto belongs to the
# risk gates, which can actually block, not to a sizing scalar.
_OPPOSED_SCALAR: float = 0.40

# Applied when the portfolio is internally conflicted — no direction of its
# own. Milder than opposition: "we don't agree with each other" is weaker
# evidence against the trade than "we agree, and against you".
_CONFLICTED_SCALAR: float = 0.70

# Below this many directional voters the portfolio is too thin to be
# evidence either way. One dissenting family is an opinion, not a consensus.
_MIN_VOTERS_FOR_DISSENT: int = 2


def portfolio_agreement_scalar(
    evaluation: EvaluationView | None,
    trade_direction: int,
) -> float:
    """
    Size ceiling in (0, 1] from the portfolio's view of `trade_direction`.

    Returns 1.0 — no reduction — whenever the portfolio has nothing to say:
    no evaluation yet, a flat trade, too few voters, or agreement. Only
    active disagreement reduces.

    Parameters
    ----------
    evaluation : the latest portfolio evaluation, or None before the first.
    trade_direction : 1 long, -1 short. 0 means no trade, so no ceiling.
    """
    if evaluation is None or trade_direction == 0:
        return 1.0

    voters = len(evaluation.voting_ids)
    if voters < _MIN_VOTERS_FOR_DISSENT:
        # Includes the case where every non-incumbent family abstained: an
        # unwired feed must not read as dissent.
        return 1.0

    if evaluation.conflict:
        log.info(
            "portfolio_agreement.conflicted",
            scalar=_CONFLICTED_SCALAR,
            voters=voters,
            agreement_ratio=round(evaluation.resolution.agreement_ratio, 4),
        )
        return _CONFLICTED_SCALAR

    direction = evaluation.direction
    if direction != 0 and direction != trade_direction:
        log.info(
            "portfolio_agreement.opposed",
            scalar=_OPPOSED_SCALAR,
            voters=voters,
            trade_direction=trade_direction,
            portfolio_direction=direction,
        )
        return _OPPOSED_SCALAR

    return 1.0


# There was an apply_portfolio_agreement() here: a multiplicative fold of
# this scalar into an existing size ceiling, written for a call site that
# turned out not to exist. The plan was to apply the ceiling before
# engine.tick(), folded into correlation_scalar — but the trade's direction
# is not known until after the tick, and this scalar is directional, so that
# call site was never viable.
#
# The ceiling reaches the order through risk.kelly.apply_size_scalar
# instead, which shrinks the sized KellyResult, requantises it to the
# exchange's precision and rechecks its minimums. A plain multiply cannot do
# any of that.
#
# Removed rather than left in place, against the general rule of keeping
# inert code and fixing it, because this was not an inert project feature —
# it was an orphan created in this branch that duplicated a live mechanism
# in a weaker form. Left exported and tested, its five green assertions
# proved nothing about the running system, and the next person to reach for
# it would have applied the ceiling without requantisation and without the
# minimum-notional check, reintroducing exactly the defect that wiring
# fetch_symbol_precision was meant to close.


__all__ = ["EvaluationView", "portfolio_agreement_scalar"]
