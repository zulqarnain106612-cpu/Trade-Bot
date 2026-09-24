# On-call runbook

Operational reference for running Trade-Bot in paper or live mode. Written
against the code in this tree — links point at the actual modules that own
each behavior. Update this file when they move.

## Topology

- **api** ([src/api/__main__.py](../../src/api/__main__.py)) — FastAPI server.
  Its lifespan owns the trading loop by default. Authoritative in the
  provided compose ([docker-compose.yml](../../docker-compose.yml)) and
  systemd ([deploy/systemd/tradebot-api.service](../../deploy/systemd/tradebot-api.service))
  configs.
- **worker** ([src/workers/__main__.py](../../src/workers/__main__.py)) —
  headless trading loop, opt-in via the compose `worker` profile or the
  `tradebot-worker.service` systemd unit. Never enable both loops against
  the same DB.
- **storage** — TimescaleDB (compose) or SQLite (dev). Chosen by
  `STORAGE_BACKEND` ([src/config.py](../../src/config.py) `StorageSettings`).

## First boot

1. Copy `.env.example` to `.env`; fill in the REQUIRED sections
   (`BINANCE_*`, `OKX_*` if used, `INTELLIGENCE_*` if you have Glassnode).
2. `python scripts/backfill.py --lookback-days 180` — pre-populate
   history so the first tick has features to compute.
3. `python scripts/check_model_artifacts.py` — heuristic check for
   trained weights. If missing, first startup will train from history
   (can be slow).
4. `make docker-up` — brings up TimescaleDB + API.
5. `curl -H "X-API-Key: $API_SECRET_KEY" http://127.0.0.1:8000/health`.

## Metrics

`/metrics` (Prometheus) is behind the `X-API-Key` header check
([src/api/main.py](../../src/api/main.py) line ~646). Prometheus itself
cannot inject that header, so either:

- Front the API with a reverse proxy that injects the key on the
  `/metrics` path (recommended), or
- Run a metrics-only sidecar that hits `/metrics` with the key and
  re-exposes on an internal port.

Scrape config: [deploy/prometheus.yml](../prometheus.yml).
Alert rules: [deploy/alerts.yml](../alerts.yml).

Exposed metrics live in [src/api/metrics.py](../../src/api/metrics.py) —
`tradebot_signal_score`, `tradebot_equity_usd`, `tradebot_kelly_fraction`,
`tradebot_gate_{pass,block}_total`, `tradebot_tick_duration_seconds`, etc.

## Kill switch

The capital-preservation floor
([src/risk/capital_preservation_floor.py](../../src/risk/capital_preservation_floor.py))
halts trading permanently when peak-equity drawdown exceeds
`RISK_CAPITAL_PRESERVATION_MAX_DRAWDOWN_PCT` (default 0.30 = 30%). Unlike the
daily halt, it does **not** auto-clear at midnight or on equity recovery.

To re-authorize after a kill-switch trip:

1. Investigate the root cause. Read `logs/AUTOMATED_DECISION_LOG.md`.
2. Verify positions have been reconciled with the exchange
   (`GET /debug/reconcile`).
3. Call `re_authorize()` on the floor via an operator entrypoint (see
   `capital_preservation_floor.py`) — this is intentionally out-of-band
   and not exposed as an API route.

## Common alerts

- **TradebotDown** — API/loop unreachable. `systemctl status tradebot-api`,
  check `journalctl -u tradebot-api`, verify DB is up.
- **EquityDrop** — >5% drop in 1h. Check open positions
  (`GET /status`), consider `POST /execution-mode` → `manual` while
  investigating.
- **GateBlockSpike** — a specific gate is persistently blocking. Check
  the gate name in the alert label; correlate with recent regime state.
- **ModelAccuracyDegraded** — rolling win-rate < 0.45 for 30m. Consider
  triggering retrain via the tuning path, or kill-switching the
  underperforming strategy.

## Shutdown

- API: `systemctl stop tradebot-api` — the lifespan gets ≤10s to persist
  equity snapshot, then the orchestrator task is cancelled. `TimeoutStopSec=30s`.
- Worker: `systemctl stop tradebot-worker` — same 10s inside
  `src/workers/trader.py`.

Never `kill -9` the loop. That skips the equity snapshot and the exchange
account will be out of sync with in-memory state on next boot.

## Escalation

If you have to page a human:

1. Note the timestamp UTC and the alert.
2. Freeze trading first: `POST /execution-mode {"mode": "manual"}`.
3. Then investigate. In-flight orders are not cancelled by mode changes
   — call `/positions/close-all` if needed.
