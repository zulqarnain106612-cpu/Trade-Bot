# Crypto-Intel v6 Integration Plan — Trade-Bot

Source file: `/home/fujitsu/Projects/Crypto-Box/crypto intel v6.md`
Every line from the source is accounted for below. Items already present in `src/` are marked **ALREADY**; items not yet implemented are in the implementation plan.

---

## Legend
- **ALREADY** — functionality exists in the current codebase
- **NEW** — must be implemented

---

## 1. Overview Claims (lines 3–14)

| Claim | Status |
|---|---|
| 10 independent temporal horizons | **NEW** — horizons not yet modelled; current engines are single-horizon |
| Each horizon has its own model, feature pipeline, ECC ops, causal graph, risk gate, retrain schedule, observability workers | **NEW** |
| Elliptic curve analysis (secp256k1, ECDSA r/s, Schnorr/Taproot, UTXO curvature, zkSNARK) | **NEW** — `coincurve` is in `requirements.lock` but no ECC analysis modules exist |
| 12-head neural ensemble (CNN, TCN, TFT, LSTM, GRU, BERT, GNN, MLP, N-BEATS, PatchTST, Conformer, ECC-head) | **NEW** — current trainer is XGBoost only |
| Cross-attention fusion → 10-output meta-network | **NEW** |
| Self-upgrade loop (ADWIN drift → MAML → Optuna walk-forward → shadow A/B deploy) | **NEW** — `optuna` exists (`pyproject.toml`) but no ADWIN/MAML/shadow-deploy pipeline |
| Zero cloud: Redpanda + TimescaleDB + DuckDB + Hopsworks OSS + Neo4j + local Bitcoin node | Partial — **ALREADY** TimescaleDB (`src/data/timescale_storage.py`); **NEW** Redpanda bus, DuckDB OLAP, Hopsworks OSS, Neo4j, local Bitcoin node |

---

## 2. Architecture — Data Ingestion (lines 21–33)

| Component | Status |
|---|---|
| Exchange feeds via ccxt WebSocket | **ALREADY** — `src/data/orderbook_stream.py` uses ccxt WS |
| Redpanda bus (Kafka-compat) between feeds and storage | **NEW** — no Kafka/Redpanda client in codebase |
| TimescaleDB tick + OHLCV hypertables | **ALREADY** — `src/data/timescale_storage.py` |
| DuckDB OLAP / backtests store | **NEW** — DuckDB not in project |
| Feature: Microstructure — OFI, VPIN, Kyle λ | Partial — **ALREADY** OFI/imbalance (`e02_microstructure.py`); **NEW** VPIN and Kyle lambda |
| Feature: On-chain — SOPR, NVT, MVRV (local node) | Partial — **ALREADY** on-chain proxy via DeFi Llama/CoinGecko (`e05_onchain.py`); **NEW** local Bitcoin node RPC, SOPR/NVT/MVRV from node |
| Feature: Derivatives — OI, funding, liquidations | **ALREADY** — `src/data/deribit_provider.py`, `e12_options.py`, `strategies/funding_carry.py` |
| Feature: NLP — CryptoBERT 110M CPU | Partial — **ALREADY** sentiment via `e14_sentiment.py`; **NEW** CryptoBERT 110M model integration (BERT head) |
| Feature: Cross-asset — DXY, gold, SPX, BTC.D | **ALREADY** — `src/data/macro_provider.py`, `src/intelligence/macro_indicators.py` |
| Feature: Options — IV skew, put/call ratio | **ALREADY** — `e12_options.py`, `src/risk/greeks.py` |
| Feature: Mempool — fee pressure, tx count | **NEW** — no mempool data source connected |

---

## 3. ECC Analysis Layer (lines 34–195)

### 3.1 secp256k1 Address Clustering (lines 97–113)
| Item | Status |
|---|---|
| `coincurve` C-binding dependency | **ALREADY** — `coincurve==21.0.0` in `requirements.lock` |
| `graphsense-lib` / AddressClusterer | **NEW** |
| Local Bitcoin node RPC (`listunspent`) | **NEW** |
| Common-input-ownership heuristic clustering | **NEW** |
| Whale cluster filtering (>100 BTC) | **NEW** |
| `cluster_flow_score ∈ [-1, +1]` emitted to feature bus | **NEW** |
| File: `src/ecc/secp256k1_cluster.py` | **NEW** |

