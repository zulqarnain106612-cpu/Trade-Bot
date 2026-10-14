# Engineering audit

src: 432 files / 108060 LOC | tests: 566 files / 131882 LOC / 9867 test fns | type-hinted fns: 97% | docstring coverage: 59% | routes: 68
chain/exchange libs: ccxt, web3 | storage libs: aiosqlite, asyncpg, duckdb, pymongo, sqlite3

## Overall score: 77/100

| domain | score | crit | high | med | low |
|---|---|---|---|---|---|
| security | 0 | 6 | 17 | 0 | 16 |
| software | 14 | 0 | 9 | 82 | 111 |
| trading | 55 | 0 | 2 | 73 | 0 |
| blockchain | 76 | 0 | 0 | 16 | 5 |
| reliability | 76 | 0 | 5 | 1 | 17 |
| quality | 80 | 0 | 0 | 203 | 2 |
| ml | 86 | 0 | 0 | 11 | 34 |
| python | 86 | 0 | 1 | 4 | 16 |
| crypto | 87 | 0 | 1 | 4 | 0 |
| devops | 89 | 0 | 1 | 0 | 64 |
| performance | 89 | 0 | 0 | 18 | 1 |
| api | 91 | 0 | 0 | 7 | 0 |
| frontend | 93 | 0 | 0 | 4 | 0 |
| observability | 94 | 0 | 0 | 0 | 195 |
| data | 98 | 0 | 0 | 1 | 0 |
| docs | 100 | 0 | 0 | 0 | 1 |
| governance | 100 | 0 | 0 | 0 | 0 |

Since last run: **0 new**, **0 fixed**

## Fix first (top 20 by severity)
- [critical] **private-key-in-repo** x5 (security): Remove, rotate, scrub history. e.g. .test_durations:846 private key block: ----***
- [critical] **secret-in-file** x14 (security): Remove, rotate, scrub history. e.g. .test_durations:850 API secret key (sk-...): sk-l***
- [high] **custom-crypto-primitive** x1 (crypto): Don't roll your own; use cryptography/libsodium. Quarantine research code. e.g. src/models/trainer.py:179 hmac_compare(): hand-rolled crypto primitive outside quarantine dirs
- [high] **no-lockfile** x1 (devops): Add lock with hashes (uv lock / pip-compile --generate-hashes). e.g. requirements*.txt:0 55 non-exact requirement lines and no lock/hash file
- [high] **swallowed-exception** x1 (python): Log with traceback or re-raise; never pass silently on money path. e.g. .claude/hooks/observation_failure.py:66 broad exception swallowed (pass/continue)
- [high] **cancelled-swallowed** x5 (reliability): Re-raise CancelledError or shutdown hangs. e.g. src/engine/orchestrator.py:1146 CancelledError caught without re-raise
- [high] **log-secret** x1 (security): Redact secrets before logging. e.g. src/ecc/ecdsa_scan.py:243 logging value named 'privkey_extracted'
- [high] **shell-exec** x2 (security): Use subprocess list args, shell=False. e.g. audit3.py:259 subprocess.run with shell
- [high] **sql-injection-risk** x7 (security): Parameterize queries. e.g. src/data/storage.py:808 dynamic SQL string in execute()
- [high] **token-in-url** x5 (security): Tokens in URL leak to logs; use header/subprotocol/ticket. e.g. src/api/main.py:1271 trades: auth-like value read from query string
- [high] **unsafe-deserialization** x2 (security): Use json/safe formats; never load untrusted pickle/yaml. e.g. src/engines/e09_ml_meta.py:39 pickle.load
- [high] **high-complexity** x37 (software): Cyclomatic >20: split branches, use tables/strategy objects. e.g. .claude/hooks/pre_tool_use.py:313 _github_route_violation: complexity 43
- [high] **lookahead-shift** x2 (trading): shift(-n) leaks future data into features. e.g. crypto-predictor/backtest.py:33 df['close'].shift(-horizon)
- [high] **retry-no-backoff** x1 (trading): while True + except without sleep/backoff = hammering exchange. e.g. src/workers/orchestrator.py:105 while True + try/except without sleep/backoff/await

## Trading-safety capability map
OK kill_switch, OK idempotency, OK position_limits, OK drawdown_stop, OK rate_limit_backoff, OK reconciliation, OK audit_trail, OK graceful_shutdown, OK paper_mode, OK secret_vault, OK metrics, OK structured_logging, OK tracing, OK health_endpoint, OK decimal_money

## Most complex functions
- 161: audit3.py:643 visit_Call
- 116: src/engine/signal_engine.py:663 tick
- 66: src/engine/orchestrator.py:1195 _tick_traced
- 56: src/intel.py:452 on_bar
- 54: src/workers/orchestrator.py:133 _run_horizon_inference
- 49: scripts/startup_preflight.py:330 main
- 43: .claude/hooks/pre_tool_use.py:313 _github_route_violation
- 41: src/mathcore/registry.py:259 _check_semantics
- 41: src/fusion/horizon_decision.py:40 fuse_horizon_predictions
- 36: audit3.py:399 visit_FunctionDef

