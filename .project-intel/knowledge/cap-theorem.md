# CAP Theorem & Distributed Systems
**Domain**: devops | **Tags**: cap, consistency, availability, partition, distributed, database, eventual, strong

## CAP Theorem — Architecture Reference

### For this project's storage choices
SQLite (current): CP — consistent, partition-tolerant, not available under write lock
TimescaleDB (migration target): CP — PostgreSQL consistency model
QuestDB: AP — available, eventually consistent — acceptable for market data

### Decision matrix for Trade Bot
Trade execution data: requires C (consistency) — wrong position size is worse than downtime
Market data (bars): AP acceptable — stale bar is recoverable
Audit log: CP required — incomplete audit is a compliance risk

### Practical implication: SQLite WAL mode (current)
WAL: readers don't block writers, writers don't block readers
Bottleneck: single writer at a time → contention under 3 concurrent timeframes
Mitigation: async queue for writes (single writer coroutine) — already in storage.py pattern

### Migration trigger (GAP-006)
When: adding 2nd symbol OR live trading with >100 trades/day
To: TimescaleDB (PostgreSQL) — drop-in replace for storage.py with asyncpg
