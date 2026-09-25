# Key rotation

Every secret handled by Trade-Bot has a rotation procedure. Rotate on a
schedule and immediately on suspected compromise.

## Inventory

Read from `.env` (never checked in). Prefixes and their sources:

| Env var                                | Owner          | Rotation                                                                  |
| -------------------------------------- | -------------- | ------------------------------------------------------------------------- |
| `BINANCE_API_KEY` / `_SECRET`          | Binance        | Console → API Management → recreate key pair, IP-restricted.              |
| `OKX_API_KEY` / `_SECRET` / `_PASSPHRASE` | OKX         | API Management → new key, restrict to `trade` + `read` (never `withdraw`).|
| `INTELLIGENCE_GLASSNODE_API_KEY`       | Glassnode      | Account portal → regenerate.                                              |
| `INTELLIGENCE_CRYPTOQUANT_API_KEY`     | CryptoQuant    | Account portal → regenerate.                                              |
| `API_SECRET_KEY` / `API_READONLY_KEY`  | you            | `openssl rand -hex 32`.                                                    |
| `STORAGE_TIMESCALE_DSN` password       | you            | Rotate DB user password (`ALTER USER tradebot PASSWORD ...`).             |

## Procedure (per key)

1. **Generate the new value** in the provider console. For exchange keys,
   restrict permissions: `trade` + `read` only. **Never enable withdraw**.
   Whitelist the deployment's IP.
2. **Stage without downtime**: write the new value into `.env` and
   reload — for compose, `docker compose up -d --force-recreate api` (or
   worker). For systemd, `systemctl restart tradebot-api`.
3. **Verify**: check `GET /health` returns 200; watch
   `tradebot_gate_block_total` for auth-failure signatures over the next
   5 minutes.
4. **Revoke the old key** in the provider console. Do not skip this — a
   dangling old key is the whole reason for rotation.
5. **Record the rotation** in the operator log: date, key name, who did it.

## Schedule

- Exchange API keys: every 90 days.
- Provider data keys (Glassnode / CryptoQuant): every 180 days.
- `API_SECRET_KEY`: every 90 days, or immediately on any suspected
  compromise (leaked in a screenshot, laptop stolen, ex-employee).
- DB password: every 180 days.

## Compromise response

If any key may have leaked:

1. **Revoke first**, then investigate. The order matters.
2. If it was an exchange key: check the exchange audit log for orders
   you did not place, then reconcile
   (`GET /debug/reconcile`).
3. If it was `API_SECRET_KEY`: rotate immediately and audit
   `logs/AUTOMATED_DECISION_LOG.md` for any `POST /execution-mode` or
   `POST /approvals/*/resolve` calls you did not authorize.