## Largest files
- 3953: src/api/main.py
- 3483: src/engine/orchestrator.py
- 2183: src/data/storage.py
- 1898: src/engine/signal_engine.py
- 1778: scripts/check_static_invariants.py
- 1639: src/models/trainer.py
- 1594: src/data/timescale_storage.py
- 1504: src/execution/live.py
- 1472: src/config.py
- 1288: audit3.py

## Git churn (90d)
```
54 src/engine/orchestrator.py
     41 scripts/check_static_invariants.py
     35 src/engine/signal_engine.py
     35 src/api/main.py
     34 tests/regression/test_regression_registry_contract.py
     34 src/config.py
     31 tests/test_static_invariants.py
     22 src/api/main.py
     20 src/intel.py
     19 tests/test_ci_failure_notify_workflow.py
```

## api (score 91)

### [medium] error-detail-leak x6
Fix: Return generic errors; log details server-side
- src/api/main.py:879 exception text returned to client
- src/api/main.py:1284 exception text returned to client
- src/api/main.py:1334 exception text returned to client
- src/api/main.py:1862 exception text returned to client
- src/api/main.py:3103 exception text returned to client
- src/api/main.py:3651 exception text returned to client

### [medium] route-no-dependency x1
Fix: Verify auth applied (Depends/middleware) on this route
- src/api/main.py:2007 WEBSOCKET /ws (websocket_endpoint): no Depends/Security - confirm auth middleware covers it

## blockchain (score 76)

### [medium] oracle-no-staleness x8
Fix: Check oracle updatedAt/round and deviation bounds
- src/api/auth.py:0 oracle/price-feed use without staleness check
- src/api/error_hygiene.py:0 oracle/price-feed use without staleness check
- src/api/object_refs.py:0 oracle/price-feed use without staleness check
- src/mathcore/curves/secp256k1.py:0 oracle/price-feed use without staleness check
- src/mathcore/derivation/nonces.py:0 oracle/price-feed use without staleness check
- src/mathcore/fields/ntt.py:0 oracle/price-feed use without staleness check
- src/mathcore/harmonic/fft_spectral.py:0 oracle/price-feed use without staleness check
- src/security/at_rest.py:0 oracle/price-feed use without staleness check

### [medium] float-chain-units x4
Fix: wei/satoshi/lamport math must use int/Decimal, not float
- src/ecc/zksnark_detect.py:208 float unit conversion by 1e+18
- src/engines/e17_liquidity.py:126 float unit conversion by 1e+06
- src/features/mempool.py:66 float unit conversion by 1e+08
- src/features/onchain.py:130 float unit conversion by 1e+08

### [medium] no-reorg-handling x3
Fix: Handle reorgs: confirmations/finalized tag, rollback derived data
- src/data/block_height_provider.py:0 reads blocks/logs without reorg/finality/confirmation handling
- src/ecc/zksnark_detect.py:0 reads blocks/logs without reorg/finality/confirmation handling
- src/features/onchain.py:0 reads blocks/logs without reorg/finality/confirmation handling

### [medium] unlimited-approval x1
Fix: Approve exact amounts; revoke after use
- audit3.py:926 unlimited approval constant: MaxU***

### [low] no-block-checkpoint x2
Fix: Persist last processed block for idempotent resume
- src/ecc/zksnark_detect.py:0 block ingestion without visible checkpoint/resume
- src/workers/orchestrator.py:0 block ingestion without visible checkpoint/resume

### [low] hardcoded-addresses x1
Fix: Move contract addresses to per-chain config
- src/ecc/zksnark_detect.py:0 8 hardcoded 0x addresses

### [low] no-checksum-validation x1
Fix: Validate EIP-55 checksum addresses
- src/ecc/zksnark_detect.py:0 web3 usage without checksum address validation

### [low] rpc-url-credentials x1
Fix: Credentials/API key inside RPC URL: move to env/secret store
- tests/agent_control/test_git_safety.py:185 credentials in URL: http***

## crypto (score 87)

### [high] custom-crypto-primitive x1
Fix: Don't roll your own; use cryptography/libsodium. Quarantine research code
- src/models/trainer.py:179 hmac_compare(): hand-rolled crypto primitive outside quarantine dirs

### [medium] non-constant-time-compare x3
Fix: Use hmac.compare_digest for MACs/tokens/signatures
- scripts/codebase_knowledge_index.py:221 digest != meta.group(2)
- scripts/codebase_knowledge_index.py:238 digest != actual_digest
- src/runtime/reconcile.py:261 alerted[0] == signature

### [medium] pq-claim-without-impl x1
Fix: Code/docs claim post-quantum but no PQ library imported - fix claim or implement
- src:0 post-quantum referenced but no PQ library (oqs/pqcrypto/kyber_py) is imported

## data (score 98)

### [medium] many-storage-backends x1
Fix: Pick a primary store; each backend adds failure modes
- src:0 5 storage libs: aiosqlite, asyncpg, duckdb, pymongo, sqlite3

## devops (score 89)

### [high] no-lockfile x1
Fix: Add lock with hashes (uv lock / pip-compile --generate-hashes)
- requirements*.txt:0 55 non-exact requirement lines and no lock/hash file