### 3.2 ECDSA r/s Nonce Reuse Detection (lines 115–138)
| Item | Status |
|---|---|
| `fastecdsa` dependency | **NEW** |
| Custom DER decoder (`extract_ecdsa_signatures`) | **NEW** |
| `r_registry` dict per-r accumulation | **NEW** |
| Private key extraction from nonce reuse (`compute_privkey_from_reuse`) | **NEW** |
| `ecc.ecdsa.weakness` event on feature bus | **NEW** |
| Weakness score → short signal for exchange addresses | **NEW** |
| File: `src/ecc/ecdsa_scan.py` | **NEW** |

### 3.3 Schnorr / Taproot Pattern Detection (lines 140–159)
| Item | Status |
|---|---|
| `bitcoin-lib-py>=0.11` dependency | **NEW** |
| `is_p2tr()` Taproot input detector | **NEW** |
| `detect_musig2_cosigners()` batch Schnorr verify | **NEW** |
| Privacy score + smart money divergence computation | **NEW** |
| Output routed to 4h and 1W horizons | **NEW** |
| File: `src/ecc/schnorr_taproot.py` | **NEW** |

### 3.4 UTXO Curve — Hodler Index (lines 161–177)
| Item | Status |
|---|---|
| `compute_hodler_index(utxo_set)` — exponential age decay | **NEW** |
| `supply_shock_proxy` (hodler_index > 0.75 → reduce short exposure) | **NEW** |
| File: `src/ecc/utxo_curve.py` | **NEW** |

### 3.5 zkSNARK Privacy Flow Inference (lines 179–195)
| Item | Status |
|---|---|
| `py_ecc` dependency (bn128 pairing) | **NEW** |
| `web3` dependency | **NEW** |
| Tornado Cash deposit detection | **NEW** |
| BFS withdrawal graph (depth=3) cluster trace | **NEW** |
| `dark_pool_pressure ∈ [0,1]` → 1D and 1W risk overlay | **NEW** |
| File: `src/ecc/zksnark_detect.py` | **NEW** |

---

## 4. Neural Ensemble (lines 199–268)

### 4.1 Model Registry — 12 heads (lines 203–216)
| Model | Architecture | Status |
|---|---|---|
| CNN | 1D dilated conv, kernel [3,5,7], BN, ReLU; input: L2 order book 100-level | **NEW** |
| TCN | Causal dilated conv, residual blocks, dropout; input: trade flow sequence | **NEW** |
| TFT | LSTM encoder + variable selection + MHA; input: OHLCV + covariates | **NEW** |
| LSTM | 2-layer + attention gate, hidden=256; input: derivatives slow signal | **NEW** |
| GRU | 2-layer + regime conditioning; input: on-chain slow signal | **NEW** |
| BERT | CryptoBERT 110M, mean pool [CLS], proj 768→128; input: news + social text | **NEW** |
| GNN | 3-layer GAT, 8 heads, edge dropout 0.1; input: asset correlation graph | **NEW** — `e18_network.py` has contagion graph but not GAT |
| MLP | 4-layer residual, LayerNorm, GELU; input: macro dense features | **NEW** |
| N-BEATS | Trend + seasonality + residual stacks; input: OHLCV univariate | **NEW** |
| PatchTST | Patch len=16, stride=8, 6-layer transformer; input: OHLCV multivariate | **NEW** |
| Conformer | Conv + rel-pos MHA, 4 blocks; input: HFT microstructure | **NEW** |
| ECC-head | 2-layer MLP → 128-dim, LayerNorm; input: ECC feature vector | **NEW** |
| Files: `src/models/cnn.py`, `tcn.py`, `tft.py`, `lstm.py`, `gru.py`, `bert_head.py`, `gnn_head.py`, `mlp.py`, `nbeats.py`, `patchtst.py`, `conformer.py`, `ecc_head.py` | **NEW** |

