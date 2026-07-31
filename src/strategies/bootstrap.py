"""
Strategy portfolio bootstrap — populates the default StrategyRegistry.

src/strategies/registry.py defines the contract and the process-wide
registry, and every strategy family (mean reversion, breakout, funding
carry, cross-sectional momentum, basis, cross-exchange arb, options carry)
already conforms to it — but nothing ever called ``register()``, so the
registry was empty at runtime and every consumer degraded to a no-op:
``performance_weighted_allocate`` had no strategies to weight, and
``GET /debug/capital-allocation`` returned ``{"allocations": {}}``
unconditionally.

This module is the missing wiring. It is deliberately declarative — a
table of (strategy_id, factory, enabled flag, capital fraction) — so
adding a strategy family is a one-line change and the enabled set is
visible in one place rather than scattered across call sites.

Registration is *not* execution. A registered strategy participates in
capital allocation and attribution; it only trades once the orchestrator
feeds it a context and acts on the returned Signal. Registering is
therefore safe to do unconditionally at startup.

Capital fractions are upper bounds enforced independently of Kelly (Kelly
is a ceiling, not a target — see CLAUDE.md Domain Priors), and the sum of
enabled fractions is validated at bootstrap so a mis-set config cannot
commit more than 100% of book capital before a single order is placed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import structlog

from src.config import StrategyPortfolioSettings, get_settings
from src.strategies.basis_trade import BasisTradeStrategy
from src.strategies.breakout import BreakoutStrategy
from src.strategies.cross_exchange_arb import CrossExchangeArbStrategy
from src.strategies.funding_carry import FundingCarryStrategy
from src.strategies.mean_reversion import MeanReversionPairsStrategy
from src.strategies.options_carry import OptionsCarryStrategy
from src.strategies.registry import (
    DuplicateStrategyError,
    StrategyProtocol,
    StrategyRegistry,
    get_default_registry,
)
from src.strategies.signal_engine_adapter import SignalEngineStrategy
from src.strategies.xsec_momentum import CrossSectionalMomentumStrategy


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """One registrable strategy family and the config that governs it."""

    strategy_id: str
    factory: Callable[[float], StrategyProtocol]
    enabled_attr: str
    fraction_attr: str


# Order is stable and meaningful: the model-driven signal engine is the
# incumbent and is listed first so it is registered even if a later
# factory raises.
_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec(
        strategy_id="signal_engine_v1",
        factory=SignalEngineStrategy,
        enabled_attr="signal_engine_enabled",
        fraction_attr="signal_engine_fraction",
    ),
    StrategySpec(
        strategy_id="mean_reversion_pairs_v1",
        factory=MeanReversionPairsStrategy,
        enabled_attr="mean_reversion_enabled",
        fraction_attr="mean_reversion_fraction",
    ),
    StrategySpec(
        strategy_id="breakout_volume_v1",
        factory=BreakoutStrategy,
        enabled_attr="breakout_enabled",
        fraction_attr="breakout_fraction",
    ),
    StrategySpec(
        strategy_id="funding_carry_v1",
        factory=FundingCarryStrategy,
        enabled_attr="funding_carry_enabled",
        fraction_attr="funding_carry_fraction",
    ),
    StrategySpec(
        strategy_id="xsec_momentum_v1",
        factory=CrossSectionalMomentumStrategy,
        enabled_attr="xsec_momentum_enabled",
        fraction_attr="xsec_momentum_fraction",
    ),
    StrategySpec(
        strategy_id="basis_trade_v1",
        factory=BasisTradeStrategy,
        enabled_attr="basis_trade_enabled",
        fraction_attr="basis_trade_fraction",
    ),
    StrategySpec(
        strategy_id="cross_exchange_arb_v1",
        factory=CrossExchangeArbStrategy,
        enabled_attr="cross_exchange_arb_enabled",
        fraction_attr="cross_exchange_arb_fraction",
    ),
    StrategySpec(
        strategy_id="options_carry_v1",
        factory=OptionsCarryStrategy,
        enabled_attr="options_carry_enabled",
        fraction_attr="options_carry_fraction",
    ),
)


def enabled_specs(cfg: StrategyPortfolioSettings) -> tuple[StrategySpec, ...]:
    """Specs whose enabled flag is set, in declaration order."""
    return tuple(spec for spec in _SPECS if getattr(cfg, spec.enabled_attr))


def total_enabled_fraction(cfg: StrategyPortfolioSettings) -> float:
    """Sum of capital fractions across enabled strategies."""
    return sum(float(getattr(cfg, spec.fraction_attr)) for spec in enabled_specs(cfg))


def register_default_strategies(
    registry: StrategyRegistry | None = None,
    cfg: StrategyPortfolioSettings | None = None,
) -> tuple[str, ...]:
    """
    Register every config-enabled strategy into ``registry``.

    Returns the tuple of registered strategy_ids, in registration order.

    Idempotent: a strategy_id already present is left alone rather than
    raising, so calling this twice (e.g. API lifespan plus a test fixture)
    does not blow up an otherwise healthy process.

    Raises ValueError when the enabled capital fractions sum above 1.0 —
    that is a config error which would let the portfolio commit more than
    the book, and it must fail at startup rather than at the first fill.
    """
    registry = registry if registry is not None else get_default_registry()
    cfg = cfg if cfg is not None else get_settings().strategy_portfolio

    total = total_enabled_fraction(cfg)
    if total > 1.0:
        raise ValueError(
            f"Enabled strategy capital fractions sum to {total:.3f} (> 1.0) — "
            f"the portfolio would commit more than the whole book. Lower the "
            f"STRATEGY_*_FRACTION values or disable a strategy."
        )

    registered: list[str] = []
    for spec in enabled_specs(cfg):
        if spec.strategy_id in registry:
            log.debug("strategy_bootstrap.already_registered", strategy_id=spec.strategy_id)
            continue
        strategy = spec.factory(float(getattr(cfg, spec.fraction_attr)))
        try:
            registry.register(strategy)
        except DuplicateStrategyError:  # pragma: no cover - guarded by the check above
            continue
        registered.append(spec.strategy_id)

    log.info(
        "strategy_bootstrap.complete",
        registered=registered,
        total_capital_fraction=round(total, 4),
        registry_size=len(registry),
    )
    return tuple(registered)
