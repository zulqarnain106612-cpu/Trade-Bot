# Money and time

Two representations that are easy to get wrong quietly, and that this project
therefore states once rather than deciding per module.

`DATA-003` (time) and `DATA-004` (money) in
`config/quality_registry.json`. Enforced by
`tests/component/test_clock_policy.py` and
`tests/component/test_money_representation.py`.

---

# Part 1 — Money

## 1. The representation, stated plainly

**This project uses IEEE-754 `float64` for money, not `Decimal`.**

That is a real decision with real costs, and stating it honestly is more
useful than an aspirational "we use Decimal" that the tree does not support.
`src/execution/unified_ledger.py`, `src/risk/kelly.py`, the executors and the
whole feature and signal path are float end to end.

The reasons it is defensible here:

- Every price and quantity arrives from the venue as a JSON number and is
  parsed by `ccxt` into a float. Converting to `Decimal` at that point buys
  exactness over a value that was already rounded by the venue's own
  serialisation.
- Crypto notionals are far inside float64's 15–17 significant decimal digits.
  A $10,000,000 position sized to eight decimal places of BTC uses about 16
  digits of headroom in total.
- The arithmetic is overwhelmingly multiplicative (price × quantity, fraction
  × capital) rather than a long chain of additions, which is where float drift
  actually accumulates.

The reason it is not free, and what is done about it, is the rest of this
section.

## 2. Where `Decimal` is mandatory

**At the exchange-precision boundary.** `src/risk/kelly._floor_to_precision`
quantises a quantity to the venue's decimal places using
`Decimal(str(value)).quantize(..., rounding=ROUND_DOWN)`.

Two properties of that line are load-bearing:

- **`Decimal(str(value))`, not `Decimal(value)`.** The string round-trip
  matches the human-readable value; converting the binary float directly
  carries its representation error into the decimal. The previous
  implementation, `math.floor(value * 10**n) / 10**n`, produced visible
  artifacts at eight decimal places on BTC quantities.
- **`ROUND_DOWN`, never `ROUND_HALF_UP`.** Quantising must never increase a
  quantity. Rounding up can push an order past a position limit that was
  checked before quantisation, and past the venue's balance.

## 3. The rules

1. **Quantise before comparing against a venue minimum or maximum.** The
   number the venue sees is the quantised one; checking the unquantised value
   against a minimum notional answers a question nobody asked.
2. **Never round up a quantity or a notional.** Down, or refuse.
3. **Compare money with a tolerance, never with `==`.** The tolerance is
   stated at the comparison, in the units being compared.
4. **Accumulate in one place.** `UnifiedLedger` is the single accumulator for
   position and margin; a second running total drifts independently of the
   first and the two then disagree for reasons nobody can reconstruct.
5. **Percent and fraction are different types in every way but the
   annotation.** `RiskSettings.daily_drawdown_halt_pct` is a percent (2.0
   means 2%) and `capital_preservation_max_drawdown_pct` is a fraction (0.30
   means 30%), despite the identical suffix. Both names are load-bearing —
   they are `RISK_*` environment variable names — so the units are documented
   at the field and pinned by `tests/test_percent_vs_fraction_units.py` rather
   than renamed out from under deployments.

## 4. The declared tolerance

Over a trading day's worth of fills, the ledger's accumulated position and
notional must agree with an exactly-computed reference to within **1e-9
relative**. That is roughly six orders of magnitude inside float64's own
precision and far inside any venue's reporting precision, so a breach means a
logic error rather than accumulated rounding.

`tests/component/test_money_representation.py` asserts it over a long
sequence of fills rather than on a single arithmetic expression, because a
single expression cannot fail this way.

## 5. When to revisit

If any of these becomes true, the decision above should be re-argued rather
than inherited:

- Fee and funding accrual moves to a long additive chain over months rather
  than a per-trade multiplication.
- A venue is added whose quantities exceed float64's significant digits.
- The ledger becomes the system of record for a balance that must reconcile
  exactly, to the satoshi, against an external statement.

---

# Part 2 — Time

## 6. The representation

**Aware UTC `datetime`, or an integer count of milliseconds since the Unix
epoch. Nothing else.** `src/data/clock.py` is the only sanctioned boundary
crossing, and it is deliberately small: the value is in there being exactly
one of each function.

| Need | Use |
|---|---|
| Current time | `clock.utc_now()` / `clock.utc_now_ms()` |
| Accept a datetime from outside | `clock.ensure_utc(value)` |
| Venue millisecond timestamp → datetime | `clock.from_epoch_ms(ms)` |
| Datetime → venue milliseconds | `clock.to_epoch_ms(value)` |
| Venue clock vs ours | `clock.check_exchange_skew(...)` |

## 7. The rules

1. **A naive `datetime` is refused, not assumed to be UTC.** Assuming is how
   a bar timestamp read in a developer's local zone ends up an hour or a day
   from the bar it describes, with every downstream comparison still
   succeeding.
2. **Another zone is converted, not relabelled.** Converting preserves the
   instant; `replace(tzinfo=UTC)` moves it.
3. **Local time never enters a calculation.** It may be produced for display,
   at the edge, by a caller that has decided to.
4. **There is no daylight saving.** UTC has none, which is the entire reason
   the internal representation is UTC rather than a local zone. The spring
   gap and the autumn repeated hour are both simply absent.
5. **Exchange time is not our time.** The difference is measured against a
   declared budget (5 s by default) rather than ignored. A signed request
   built against a drifted clock is rejected by the venue, and the rejection
   says nothing about clocks — so the check has to be ours.
6. **Bar timestamps are the bar's *open*, in epoch milliseconds, ascending.**
   Whether the most recent bar is closed is a separate question, answered by
   comparing against the timeframe interval — see `SignalEngine.tick`.

## 8. What the data-quality gate enforces

`src/data/quality_gate.py` refuses a frame whose timestamps are unparseable,
non-monotonic, duplicated, or older than the declared freshness budget
(`DATA-001`, `DATA-002`, `INV-008`). The budget is derived from the bar
interval rather than fixed, because the newest *closed* bar on an N-second
timeframe is always at least N seconds old — a fixed five-minute budget
rejects every well-formed 15-minute frame, which is why the gate could not
simply be switched on as it stood.