### 4.2 Cross-Attention Fusion (lines 218–240)
| Item | Status |
|---|---|
| `CrossAttentionFusion` module (12→1) | **NEW** |
| Regime vector → query via `nn.Linear(64, d_model)` | **NEW** |
| `nn.MultiheadAttention(d_model=128, num_heads=8, dropout=0.1)` | **NEW** |
| Sparse gating per head via sigmoid | **NEW** |
| Learnable ECC anomaly boost scalar (`nn.Parameter`) | **NEW** |
| ECC-head (index 11) scaled by `ecc_boost * ecc_anomaly` | **NEW** |
| Returns `[B, 128]` fused embedding + `[B, 1, 12]` attention weights | **NEW** |
| File: `src/fusion/cross_attention.py` | **NEW** |

### 4.3 Meta-Network — 10 Output Heads (lines 242–267)
| Item | Status |
|---|---|
| `MetaNetwork` with shared `Linear(128,256)+GELU+LayerNorm` | **NEW** |
| `direction` head per horizon: `Linear(256,3)` + softmax (CE loss) | **NEW** |
| `magnitude` head per horizon: `Linear(256,2)` → μ + log σ (Gaussian NLL) | **NEW** |
| `timing` head per horizon: `Linear(256,1)` + sigmoid (CE loss) | **NEW** |
| Joint loss: `CE(direction) + GaussNLL(magnitude) + CE(timing)` | **NEW** |
| Optimizer: `AdamW(lr=3e-4, weight_decay=1e-2)`, `grad_clip=1.0` | **NEW** |
| File: `src/fusion/meta_network.py` | **NEW** |

---

## 5. Horizon Configuration (lines 79–91)

| Horizon | Label | Models | ECC Op | Retrain | Min Workers | Status |
|---|---|---|---|---|---|---|
| h1 | 30s | CNN + TCN | secp256k1 clust | hourly | 2 | **NEW** |
| h2 | 2m | TCN + LSTM | ECDSA r/s scan | 2h | 2 | **NEW** |
| h3 | 5m | TFT + CNN | secp256k1 + Schnorr | 4h | 1 | **NEW** |
| h4 | 15m | TFT + GRU | UTXO curvature | 6h | 1 | **NEW** |
| h5 | 1h | TFT + PatchTST | Schnorr Taproot | daily | 1 | **NEW** |
| h6 | 4h | LSTM + N-BEATS | UTXO + cluster | daily | 1 | **NEW** |
| h7 | 1D | N-BEATS + MLP | full ECC suite | weekly | 1 | **NEW** |
| h8 | 3D | MAML + MLP | zkSNARK flows | weekly | 1 | **NEW** |
| h9 | 1W | Conformer + GRU | full + addr graph | biweekly | 1 | **NEW** |
| h10 | 1M | MAML + MLP + GNN | deep ECC struct | monthly | 1 | **NEW** |
| Config file: `config/horizons.yaml` | **NEW** |

---

## 6. Worker Orchestrator (lines 273–332)

| Item | Status |
|---|---|
| `WorkerOrchestrator` class | **NEW** — `src/engine/crypto_box_adapter.py` exists but is only an adapter stub, not a full orchestrator |
| `MIN_WORKERS=2`, `MAX_WORKERS=24` | **NEW** |
| `mp.Queue(maxsize=1000)` for queue_in and queue_out | **NEW** |
| Multiprocessing horizon model workers | **NEW** |
| NUMA locality pinning via `os.sched_setaffinity` | **NEW** |
| Dedicated ECC thread (`threading.Thread`) — GIL released via coincurve C ext | **NEW** |
| `SIGTERM` handler in `_model_worker` | **NEW** |
| `scale(n)` method — graceful add/remove workers | **NEW** |
| `shutdown()` — poison-pill drain + join with timeout | **NEW** |
| File: `src/workers/orchestrator.py` | **NEW** |

---

## 7. Causal + Graph Layer (lines 41–44)

| Item | Status |
|---|---|
| DoWhy SCM — `do-calculus` causal effect estimation | Partial — **ALREADY** `src/intelligence/causal_inference.py` exists but is EXPERIMENTAL/blocked (GAP-015); **NEW** wire to live signal path with DoWhy library |
| Asset GNN — GAT over correlation graph (`torch_geometric`) | **NEW** — `e18_network.py` uses NetworkX, not a trainable GAT |
| Granger rolling BTC→ALT causal edge detection | **NEW** — no Granger test in codebase |
| File: `src/causal/dowhy_scm.py`, `src/causal/asset_gnn.py`, `src/causal/granger.py` | **NEW** |

