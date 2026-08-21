# Real-Time Systems Reference

## Architecture Pattern: Event-Driven Pipeline

```
Market Data Feed
    │
    ▼
Normalizer (canonical format, validation, timestamp, spike filter)
    │
    ▼
Feature Engine (indicators, microstructure, on-chain, OFI)
    │
    ▼
Signal Engine (per-strategy signals, ensemble, regime gate)
    │
    ▼
Risk Engine (pre-trade: VaR, CVaR, drawdown, correlation, compliance, flash-crash gate)
    │
    ▼
Order Manager (submission, idempotency, tracking, reconciliation, wash-guard)
    │
    ▼
Exchange API Layer (CEX/DEX adapters, FIX/REST/WS, venue health scoring)
    │
    ▼
Audit Logger (immutable, HMAC-signed, append-only event log, sequence-numbered)
```

### Design Rules
- Each stage: typed messages (Protobuf/FlatBuffers), no shared mutable state
- Back-pressure propagates upstream — downstream slowness slows ingestion, not crash
- Every stage: configurable queue depth; overflow = alert, not silent drop
- No synchronous calls in hot path — async/event-driven only
- Dead letter queue (DLQ): failed messages routed; never silently dropped
- Each stage emits latency histograms; breach of per-stage SLA = alert

---

## LMAX Disruptor Pattern

### Mechanical Sympathy Design
- Ring buffer: pre-allocated, power-of-2 size; avoids GC in hot path
- Sequence number: single atomic `long`; CAS for producer claim; no locks
- Cache line padding: prevent false sharing — pad sequence to 64 bytes each side
- Wait strategies:
  - `BusySpinWait`: lowest latency; burns CPU; use only on dedicated core
  - `YieldingWait`: slightly higher latency; yields CPU between checks
  - `BlockingWait`: lowest CPU; use for low-frequency stages

```
Producer → Ring Buffer → [Normalizer] → [Feature Engine] → [Signal Engine] → ...
                              ↑ Sequence tracking; no locks after initial claim
```
- Throughput: 6M+ events/sec on commodity hardware

---

## Rust Async for Latency-Critical Components (v3)

### Why Rust for Hot Path
- Zero-cost abstractions: async/await compiles to state machines; no runtime overhead
- No GC pauses: eliminates latency spikes from garbage collection
- Memory safety without GC: ownership model prevents data races at compile time
- `tokio`: production-grade async runtime; work-stealing scheduler; io_uring backend (Tokio uring)

### Tokio Architecture Patterns
```rust
use tokio::sync::mpsc;
use tokio::time::{interval, Duration};

// Multi-producer single-consumer channel for order pipeline
let (tx, mut rx) = mpsc::channel::<Order>(1024);

// Dedicated tokio runtime for hot path — separate from management tasks
let rt = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(4)
    .thread_name("trade-hot")
    .enable_io()
    .enable_time()
    .build()?;
```

### Lock-Free Structures in Rust
- `crossbeam-queue`: lock-free MPMC queue; ArrayQueue for bounded, SegQueue for unbounded
- `dashmap`: concurrent HashMap; `RwLock<HashMap>` is 2–10× slower under contention
- `arc-swap`: atomic swap of Arc references; zero-copy reader update
- `std::sync::atomic`: Relaxed ordering for metrics counters; SeqCst only where total
  order required (risk gates, sequence numbers)

### async-std vs tokio
- tokio: preferred for production; larger ecosystem; Axum, Hyper, reqwest
- async-std: simpler API; use for single-purpose daemons without complex dependency tree
- Never mix runtimes in same binary; spawning tokio tasks from async-std panics

---

## Kernel Bypass Networking

### DPDK (Data Plane Development Kit)
- Bypasses Linux kernel network stack; userspace NIC driver
- Polls NIC directly: interrupt latency (~10μs → ~200ns)
- PMD (Poll Mode Driver): dedicated CPU core per NIC queue — no other work
- Use case: HFT on co-located infrastructure

### Solarflare OpenOnload / Xilinx EFVI
- Kernel-bypass TCP/UDP: zero-copy socket API
- `efvi` API: raw packet access for sub-microsecond read
- RTT improvement: 50μs → 5μs typical for same-host exchange proximity

### io_uring (Linux 5.1+, updated through 6.x)
- Async I/O without syscall overhead for storage and network
- Submission queue (SQ) + completion queue (CQ): userspace ring buffers
- `IORING_OP_RECV_MULTISHOT` (Linux 5.19+): single submission, multiple completions
  — dramatically reduces overhead for WebSocket message handling
- `IORING_OP_SENDMSG_ZC` (Linux 6.0+): zero-copy send for large audit log writes
- `IORING_FEAT_NODROP`: guarantee no event loss on CQ overflow
- Use in Rust via `tokio-uring` or `rio`

### eBPF for Observability (zero-instrumentation tracing)
```c
// Example: trace TCP latency without modifying application code
SEC("kprobe/tcp_rcv_established")
int trace_tcp_recv(struct pt_regs *ctx) {
    u64 ts = bpf_ktime_get_ns();
    // Calculate RTT from timestamp stored at send
    ...
}
```
- BCC / bpftrace: Python/Go bindings for eBPF programs
- Measure: kernel-level TCP RTT, syscall latency, context switches — without
  any application code change