### [low] env-undocumented x59
Fix: Document in .env.example
- .env.example:0 ALLOW_SKIPPED used in code, not documented
- .env.example:0 BTC_RPC_PASS used in code, not documented
- .env.example:0 BTC_RPC_URL used in code, not documented
- .env.example:0 BTC_RPC_USER used in code, not documented
- .env.example:0 CLAUDE_PROJECT_DIR used in code, not documented
- .env.example:0 COORDINATOR_MODEL used in code, not documented
- .env.example:0 CRYPTOBERT_MODEL used in code, not documented
- .env.example:0 CRYPTOBERT_REVISION used in code, not documented
- .env.example:0 CRYPTO_BOX used in code, not documented
- .env.example:0 DRY_RUN used in code, not documented
- .env.example:0 DUCKDB_PATH used in code, not documented
- .env.example:0 DVC_REMOTE used in code, not documented
- ... +47 more (audit_findings.json)

### [low] gitignore-missing x4
Fix: Add to .gitignore
- .gitignore:0 '*.pem' not ignored
- .gitignore:0 '*.db' not ignored
- .gitignore:0 '*.sqlite' not ignored
- .gitignore:0 '*.key' not ignored

### [low] workflow-continue-on-error x1
Fix: Hides failures
- .github/workflows/ci.yml:0 continue-on-error: true

## docs (score 100)

### [low] missing-doc-file x1
Fix: Add file
- CHANGELOG:0 CHANGELOG not found

## frontend (score 93)