---

## 8. Risk Gate (lines 335–371)

| Item | Status |
|---|---|
| `RiskGate` class with half-Kelly (`kelly_fraction=0.5`) | **ALREADY** — `src/risk/kelly.py` implements Kelly; `src/risk/gates.py` has half-Kelly cap |
| `conf_threshold=0.65` | **ALREADY** — `src/engines/signal_gate.py` has confidence threshold |
| `sharpe_min=1.0` | **ALREADY** — `src/risk/gates.py` Sharpe gate |
| `drawdown_floor=0.10` halt at -10% | **ALREADY** — `src/risk/gates.py` daily drawdown halt |
| `ADWIN(delta=0.002)` per horizon (10 detectors) | **NEW** — `river.drift.ADWIN` not imported in risk module |
| `size()` — CVaR cap (`0.02 / max(cvar, 1e-6)`) | Partial — CVaR in `src/engines/risk_quantifier.py`; **NEW** combine into unified `size()` API |
| `horizon_idx` scaling (`1/(1 + idx*0.1)`) | **NEW** |
| `check_drift()` → ADWIN per horizon → trigger retrain | **NEW** |
| `circuit_breaker()` — drawdown > 10% OR daily_loss > 2% | **ALREADY** — equivalent in `src/risk/gates.py` |
| `HorizonConflictResolver.resolve()` — regime-weighted direction vote | **NEW** |
| File: `src/risk/gate.py` (new, distinct from existing `gates.py`), `src/risk/conflict_resolver.py` | **NEW** |

---

## 9. Execution Layer (lines 374–411)

| Item | Status |
|---|---|
| `SmartOrderRouter` class | **NEW** — `src/execution/` has order manager but no multi-venue smart router |
| Multi-exchange support (binance, bybit, okx) in one router | **ALREADY** partial — ccxt used; **NEW** unified venue comparison in one `route()` call |
| Kyle lambda market-impact estimation | **NEW** — referenced in `e02_microstructure.py` context but not computed |
| `_select_algo()` — IOC / iceberg / TWAP by horizon + impact | **NEW** |
| `_best_venue()` — live order book comparison across venues | **NEW** |
| IOC order execution via ccxt | **ALREADY** — `src/execution/live.py` |
| TWAP slice execution | **NEW** — no TWAP chunking logic |
| Iceberg execution | **NEW** |
| `_execute()` dispatcher | **NEW** |
| File: `src/execution/router.py` | **NEW** |

### 9a. RL Execution Agent (lines 69–71 architecture, implied by SB3)
| Item | Status |
|---|---|
| PPO/SAC via Stable-Baselines3 for execution | Partial — **ALREADY** `e15_rl.py` has DQN; **NEW** PPO/SAC SB3 execution agent |
| File: `src/execution/rl_agent.py` | **NEW** |

### 9b. Post-Trade Analytics
| Item | Status |
|---|---|
| Fill analytics / post-trade | **ALREADY** — `src/diagnostics/trade_auditor.py` |
| File: `src/execution/post_trade.py` (consolidation) | **NEW** |

---

## 10. Self-Upgrade Loop (lines 414–455)

| Item | Status |
|---|---|
| `SelfUpgradeLoop` class | **NEW** |
| MLflow tracking URI init | **NEW** — `mlflow` not imported in any `src/` file currently |
| `retrain_horizon()` — drift-check + schedule-check guard | **NEW** |
| Optuna `TPESampler` + `HyperbandPruner` study | **ALREADY** `optuna` in deps; **NEW** wired to horizon retraining |
| `_wf_objective()` — walk-forward Sharpe on expanding window | **NEW** |
| Hyperparams: lr, hidden, dropout, n_layers | **NEW** |
| Shadow A/B deploy — 24h live Sharpe comparison | **NEW** |
| Promote challenger if Sharpe >5% better than incumbent | **NEW** |
| MLflow `log_params`, `pytorch.log_model`, `register_model` | **NEW** |
| File: `src/upgrade/optuna_wf.py`, `src/upgrade/maml.py`, `src/upgrade/shadow_deploy.py`, `src/upgrade/registry.py` | **NEW** |

