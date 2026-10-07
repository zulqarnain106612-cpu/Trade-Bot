# Realtime GUI — current state and ordered work

Not generated. Edit this file directly.

## The finding

The GUI has no push path. `/ws` looks like a live feed but is a timer:
`while True: await asyncio.sleep(ws_heartbeat_s)`, running once per
connected client. Nothing in the process could cause a push, so a fill
landing ten milliseconds after a tick waited out the rest of the period.
Every other surface — 16 panels — is independent `setInterval` HTTP
polling at 10–60s.

Raising the tick rate does not fix this. It trades latency for load and
leaves the floor where it is, because the floor is the timer, not the
producer.

## Defects, and where each one stands

| # | Defect | Status |
|---|--------|--------|
| 1 | No event bus — `event_bus\|EventBus\|pubsub\|asyncio.Queue` had zero hits under `src/`, so both transports were timers and latency was floored by the period regardless of producer speed | `src/eventbus/` (GOV-032/033/034) |
| 2 | Per-client snapshot build — each of up to 50 clients runs its own `storage.latest_regime()` and `json.dumps` for identical bytes | open |
| 3 | Fixed 6-field payload — equity, cash, positions, approvals, two modes, regime. The other ~16 streams have no push path | open |
| 4 | `src/data/orderbook_stream.py` is dead in production — a working Binance WS book/aggTrade client referenced only from `tests/`. No `src/` module imports it. The one sub-second price source in the tree feeds nothing, and mark-to-market waits on a 5s REST loop | open |
| 5 | Reconnect was lossy — flat `setTimeout(connect, 3000)`, no backoff, no jitter, no resync, and a loop that outlived the component | fixed (REG-0019) |

## Worst-case lag as measured

Positions ~10s (5s monitor plus 5s heartbeat, unsynchronised) ·
approvals ≤5s · drift, reconcile and attribution ≤30s · gauntlet ≤60s ·
risk-gate blocks and kill-switch trips **never pushed** · signal and
regime ≤15 min.

The last one is bar-close bound: that part is the strategy, not the
transport, and no change to the transport moves it.

## Wired subsystems with no GUI surface

Zero matches in `frontend/src` for `/intelligence/coverage`,
`/intelligence/providers`, `/performance-drift`,
`/strategies/stress-test`, `/orders/{id}/status`, `/debug/selftest`.

The whole intelligence layer is invisible. It fails open by design, so a
degraded provider quietly cuts signal confidence with no indication
anywhere — the failure and the absence of a panel compound.

## Ordered work

1. **Event bus with drop-oldest, plus a single broadcaster.** The GUI
   must never backpressure the trading loop. *Bus landed; the
   broadcaster is item 2 above and still open.*
2. **Frontend `useStream`** replacing the 16 timers.
3. **Wire `orderbook_stream.py`** for sub-second mark-to-market.
4. **Topic subscriptions, the missing panels, a lag histogram.**

## Constraints that shape the work

- A new `engine -> api` edge trips `arch_gate` (GOV-018) unless declared
  in the wiring contract. The bus avoids it by living in `foundation`,
  which every layer may import downward.
- GOV-016 forbids `sleep` in new tests, so bus tests use
  `asyncio.Event` handshakes and an injected clock.
- Frontend behaviour needs a deciding test (GOV-035); `npm run build`
  proves nothing about lifecycle.

## What "fully operational" needs, which is mostly not code

The live gate wants 30 banked paper days, `oos_sharpe > 1.5`, and
sandbox drills that deliberately trip the capital floor and the kill
switch. Models exist for all three timeframes, so the signal path itself
is ready to soak — the remaining distance is evidence, not
implementation.