- Continuous profiling: Parca, Grafana Pyroscope — eBPF-based; < 1% CPU overhead
- xdp (eXpress Data Path): eBPF at NIC driver level; packet processing before kernel;
  use for rate limiting and packet filtering at line rate

---

## QUIC / HTTP3

- QUIC: UDP-based transport; multiplexed streams without head-of-line blocking
- Advantage over TCP: connection migration (IP change survives); 0-RTT reconnect;
  independent stream delivery (one lost packet blocks only that stream, not all)
- Exchange adoption: early stages; Bybit, Coinbase evaluating; monitor per exchange
- TLS 1.3 integrated: no separate TLS handshake; QUIC encrypts by default
- `quinn` (Rust), `quiche` (Cloudflare, C/Rust): production-ready libraries
- Latency: 0-RTT reconnect is critical after WebSocket drop; if exchange supports QUIC,
  reconnect latency drops from ~50ms (TCP+TLS) to < 5ms (0-RTT)

---

## Latency Budget

### Targets by Strategy Type
| Strategy | Signal→Order Budget | p99 Target |
|---|---|---|
| HFT (co-located, kernel bypass) | < 1 ms | < 500 μs |
| Latency arbitrage | < 10 ms | < 5 ms |
| Day trading | < 500 ms | < 200 ms |
| Swing | < 60 s | < 30 s |

### Measurement Points (mandatory)
```
t0: market data received (NIC timestamp preferred)
t1: normalized + validated
t2: features computed
t3: signal computed
t4: risk check complete (VaR + confidence gate + compliance)
t5: order serialized and submitted
t6: exchange acknowledgement received
t7: fill notification received
```
- Histogram (p50/p95/p99) per stage; latency spike > 3× p99 → alert
- `time.monotonic_ns()` for measurements; wall clock for event timestamps

---

## Zero-Copy Architecture

### Shared Memory IPC
```python
import mmap, struct
shm = mmap.mmap(fd, size=4096)
shm.write(struct.pack("!ddd", bid, ask, timestamp_ns))  # zero-copy write
bid, ask, ts = struct.unpack("!ddd", shm.read(24))      # consumer: no copy
```
- `shm_open` + `mmap`: inter-process on same host; < 1μs latency

### FlatBuffers / Cap'n Proto
- Zero-copy deserialization: read fields directly from wire buffer without parsing
- FlatBuffers: better for access patterns where only a few fields are read per message
- Cap'n Proto: zero-copy RPC; streaming; slightly more complex but lower latency
- Protobuf: avoid in hot path; requires allocation + full deserialization

---

## Message Queue Architecture

| Queue | Latency | Persistence | Use Case |
|---|---|---|---|
| In-process ring buffer (Disruptor) | ~100 ns | No | Hot path intra-process |
| Shared memory (shm) | ~200 ns | No | Inter-process, same host |
| ZeroMQ (nanomsg) | ~10 μs | No | Inter-process, low latency |
| Redis Streams | ~1 ms | Yes | Cross-host, audit trail |
| Kafka | ~5 ms | Yes | Multi-consumer, replay, audit |
| Redpanda | ~2 ms | Yes | Kafka-compatible; lower latency |

- **Hot path**: Disruptor or shm only
- **Audit log**: Kafka / Redpanda (ordered, replayable, durable)
- **Strategy fan-out**: ZeroMQ PUB/SUB
- Queue depth alert at 80%; circuit open at 100%

---

## Fault Tolerance Patterns

### Circuit Breaker
```
CLOSED → [N failures in window] → OPEN → [timeout] → HALF-OPEN → [success] → CLOSED
```
- Apply to: all exchange API calls, external data feeds, vault calls, blockchain RPC

### Retry with Exponential Backoff + Jitter
```python
delay = min(cap, base * (2 ** attempt)) + random.uniform(0, jitter)
# Base: 100ms, Cap: 60s, Jitter: ±20%
```
- Idempotency key mandatory before retrying any order submission

### Graceful Degradation
- Feed lag > threshold → switch to cached/synthetic; log DEGRADED
- Exchange API down → halt new orders; manage existing positions; alert
- Risk engine failure → suspend all auto-execution; manual-only mode
- Flash crash detected → halt new entries; log FLASH_CRASH_HALT; reassess after 10s

---

## State Management

### Persistent State (must survive restart)
Open positions, orders, strategy state, account balances per exchange

### Storage Stack
- Primary: Redis (in-memory, pub/sub for change events)
- Backup: PostgreSQL (ACID, audit history) or TimescaleDB for time-series state
- Write-through: Redis → async replicate to Postgres
- WAL-based state: Postgres WAL = RPO ≤ 5s for order state

### Startup Reconciliation (mandatory)
```
1. Load positions from local DB
2. Fetch from exchange API
3. Diff; source of truth = exchange
4. Resolve: missing local → add + WARNING; quantity mismatch → use exchange + CRITICAL
5. Resume execution only after reconciliation confirmed
```

---

## Clock and Time Synchronization

- All timestamps: UTC, nanosecond precision
- NTP: max drift 100ms; chrony preferred over ntpd
- PTP (IEEE 1588): for < 1ms strategies — sub-microsecond sync; hardware timestamping
- Monotonic clock (`time.monotonic_ns()`) for latency; wall clock for event timestamps
- Exchange timestamps: record; compare to local; `latency = local_recv - exchange_ts`