### 10a. MAML Fast Adapt (self-upgrade loop line 73, horizon h8/h10)
| Item | Status |
|---|---|
| Model-Agnostic Meta-Learning adapt step | **NEW** |
| Used by h8 (3D) and h10 (1M) horizons | **NEW** |
| File: `src/upgrade/maml.py` | **NEW** |

---

## 11. Tradebot Integration — Drop-in Interface (lines 459–550)

| Item | Status |
|---|---|
| `IntelligenceAdapter` class | Partial — **ALREADY** `src/engine/crypto_box_adapter.py` is a thin stub; **NEW** full `IntelligenceAdapter` matching the spec |
| `CryptoIntelligence` main class | **NEW** — `src/intel.py` |
| `horizons`, `n_workers`, `ecc_enabled`, `exchanges`, `local_node_rpc`, `risk_config` constructor params | **NEW** |
| `intel.start()` — spawns workers + ECC thread | **NEW** |
| `get_signal(symbol, bar)` → per-horizon signal dict | **NEW** |
| Return schema: `{h1: {direction, size_pct, confidence, algo}, ..., meta: {regime, ecc_anomaly, conflict}}` | **NEW** |
| `shutdown()` | **NEW** |
| `on_bar()` integration in `src/api/main.py` | **NEW** wiring |
| File: `src/intelligence/intelligence_adapter.py` | **NEW** |

### 11a. Config File (lines 502–527)
| Item | Status |
|---|---|
| `config/intelligence.yaml` | **NEW** |
| horizons list (0–5 for intraday) | **NEW** |
| n_workers: 12 | **NEW** |
| ecc_enabled: true | **NEW** |
| exchanges: binance, bybit, okx | **ALREADY** ccxt supports these; **NEW** yaml config |
| btc_node_rpc | **NEW** |
| risk sub-section (kelly_fraction, conf_threshold, sharpe_min, drawdown_floor, max_daily_loss) | **NEW** |
| retrain sub-section (adwin_delta, optuna_trials, shadow_hours) | **NEW** |
| observability sub-section (grafana_port, mlflow_uri, evidently_reports) | **NEW** |
| File: `config/intelligence.yaml` | **NEW** |

### 11b. Minimal Wiring (lines 529–550)
| Item | Status |
|---|---|
| `yaml.safe_load('config/intelligence.yaml')` | **NEW** |
| `IntelligenceAdapter(cfg)` at bot startup | **NEW** |
| `on_bar()` dispatcher iterating per-horizon signals | **NEW** |
| `bot.execute(symbol, direction, size_pct, algo)` call | **NEW** |
| `confidence >= conf_threshold` gate before execute | **ALREADY** pattern exists in signal_gate; **NEW** in adapter path |

---

## 12. Installation / Infrastructure (lines 554–588)

| Item | Status |
|---|---|
| `torch`, `torchvision`, `torch_geometric` deps | **NEW** — not in `pyproject.toml` |
| `coincurve` | **ALREADY** in `requirements.lock` |
| `fastecdsa`, `bitcoin-lib-py`, `graphsense-lib` | **NEW** |
| `transformers` (CryptoBERT) | **NEW** |
| `river` (ADWIN) | **NEW** |
| `optuna` | **ALREADY** |
| `ray[tune]` | **NEW** |
| `mlflow` | **NEW** |
| `dvc` | **NEW** |
| `stable-baselines3` | **NEW** |
| `dowhy` | **NEW** |
| `ccxt` | **ALREADY** |
| `prefect` | **NEW** |
| `timescale-client` | **ALREADY** (asyncpg-based in codebase) |
| `duckdb` | **NEW** |
| `hopsworks-client` | **NEW** |
| `py_ecc` | **NEW** |
| `web3` | **NEW** |
| Redpanda docker container (port 9092) | **NEW** |
| TimescaleDB docker container (port 5432) | **ALREADY** — GAP-006 resolved, podman container exists |
| Neo4j docker container (ports 7474, 7687) | **NEW** |
| Bitcoin node (`bitcoind -daemon -txindex=1`) | **NEW** |
| MLflow server (port 5000) | **NEW** |
| Grafana docker container (port 3000) | **NEW** — `src/api/metrics.py` exports Prometheus but no Grafana wired |
| `docker-compose.yml` (Redpanda + TimescaleDB + Neo4j + Grafana) | **NEW** |

---

