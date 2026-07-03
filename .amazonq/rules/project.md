# Trade Bot — Amazon Q Rules

## Security (enforced on every endpoint)
- All API endpoints require `X-API-Key` header
- `/execution-mode` requires additional `OPERATOR_SECRET` (second factor)
- CORS wildcard `*` is forbidden — specify explicit origins
- Never expose API without TLS termination outside loopback

## Testing
- `pytest tests/ -v` — 60% coverage minimum enforced
- `invalidate_settings_cache()` between test cases
- Tests in `tests/` — mirror `src/` structure

## Code rules
- `get_settings()` for all config — never `os.getenv()`
- `StorageBackend` is the only persistence layer
- `asyncio.Lock` in async code — never `threading.Lock`
- `structlog` for logging — never `print()` or stdlib logging
- `TRADING_MODE=live` in `.env` only — cannot be set programmatically