### [medium] js-token-storage x3
Fix: Don't keep tokens in localStorage; use httpOnly cookies
- frontend/src/components/terminal/TerminalDock.jsx:147 try { localStorage.setItem(HEIGHT_KEY, String(height)); } catch { /* storage una
- frontend/src/components/terminal/terminalUi.test.jsx:261 localStorage.setItem(FAILURES_SEEN_KEY, '500');
- frontend/src/terminal/TerminalContext.jsx:148 try { localStorage.setItem(FAILURES_SEEN_KEY, String(state.failuresSeenAt)); } c

### [medium] js-xss-sink x1
Fix: Avoid innerHTML/dangerouslySetInnerHTML/eval; sanitize
- frontend/src/components/workspace/Workspace.test.jsx:72 header.innerHTML = `

## ml (score 86)

### [medium] joblib-load x10
Fix: joblib/pickle artifacts are code execution: verify hash/signature before load
- src/intelligence/ensemble_predictor.py:951 joblib.load
- src/models/horizon_mlp.py:302 joblib.load
- src/models/online_trainer.py:331 joblib.load
- src/models/online_trainer.py:337 joblib.load
- src/models/trainer.py:1278 joblib.load
- src/models/trainer.py:1294 joblib.load
- src/regime/depth_detector_v2.py:160 joblib.load
- src/regime/depth_detector_v2.py:161 joblib.load
- src/regime/depth_detector_v2.py:162 joblib.load
- src/regime/detector.py:703 joblib.load

### [medium] heavy-import-live-path x1
Fix: Lazy-import torch/sklearn in live path (startup, memory, attack surface)
- src/engine/signal_engine.py:39 module-scope import of xgboost in live path

### [low] no-seed x34
Fix: Set seeds / random_state for reproducibility
- crypto-predictor/backtest.py:0 ML/random usage with no seed/random_state
- crypto-predictor/risk.py:0 ML/random usage with no seed/random_state
- crypto-predictor/validation.py:0 ML/random usage with no seed/random_state
- scripts/startup_preflight.py:0 ML/random usage with no seed/random_state
- src/causal/asset_gnn.py:0 ML/random usage with no seed/random_state
- src/engine/orchestrator.py:0 ML/random usage with no seed/random_state
- src/engine/signal_engine.py:0 ML/random usage with no seed/random_state
- src/engines/consensus.py:0 ML/random usage with no seed/random_state
- src/engines/e11_stochastic.py:0 ML/random usage with no seed/random_state
- src/engines/e15_rl.py:0 ML/random usage with no seed/random_state
- src/features/selftest.py:0 ML/random usage with no seed/random_state
- src/fusion/cross_attention.py:0 ML/random usage with no seed/random_state
- ... +22 more (audit_findings.json)

## observability (score 94)

### [low] log-no-traceback x188
Fix: Use logger.exception / exc_info=True
- scripts/backfill.py:122 error logged inside except without traceback (use logger.exception)
- src/api/main.py:193 error logged inside except without traceback (use logger.exception)
- src/api/main.py:467 error logged inside except without traceback (use logger.exception)
- src/api/main.py:471 error logged inside except without traceback (use logger.exception)
- src/api/main.py:512 error logged inside except without traceback (use logger.exception)
- src/api/main.py:530 error logged inside except without traceback (use logger.exception)
- src/api/main.py:538 error logged inside except without traceback (use logger.exception)
- src/api/main.py:544 error logged inside except without traceback (use logger.exception)
- src/api/main.py:554 error logged inside except without traceback (use logger.exception)
- src/api/main.py:562 error logged inside except without traceback (use logger.exception)
- src/api/main.py:572 error logged inside except without traceback (use logger.exception)
- src/api/main.py:683 error logged inside except without traceback (use logger.exception)
- ... +176 more (audit_findings.json)

### [low] logging-fstring x7
Fix: Use logger.info('x %s', v) / structured fields
- src/api/main.py:1142 f-string in logging call
- src/api/main.py:3555 f-string in logging call
- src/data/fetcher.py:542 f-string in logging call
- src/intelligence/ensemble_predictor.py:687 f-string in logging call
- src/intelligence/ensemble_predictor.py:697 f-string in logging call
- src/intelligence/ensemble_predictor.py:699 f-string in logging call
- src/models/trainer.py:1579 f-string in logging call

## performance (score 89)

### [medium] n-plus-1-query x18
Fix: Batch queries outside loop
- src/data/storage.py:804 query inside loop
- src/data/storage.py:808 query inside loop
- src/data/storage.py:2137 query inside loop
- src/data/timescale_storage.py:450 query inside loop
- src/data/timescale_storage.py:486 query inside loop
- src/data/timescale_storage.py:487 query inside loop
- src/models/horizon_backfill.py:53 query inside loop
- src/models/horizon_learning.py:80 query inside loop
- src/models/horizon_learning.py:117 query inside loop
- src/models/horizon_learning.py:255 query inside loop
- src/models/horizon_learning.py:323 query inside loop
- src/models/horizon_learning.py:334 query inside loop
- ... +6 more (audit_findings.json)

### [low] regex-in-loop x1
Fix: Compile once at module level
- common/command_schema.py:560 re.compile

## python (score 86)

### [high] swallowed-exception x1
Fix: Log with traceback or re-raise; never pass silently on money path
- .claude/hooks/observation_failure.py:66 broad exception swallowed (pass/continue)

### [medium] baseexception x4
Fix: Never catch BaseException unless re-raising
- src/data/storage.py:876 except BaseException
- src/data/timescale_storage.py:452 except BaseException
- src/execution/mode_persistence.py:78 except BaseException
- src/terminal/sessions.py:94 except BaseException

### [low] float-equality x16
Fix: Use math.isclose / Decimal
- src/data/deribit_provider.py:118 float == comparison
- src/data/deribit_provider.py:118 float == comparison
- src/data/fetcher.py:130 float == comparison
- src/data/quality_gate.py:401 float == comparison
- src/data/quality_gate.py:403 float == comparison
- src/diagnostics/attribution.py:81 float == comparison
- src/diagnostics/attribution.py:98 float == comparison
- src/engine/signal_engine.py:1752 float == comparison
- src/engine/signal_engine.py:1766 float == comparison
- src/features/pipeline.py:632 float == comparison
- src/features/pipeline.py:633 float == comparison
- src/intelligence/macro_indicators.py:134 float == comparison
- ... +4 more (audit_findings.json)
- [info] print-in-src: 236

## quality (score 80)

### [medium] assertless-test x202
Fix: Test without assertion proves nothing; assert behaviour
- tests/api/test_injection_and_rate_limiting.py:82 test_real_timeframes_are_accepted has no assertion
- tests/api/test_injection_and_rate_limiting.py:161 test_a_single_call_is_allowed has no assertion
- tests/api/test_kill_switch_durability.py:139 test_clearing_a_missing_file_is_not_an_error has no assertion
- tests/api/test_websocket_frame_guard.py:75 test_distinct_nonces_are_all_accepted has no assertion
- tests/api/test_websocket_frame_guard.py:155 test_a_frame_inside_the_window_survives has no assertion
- tests/api/test_websocket_frame_guard.py:196 test_the_budget_refills_as_the_window_slides has no assertion
- tests/api/test_websocket_frame_guard.py:215 test_one_connection_cannot_spend_anothers_budget has no assertion
- tests/api/test_websocket_frame_guard.py:223 test_two_clients_may_use_the_same_nonce has no assertion
- tests/component/test_money_representation.py:156 test_clearing_an_absent_position_is_harmless has no assertion
- tests/engines/test_data_providers_extended.py:38 test_update_cache_no_crash_when_cache_unavailable has no assertion
- tests/engines/test_engine_guard_and_error_paths.py:604 test_engine_output_log_failure_is_swallowed has no assertion
- tests/engines/test_sentiment_provider.py:176 test_cache_publish_failure_is_swallowed has no assertion
- ... +190 more (audit_findings.json)

### [medium] coverage-chasing-tests x1
Fix: Merge into real module tests; assert behaviour, not lines
- tests/test_drift_integration_coverage.py:0 44 coverage-padding test files: test_drift_integration_coverage.py, test_signal_debugger_coverage.py, test_onchain_base_coverage.py, test_zksnark_detect_coverage.py, test_coverage_boost6.py, test_coverage_boost3.py, test_kelly_gaps.py, test_live_executor_coverage.py

### [low] sleep-in-test x1
Fix: Use fake clock/event; sleeps cause flakiness
- tests/test_crypto_intel_v6_coverage.py:0 time.sleep in test

### [low] test-config-weak x1
Fix: Add cov-fail-under, --strict-markers, pytest-timeout
- pytest config:0 missing pytest-timeout

## reliability (score 76)

### [high] cancelled-swallowed x5
Fix: Re-raise CancelledError or shutdown hangs
- src/engine/orchestrator.py:1146 CancelledError caught without re-raise
- src/engine/orchestrator.py:2183 CancelledError caught without re-raise
- src/engine/orchestrator.py:2231 CancelledError caught without re-raise
- src/engine/orchestrator.py:2315 CancelledError caught without re-raise
- src/tuning/scheduler.py:220 CancelledError caught without re-raise

### [medium] unclosed-session x1
Fix: Use 'async with ClientSession()'
- src/intelligence/onchain/base.py:251 ClientSession outside 'async with'

### [low] subprocess-no-timeout x17
Fix: Pass timeout=
- .claude/skills/quality-engineering/scripts/qe_gate.py:157 subprocess.run
- .claude/skills/quality-engineering/scripts/qe_gate.py:179 subprocess.run
- .claude/skills/quality-engineering/scripts/qe_gate.py:320 subprocess.run
- review/build_context.py:23 subprocess.run
- scripts/check_coverage_floors.py:43 subprocess.run
- scripts/check_production_readiness.py:109 subprocess.run
- scripts/codebase_knowledge_index.py:51 subprocess.run
- scripts/collect_quality_metrics.py:87 subprocess.run
- scripts/ingest_kg_repo.py:35 subprocess.run
- scripts/ingest_repo.py:51 subprocess.run
- scripts/local_checks.py:34 subprocess.run
- scripts/resolve_registry_merge.py:86 subprocess.run
- ... +5 more (audit_findings.json)

## security (score 0)

### [critical] secret-in-file x4
Fix: Remove, rotate, scrub history
- .test_durations:850 API secret key (sk-...): sk-l***
- .test_durations:859 API secret key (sk-...): sk-l***
- .test_durations:2377 API secret key (sk-...): sk-a***
- .test_durations:2378 Slack token: xoxb***

### [critical] private-key-in-repo x2
Fix: Remove, rotate, scrub history
- .test_durations:846 private key block: ----***
- .test_durations:855 private key block: ----***

### [high] sql-injection-risk x7
Fix: Parameterize queries
- src/data/storage.py:808 dynamic SQL string in execute()
- src/data/storage.py:1274 dynamic SQL string in execute()
- src/data/storage.py:2137 dynamic SQL string in execute()
- src/data/timescale_storage.py:653 dynamic SQL string in execute()
- src/models/horizon_learning.py:80 dynamic SQL string in execute()
- src/models/horizon_learning.py:117 dynamic SQL string in execute()
- src/models/horizon_learning.py:905 dynamic SQL string in execute()

### [high] token-in-url x5
Fix: Tokens in URL leak to logs; use header/subprotocol/ticket
- src/api/main.py:1271 trades: auth-like value read from query string
- src/api/main.py:1323 missed_trades: auth-like value read from query string
- src/api/main.py:1360 equity_curve: auth-like value read from query string
- src/api/main.py:2194 debug_audit: auth-like value read from query string
- src/api/main.py:2209 audit_chain_integrity: auth-like value read from query string

### [high] shell-exec x2
Fix: Use subprocess list args, shell=False
- audit3.py:259 subprocess.run with shell
- common/shell_exec.py:140 subprocess.Popen with shell

### [high] unsafe-deserialization x2
Fix: Use json/safe formats; never load untrusted pickle/yaml
- src/engines/e09_ml_meta.py:39 pickle.load
- src/engines/e15_rl.py:56 pickle.load

### [high] log-secret x1
Fix: Redact secrets before logging
- src/ecc/ecdsa_scan.py:243 logging value named 'privkey_extracted'

### [low] secret-in-file x10
Fix: Remove, rotate, scrub history
- tests/api/test_error_hygiene.py:40 Stripe live key: sk_l***
- tests/api/test_error_hygiene.py:164 Stripe live key: sk_l***
- tests/api/test_error_hygiene.py:169 Stripe live key: sk_l***
- tests/api/test_error_hygiene.py:284 Stripe live key: sk_l***
- tests/api/test_error_hygiene.py:286 Stripe live key: sk_l***
- tests/fuzz/test_malformed_input_is_safe.py:91 API secret key (sk-...): sk-l***
- tests/security/test_secret_leak_provocation.py:31 API secret key (sk-...): sk-a***
- tests/security/test_secret_leak_provocation.py:32 GitHub token: ghp_***
- tests/test_command_schema_policy.py:305 API secret key (sk-...): sk-a***
- tests/test_command_schema_policy.py:306 Slack token: xoxb***

### [low] hardcoded-secret x3
Fix: Move to env/vault, rotate the key
- tests/security/test_secret_leak_provocation.py:31 API_KEY = 'sk-a***' (len 43)
- tests/security/test_secret_leak_provocation.py:32 GITHUB_TOKEN = 'ghp_***' (len 40)
- tests/test_self_tuning_api.py:18 _WRONG_SECRET = 'wron***' (len 12)

### [low] private-key-in-repo x3
Fix: Remove, rotate, scrub history
- tests/fuzz/test_malformed_input_is_safe.py:93 private key block: ----***
- tests/security/test_secret_leak_provocation.py:35 private key block: ----***
- tests/test_command_schema_policy.py:325 private key block: ----***

## software (score 14)

### [high] high-complexity x9
Fix: Cyclomatic >20: split branches, use tables/strategy objects
- .claude/hooks/pre_tool_use.py:313 _github_route_violation: complexity 43
- audit3.py:643 visit_Call: complexity 161
- scripts/startup_preflight.py:330 main: complexity 49
- src/engine/orchestrator.py:1195 _tick_traced: complexity 66
- src/engine/signal_engine.py:663 tick: complexity 116
- src/fusion/horizon_decision.py:40 fuse_horizon_predictions: complexity 41
- src/intel.py:452 on_bar: complexity 56
- src/mathcore/registry.py:259 _check_semantics: complexity 41
- src/workers/orchestrator.py:133 _run_horizon_inference: complexity 54

### [medium] long-function x35
Fix: Extract helpers, aim <60 lines
- audit3.py:643 visit_Call: 139 lines
- common/shell_exec.py:311 run: 153 lines
- scripts/startup_preflight.py:330 main: 204 lines
- src/api/main.py:584 lifespan: 213 lines
- src/data/storage.py:970 store_intelligence_features: 152 lines
- src/diagnostics/runtime_monitor.py:223 _run_all_probes: 160 lines
- src/engine/orchestrator.py:268 __init__: 122 lines
- src/engine/orchestrator.py:534 _warm_up_models: 174 lines
- src/engine/orchestrator.py:1195 _tick_traced: 556 lines
- src/engine/orchestrator.py:1963 _train_models: 195 lines
- src/engine/orchestrator.py:2910 _build_portfolio_inputs: 146 lines
- src/engine/signal_engine.py:663 tick: 961 lines
- ... +23 more (audit_findings.json)

### [medium] high-complexity x28
Fix: Cyclomatic >20: split branches, use tables/strategy objects
- .claude/hooks/pre_tool_use.py:163 _github_repo_refs: complexity 21
- .claude/hooks/pre_tool_use.py:218 _github_boundary_write_violation: complexity 26
- .claude/hooks/pre_tool_use.py:263 _github_fetch_endpoint_allowed: complexity 27
- .claude/hooks/pre_tool_use.py:655 _violations: complexity 23
- audit3.py:399 visit_FunctionDef: complexity 36
- audit3.py:523 visit_Try: complexity 21
- audit3.py:582 assign: complexity 30
- common/shell_exec.py:185 _filter: complexity 24
- scripts/check_static_invariants.py:324 check_settings_are_read: complexity 21
- scripts/check_static_invariants.py:646 check_protocol_methods_are_called: complexity 25
- scripts/check_static_invariants.py:767 check_dataclass_fields_are_read: complexity 26
- scripts/check_static_invariants.py:1420 _locals_typed_as_dataclasses: complexity 30
- ... +16 more (audit_findings.json)

### [medium] layer-violation x10
Fix: Live path must not import research/experimental code
- src.api.runtime_control:0 src.api.runtime_control (live) imports src.agent_control.store (research/quarantine)
- src.api.runtime_control:0 src.api.runtime_control (live) imports src.agent_control.model (research/quarantine)
- src.security.api_signer:0 src.security.api_signer (live) imports src.mathcore.curves.ed25519 (research/quarantine)
- src.security.api_signer:0 src.security.api_signer (live) imports src.mathcore.curves (research/quarantine)
- src.security.pq_transport:0 src.security.pq_transport (live) imports src.mathcore.fields.ntt (research/quarantine)
- src.workers.orchestrator:0 src.workers.orchestrator (live) imports src.ecc.ecdsa_scan (research/quarantine)
- src.workers.orchestrator:0 src.workers.orchestrator (live) imports src.ecc.zksnark_detect (research/quarantine)
- src.workers.orchestrator:0 src.workers.orchestrator (live) imports src.ecc.secp256k1_cluster (research/quarantine)
- src.workers.orchestrator:0 src.workers.orchestrator (live) imports src.ecc.schnorr_taproot (research/quarantine)
- src.workers.orchestrator:0 src.workers.orchestrator (live) imports src.ecc.utxo_curve (research/quarantine)

### [medium] god-file x8
Fix: Split by responsibility (router/service/repository)
- scripts/check_static_invariants.py:0 1778 lines
- src/api/main.py:0 3953 lines
- src/data/storage.py:0 2183 lines
- src/data/timescale_storage.py:0 1594 lines
- src/engine/orchestrator.py:0 3483 lines
- src/engine/signal_engine.py:0 1898 lines
- src/execution/live.py:0 1504 lines
- src/models/trainer.py:0 1639 lines

### [medium] import-cycle x1
Fix: Invert dependency or extract shared module
- src.data.timescale_storage:0 cycle of 2: src.data.storage <-> src.data.timescale_storage

### [low] deep-loop-nesting x28
Fix: Flatten loops / vectorize
- audit3.py:838 3+ nested loops
- audit3.py:939 3+ nested loops
- audit3.py:1267 3+ nested loops
- scripts/check_static_invariants.py:177 3+ nested loops
- scripts/check_static_invariants.py:397 3+ nested loops
- scripts/check_static_invariants.py:544 3+ nested loops
- scripts/check_static_invariants.py:625 3+ nested loops
- scripts/check_static_invariants.py:697 3+ nested loops
- scripts/check_static_invariants.py:805 3+ nested loops
- scripts/check_static_invariants.py:816 3+ nested loops
- scripts/check_static_invariants.py:957 3+ nested loops
- scripts/check_static_invariants.py:1123 3+ nested loops
- ... +16 more (audit_findings.json)

### [low] too-many-params x28
Fix: Group into a dataclass/config object
- src/data/storage.py:407 __init__: 11 params
- src/data/storage.py:483 __init__: 26 params
- src/data/storage.py:557 __init__: 12 params
- src/data/storage.py:599 __init__: 10 params
- src/data/storage.py:639 __init__: 12 params
- src/data/storage.py:681 __init__: 10 params
- src/engine/signal_engine.py:260 __init__: 11 params
- src/engine/signal_engine.py:663 tick: 13 params
- src/engines/signal_gate.py:25 consensus_to_signal: 10 params
- src/execution/live.py:394 submit_signal: 11 params
- src/execution/live.py:490 _submit_signal_auto: 11 params
- src/execution/live.py:517 _submit_signal_with_approval: 12 params
- ... +16 more (audit_findings.json)

### [low] duplicate-module-name x17
Fix: Merge or rename (two orchestrator.py etc.)
- rag_mongo/db.py:0 db.py: rag_mongo/db.py, kg/db.py
- rag_mongo/ingest.py:0 ingest.py: rag_mongo/ingest.py, kg/ingest.py
- review/config.py:0 config.py: review/config.py, src/config.py, src/terminal/config.py, orchestrator/config.py, rag_mongo/config.py, kg/config.py
- src/agent_control/cli.py:0 cli.py: src/agent_control/cli.py, src/terminal/cli.py
- src/data/quality_gate.py:0 quality_gate.py: src/data/quality_gate.py, .claude/hooks/quality_gate.py
- src/diagnostics/metrics.py:0 metrics.py: src/diagnostics/metrics.py, src/intelligence/metrics.py
- src/features/pipeline.py:0 pipeline.py: src/features/pipeline.py, rag_mongo/pipeline.py
- src/intelligence/client.py:0 client.py: src/intelligence/client.py, src/terminal/client.py
- src/intelligence/onchain/base.py:0 base.py: src/intelligence/onchain/base.py, src/intelligence/providers/base.py, src/execution/base.py
- src/intelligence/onchain/schema.py:0 schema.py: src/intelligence/onchain/schema.py, src/engines/schema.py, kg/schema.py
- src/quality/registry.py:0 registry.py: src/quality/registry.py, src/tuning/registry.py, src/strategies/registry.py, src/terminal/registry.py, src/upgrade/registry.py, src/mathcore/registry.py, src/runtime/registry.py, tools/registry.py
- src/risk/gate.py:0 gate.py: src/risk/gate.py, src/tuning/gate.py, src/mathcore/folklore/gate.py
- ... +5 more (audit_findings.json)

### [low] mutable-global x16
Fix: Encapsulate in class / pass explicitly; guard with lock if shared
- audit3.py:797 module-level mutable 'parts'
- audit3.py:823 module-level mutable 'pm'
- audit3.py:827 module-level mutable 'tg'
- audit3.py:904 module-level mutable 'names'
- audit3.py:1141 module-level mutable 'used'
- audit3.py:1197 module-level mutable 'tool_out'
- audit3.py:1204 module-level mutable 'prev'
- audit3.py:1210 module-level mutable 'prev'
- audit3.py:1216 module-level mutable 'score'
- audit3.py:1230 module-level mutable 'out'
- audit3.py:1240 module-level mutable 'seen'
- audit3.py:1241 module-level mutable 'top'
- ... +4 more (audit_findings.json)

### [low] duplicate-code x8
Fix: Extract shared function
- src/data/storage.py:733 identical body: src/data/storage.py:733 _get_lock, src/data/timescale_storage.py:421 _get_lock
- src/engine/orchestrator.py:421 identical body: src/engine/orchestrator.py:421 _cfg, src/engine/signal_engine.py:369 _cfg
- src/execution/paper.py:173 identical body: src/execution/paper.py:173 to_dict, src/execution/live.py:154 to_dict
- src/intelligence/providers/bybit_provider.py:123 identical body: src/intelligence/providers/bybit_provider.py:123 close, src/intelligence/providers/okx_provider.py:116 close, src/intelligence/providers/binance_provider.py:123 close
- src/intelligence/providers/bybit_provider.py:232 identical body: src/intelligence/providers/bybit_provider.py:232 _fetch_funding_data, src/intelligence/providers/okx_provider.py:231 _fetch_funding_data
- src/intelligence/providers/bybit_provider.py:260 identical body: src/intelligence/providers/bybit_provider.py:260 _fetch_oi_data, src/intelligence/providers/okx_provider.py:259 _fetch_oi_data
- src/quality/registry.py:288 identical body: src/quality/registry.py:288 _validate_against_schema, src/mathcore/registry.py:233 _validate_against_schema
- src/quality/registry.py:454 identical body: src/quality/registry.py:454 visit, src/mathcore/registry.py:389 visit

### [low] deep-nesting x6
Fix: Use guard clauses / early returns
- src/agent_control/cli.py:157 _task: nesting depth 7
- src/agent_control/cli.py:369 _arguments: nesting depth 16
- src/agent_control/cli.py:455 _dispatch: nesting depth 9
- src/agent_control/gitstate.py:118 parse_status_v2: nesting depth 7
- src/agent_control/gitstate.py:172 parse_worktrees: nesting depth 9
- src/ecc/ecdsa_scan.py:94 extract_ecdsa_signatures: nesting depth 7

### [low] high-coupling x3
Fix: Fan-out >25: introduce a facade / split module
- src.api.main:0 imports 55 internal modules
- src.engine.orchestrator:0 imports 42 internal modules
- src.engine.signal_engine:0 imports 29 internal modules

### [low] orphan-untested x3
Fix: Delete, or import + test it
- .claude/hooks/completion_gate.py:0 never imported by src, never referenced in tests
- .claude/hooks/precompact_checkpoint.py:0 never imported by src, never referenced in tests
- .claude/hooks/task_lifecycle.py:0 never imported by src, never referenced in tests

### [low] unused-definition x2
Fix: Delete or wire in (verify no dynamic use first)
- src/features/microstructure.py:40 VPINState referenced nowhere else
- src/tuning/runner.py:64 SelfTuningDisabledError referenced nowhere else

## trading (score 55)

### [high] lookahead-shift x1
Fix: shift(-n) leaks future data into features
- crypto-predictor/backtest.py:33 df['close'].shift(-horizon)

### [high] retry-no-backoff x1
Fix: while True + except without sleep/backoff = hammering exchange
- src/workers/orchestrator.py:105 while True + try/except without sleep/backoff/await

### [medium] float-money x50
Fix: Use Decimal/integer minor units for money
- src/api/main.py:2371 float(cast(float, p['quantity']))
- src/api/main.py:2957 float(executor.equity_usd)
- src/diagnostics/signal_debugger.py:330 float(pnl_usd)
- src/diagnostics/trade_journal.py:234 float(pnl)
- src/diagnostics/trade_journal.py:235 float(fee)
- src/diagnostics/trade_journal.py:236 float(gross_pnl)
- src/diagnostics/trade_journal.py:237 float(fee)
- src/ecc/secp256k1_cluster.py:163 float(u.get('amount', u.get('value', 0.0)))
- src/ecc/utxo_curve.py:72 float(u.get('value_btc', u.get('amount', 0.0)))
- src/engine/orchestrator.py:229 float(cast('float', raw.get('quantity') or 0.0)
- src/engine/orchestrator.py:233 float(cast('float', raw.get('entry_price') or )
- src/engine/orchestrator.py:237 float(cast('float', raw.get('notional_usd') or)
- ... +38 more (audit_findings.json)

### [medium] no-rate-limit-handling x13
Fix: Handle 429/Retry-After, enableRateLimit
- .claude/skills/crypto-architect/scripts/validate_arch.py:0 exchange calls without 429/rate-limit handling
- src/data/feeds.py:0 exchange calls without 429/rate-limit handling
- src/execution/exchange_contract.py:0 exchange calls without 429/rate-limit handling
- src/execution/idempotency.py:0 exchange calls without 429/rate-limit handling
- src/execution/live_fsm_integration.py:0 exchange calls without 429/rate-limit handling
- src/execution/order_fsm.py:0 exchange calls without 429/rate-limit handling
- src/execution/order_manager.py:0 exchange calls without 429/rate-limit handling
- src/features/derivatives.py:0 exchange calls without 429/rate-limit handling
- src/intelligence/providers/base.py:0 exchange calls without 429/rate-limit handling
- src/intelligence/providers/binance_provider.py:0 exchange calls without 429/rate-limit handling
- src/intelligence/providers/bybit_provider.py:0 exchange calls without 429/rate-limit handling
- src/intelligence/providers/okx_provider.py:0 exchange calls without 429/rate-limit handling
- ... +1 more (audit_findings.json)

### [medium] no-precision-handling x9
Fix: Use exchange precision/tick/step; Decimal quantize
- .claude/skills/crypto-architect/scripts/validate_arch.py:0 orders without precision/tick/step handling
- scripts/check_static_invariants.py:0 orders without precision/tick/step handling
- src/config.py:0 orders without precision/tick/step handling
- src/data/feeds.py:0 orders without precision/tick/step handling
- src/execution/exchange_contract.py:0 orders without precision/tick/step handling
- src/execution/order_manager.py:0 orders without precision/tick/step handling
- src/execution/router.py:0 orders without precision/tick/step handling
- src/features/derivatives.py:0 orders without precision/tick/step handling
- src/intelligence/providers/base.py:0 orders without precision/tick/step handling

### [medium] lookahead-shift x1
Fix: shift(-n) leaks future data into features
- crypto-predictor/validation.py:94 df['close'].shift(-horizon)

## tool: ruff
```
(skipped: tool not installed)
```

## tool: bandit
```
(skipped: tool not installed)
```

## tool: vulture
```
(skipped: tool not installed)
```

## tool: radon-cc
```
(skipped: tool not installed)
```

## tool: pip-audit
```
(skipped: tool not installed)
```

## tool: gitleaks
```
(skipped: tool not installed)
```

## tool: hadolint
```
(skipped: tool not installed)
```

## tool: actionlint
```
(skipped: tool not installed)
```

## tool: trivy
```
(skipped: tool not installed)
```