## 13. Directory Layout (lines 592–655)

| Path | Status |
|---|---|
| `config/intelligence.yaml` | **NEW** |
| `config/horizons.yaml` | **NEW** |
| `config/risk.yaml` | **NEW** |
| `src/data/feeds.py` (ccxt WS ingestion → Redpanda) | Partial — **ALREADY** `src/data/orderbook_stream.py`; **NEW** Redpanda emit |
| `src/data/timescale.py` (hypertable writer) | **ALREADY** `src/data/timescale_storage.py` |
| `src/data/duckdb_store.py` (OLAP queries) | **NEW** |
| `src/features/microstructure.py` (OFI, VPIN, Kyle λ) | Partial — **ALREADY** OFI; **NEW** VPIN + Kyle λ as a dedicated features file |
| `src/features/onchain.py` (SOPR, NVT, MVRV) | **NEW** — existing e05 uses DeFiLlama proxy, not node-derived metrics |
| `src/features/derivatives.py` (OI, funding, liquidations) | **ALREADY** scattered; **NEW** consolidate into single features file |
| `src/features/nlp.py` (CryptoBERT pipeline) | **NEW** |
| `src/ecc/secp256k1_cluster.py` | **NEW** |
| `src/ecc/ecdsa_scan.py` | **NEW** |
| `src/ecc/schnorr_taproot.py` | **NEW** |
| `src/ecc/utxo_curve.py` | **NEW** |
| `src/ecc/zksnark_detect.py` | **NEW** |
| `src/causal/dowhy_scm.py` | **NEW** |
| `src/causal/asset_gnn.py` | **NEW** |
| `src/causal/granger.py` | **NEW** |
| `src/models/cnn.py` | **NEW** |
| `src/models/tcn.py` | **NEW** |
| `src/models/tft.py` | **NEW** |
| `src/models/lstm.py` | **NEW** |
| `src/models/gru.py` | **NEW** |
| `src/models/bert_head.py` | **NEW** |
| `src/models/gnn_head.py` | **NEW** |
| `src/models/mlp.py` | **NEW** |
| `src/models/nbeats.py` | **NEW** |
| `src/models/patchtst.py` | **NEW** |
| `src/models/conformer.py` | **NEW** |
| `src/models/ecc_head.py` | **NEW** |
| `src/fusion/cross_attention.py` | **NEW** |
| `src/fusion/meta_network.py` | **NEW** |
| `src/risk/gate.py` (CVaR-Kelly + ADWIN) | **NEW** (distinct from existing `gates.py`) |
| `src/risk/conflict_resolver.py` | **NEW** |
| `src/execution/router.py` | **NEW** |
| `src/execution/rl_agent.py` | **NEW** |
| `src/execution/post_trade.py` | **NEW** (consolidation) |
| `src/upgrade/optuna_wf.py` | **NEW** |
| `src/upgrade/maml.py` | **NEW** |
| `src/upgrade/shadow_deploy.py` | **NEW** |
| `src/upgrade/registry.py` | **NEW** |
| `src/workers/orchestrator.py` | **NEW** |
| `src/intel.py` (`CryptoIntelligence` main class) | **NEW** |
| `src/intelligence/intelligence_adapter.py` | **NEW** |
| `tests/test_ecc.py` | **NEW** |
| `tests/test_risk_gate.py` | **NEW** (risk gate new API) |
| `tests/test_horizons.py` | **NEW** |
| `notebooks/ecc_signal_analysis.ipynb` | **NEW** |
| `notebooks/horizon_backtest.ipynb` | **NEW** |
| `docker-compose.yml` | **NEW** |

---

## 14. Environment Variables (lines 659–671)

| Variable | Status |
|---|---|
| `BTC_RPC_URL=http://127.0.0.1:8332` | **NEW** |
| `BTC_RPC_USER=crypto` | **NEW** |
| `BTC_RPC_PASS=crypto` | **NEW** |
| `REDPANDA_BROKERS=localhost:9092` | **NEW** |
| `TIMESCALE_DSN=postgresql://crypto:***@localhost:5432/crypto` | **ALREADY** — GAP-006 resolved |  <!-- pragma: allowlist secret -->
| `MLFLOW_TRACKING_URI=http://localhost:5000` | **NEW** |
| `NEO4J_URI=bolt://localhost:7687` | **NEW** |
| `NEO4J_USER=neo4j` | **NEW** |
| `NEO4J_PASS=crypto` | **NEW** |

---

## 15. Key Metrics + Alert Thresholds (lines 674–689)

| Metric | Target | Alert | Status |
|---|---|---|---|
| Per-horizon Sharpe > 1.5 | < 1.0 → retrain | **NEW** — horizon-level Sharpe tracking |
| Signal confidence > 0.70 | < 0.65 → suppress | **ALREADY** in `signal_gate.py` |
| ECC cluster flow score ±0.5 typical | > 0.85 → whale alert | **NEW** |
| ECDSA weakness score < 0.2 baseline | > 0.80 → short signal | **NEW** |
| Hodler index 0.6–0.9 | > 0.85 → supply shock | **NEW** |
| Worker CPU < 85% avg | > 95% → scale up | **NEW** |
| Retrain frequency h1 hourly | ADWIN triggered | **NEW** |
| Retrain frequency h10 monthly | ADWIN triggered | **NEW** |
| Max drawdown gate 10% | halt execution | **ALREADY** |
| Daily loss limit 2% | halt execution | **ALREADY** |

---

## Implementation Phases

### Phase 1 — Infrastructure & Data Bus
1. Add new deps to `pyproject.toml`: `torch`, `torch_geometric`, `transformers`, `river`, `mlflow`, `dvc`, `stable-baselines3`, `dowhy`, `ray[tune]`, `prefect`, `duckdb`, `hopsworks-client`, `py_ecc`, `web3`, `fastecdsa`, `bitcoin-lib-py`, `graphsense-lib`
2. Create `docker-compose.yml` (Redpanda + Neo4j + Grafana; TimescaleDB already running)
3. Add env vars to `.env.example`: `BTC_RPC_URL`, `REDPANDA_BROKERS`, `MLFLOW_TRACKING_URI`, `NEO4J_URI/USER/PASS`
4. Create `src/data/duckdb_store.py` — OLAP/backtest query layer
5. Extend `src/data/orderbook_stream.py` → emit to Redpanda topic (add `src/data/feeds.py` Redpanda producer)

### Phase 2 — Feature Layer Completion
6. Create `src/features/microstructure.py` — VPIN + Kyle λ (OFI already in e02)
7. Create `src/features/onchain.py` — SOPR, NVT, MVRV via local Bitcoin node RPC
8. Create `src/features/derivatives.py` — consolidate OI, funding, liquidations
9. Create `src/features/nlp.py` — CryptoBERT 110M CPU inference pipeline
10. Create `src/features/mempool.py` — fee pressure + tx count from local node

### Phase 3 — ECC Layer
11. Create `src/ecc/__init__.py`
12. Create `src/ecc/secp256k1_cluster.py` — AddressClusterer, whale filter, flow score
13. Create `src/ecc/ecdsa_scan.py` — DER decoder, r-registry, nonce-reuse privkey extraction
14. Create `src/ecc/schnorr_taproot.py` — P2TR detector, MuSig2 cosigner clustering, smart money divergence
15. Create `src/ecc/utxo_curve.py` — hodler index computation
16. Create `src/ecc/zksnark_detect.py` — Tornado Cash BFS, dark pool pressure

### Phase 4 — Neural Ensemble (12 heads)
17. Create `src/models/cnn.py` — 1D dilated conv
18. Create `src/models/tcn.py` — causal dilated conv + residual
19. Create `src/models/tft.py` — Temporal Fusion Transformer
20. Create `src/models/lstm.py` — 2-layer + attention
21. Create `src/models/gru.py` — 2-layer + regime conditioning
22. Create `src/models/bert_head.py` — CryptoBERT projection 768→128
23. Create `src/models/gnn_head.py` — 3-layer GAT (`torch_geometric`)
24. Create `src/models/mlp.py` — 4-layer residual + LayerNorm + GELU
25. Create `src/models/nbeats.py` — trend + seasonality + residual stacks
26. Create `src/models/patchtst.py` — Patch transformer (len=16, stride=8, 6 layers)
27. Create `src/models/conformer.py` — Conv + rel-pos MHA, 4 blocks
28. Create `src/models/ecc_head.py` — ECC feature → 128-dim embedding

### Phase 5 — Fusion + Meta-Network
29. Create `src/fusion/__init__.py`
30. Create `src/fusion/cross_attention.py` — `CrossAttentionFusion` (regime query, 8-head MHA, sparse gate, ECC boost)
31. Create `src/fusion/meta_network.py` — `MetaNetwork` (10 output heads: direction/magnitude/timing), joint loss, AdamW

### Phase 6 — Causal + Graph Layer
32. Create `src/causal/__init__.py`
33. Create `src/causal/dowhy_scm.py` — DoWhy SCM, do-calculus causal effect (unblock GAP-015)
34. Create `src/causal/asset_gnn.py` — GAT over correlation graph with `torch_geometric`
35. Create `src/causal/granger.py` — rolling Granger BTC→ALT edge detection

### Phase 7 — Horizon System + Worker Orchestrator
36. Create `config/horizons.yaml` — per-horizon model, ECC op, retrain schedule, min workers
37. Create `config/intelligence.yaml` — main config
38. Create `config/risk.yaml` — risk gate params
39. Create `src/workers/__init__.py`
40. Create `src/workers/orchestrator.py` — `WorkerOrchestrator` (multiprocessing + ECC thread + NUMA pinning + scale/shutdown)

### Phase 8 — Risk Gate v2 + Conflict Resolver
41. Create `src/risk/gate.py` — `RiskGate` with ADWIN per horizon, CVaR-Kelly `size()`, `check_drift()`, `circuit_breaker()`
42. Create `src/risk/conflict_resolver.py` — `HorizonConflictResolver.resolve()` regime-weighted vote

### Phase 9 — Execution Layer
43. Create `src/execution/router.py` — `SmartOrderRouter` (IOC/iceberg/TWAP, Kyle lambda, multi-venue best-price)
44. Create `src/execution/rl_agent.py` — PPO/SAC via Stable-Baselines3
45. Create `src/execution/post_trade.py` — fill analytics consolidation

### Phase 10 — Self-Upgrade Loop
46. Create `src/upgrade/__init__.py`
47. Create `src/upgrade/maml.py` — MAML fast-adapt for h8/h10
48. Create `src/upgrade/optuna_wf.py` — walk-forward Sharpe objective, Hyperband pruner
49. Create `src/upgrade/shadow_deploy.py` — 24h A/B live Sharpe gating
50. Create `src/upgrade/registry.py` — MLflow `log_model`, `register_model`, DVC integration

### Phase 11 — Main CryptoIntelligence Class + Adapter
51. Create `src/intel.py` — `CryptoIntelligence` (orchestrates all 10 horizons, feature bus, ECC worker, causal layer, fusion, risk gate)
52. Create `src/intelligence/intelligence_adapter.py` — `IntelligenceAdapter` drop-in (replaces thin `CryptoBoxSignalAdapter` stub)
53. Wire `on_bar()` dispatch in `src/api/main.py` — load yaml config, call `intel.get_signal()`, gate by confidence, call `bot.execute()`

### Phase 12 — Tests + Observability
54. Create `tests/test_ecc.py` — unit tests for all 5 ECC modules
55. Create `tests/test_risk_gate.py` — new `RiskGate` API tests
56. Create `tests/test_horizons.py` — 10-horizon inference path tests
57. Wire Grafana dashboard for horizon Sharpe, ECC scores, worker CPU, drift alerts
58. Add evidently report generation in `src/upgrade/shadow_deploy.py`

---

## Summary Counts

| Category | ALREADY | NEW |
|---|---|---|
| Data infrastructure | 2 | 5 |
| Feature layer | 3 | 5 |
| ECC modules | 1 (dep only) | 5 files + bitcoin node |
| Neural models | 0 | 12 |
| Fusion | 0 | 2 |
| Causal/Graph | 1 (blocked) | 3 |
| Horizon config | 0 | 10 horizons + 3 yaml |
| Worker orchestrator | 0 | 1 |
| Risk gate | 3 (partial) | 2 new files |
| Execution | 1 (live.py) | 3 |
| Self-upgrade | 0 | 4 |
| Integration adapter | 1 (stub) | 2 |
| Tests | 0 | 3 |
| Observability/infra | 1 (TimescaleDB) | 4 services |
| **Total** | **~13** | **~62** |
