# FastAPI Route Reference

Index version 1.0.1.

Generated from app decorators in src/api/main.py at source snapshot 9128e3da8b9542efef5c89706b8f2e8f3512f054; src/api/main.py was unchanged through main HEAD 6232016c94db2025e9afddf4aa3e3e2331dc7b2f. Line numbers shift with edits. This documents paths, handler names and visible route dependencies, not a proof that each role can use each route. Re-read role policy, dependencies, second factor, readiness and rate limit code before changing an endpoint.

Total app-decorator routes discovered: 68.

## Liveness, readiness and summary

| Method | Path | Handler | Line | Declared route dependencies |
| --- | --- | --- | ---: | --- |
| GET | /livez | livez | 1152 | Depends(api_key_header) |
| GET | /readyz | readyz | 1162 | Depends(api_key_header) |
| GET | /health | health | 1182 | Depends(api_key_header) |
| GET | /metrics | prometheus_metrics | 1195 | Depends(api_key_header) |
| GET | /status | status | 1204 | Depends(api_key_header), Depends(require_ready) |
| GET | /crypto-box/status | crypto_box_status | 1437 | Depends(api_key_header) |
| GET | /self-tuning/status | self_tuning_status | 1739 | Depends(api_key_header) |
| GET | /debug/health | debug_health | 2183 | Depends(api_key_header) |
| GET | /orders/{order_id}/status | get_order_status | 2438 | Depends(api_key_header) |
| GET | /recovery/status | recovery_status | 2562 | Depends(api_key_header) |
| GET | /models/status | models_status | 2663 | Depends(api_key_header), Depends(require_ready) |

## All routes

| Method | Path | Handler | Line | Decorator dependencies | Handler arguments |
| --- | --- | --- | ---: | --- | --- |
| GET | /livez | livez | 1152 | Depends(api_key_header) | |
| GET | /readyz | readyz | 1162 | Depends(api_key_header) | |
| GET | /health | health | 1182 | Depends(api_key_header) | |
| GET | /metrics | prometheus_metrics | 1195 | Depends(api_key_header) | |
| GET | /status | status | 1204 | Depends(api_key_header), Depends(require_ready) | |
| GET | /trades | trades | 1271 | Depends(api_key_header) | symbol : Annotated[Optional[str], Query()] = None, limit : Annotated[int, Query(ge=1, le=1000)] = 100, offset : Annotated[int, Query(ge=0, le=10000)] = 0 |
| GET | /missed-trades | missed_trades | 1323 | Depends(api_key_header) | symbol : Annotated[Optional[str], Query()] = None, limit : Annotated[int, Query(ge=1, le=500)] = 50 |
| GET | /equity | equity_curve | 1360 | Depends(api_key_header) | limit : Annotated[int, Query(ge=1, le=10000)] = 1440 |
| GET | /regime/{timeframe} | regime | 1393 | Depends(api_key_header) | timeframe : str |
| GET | /crypto-box/status | crypto_box_status | 1437 | Depends(api_key_header) | |
| GET | /approvals | approvals | 1459 | Depends(api_key_header), Depends(require_ready) | |
| POST | /approvals/{request_id}/resolve | resolve_approval | 1480 | Depends(requires(Permission.APPROVE_TRADE)), Depends(require_healthy_security_controls), Depends(require_ready) | request_id : str, body : ResolveApprovalRequest, request : Request |
| POST | /execution-mode | set_execution_mode | 1539 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_healthy_security_controls) | body : SetExecutionModeRequest |
| GET | /risk-controls | get_risk_controls | 1600 | Depends(api_key_header), Depends(require_ready) | |
| POST | /risk-controls | set_risk_controls | 1625 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_healthy_security_controls), Depends(require_ready) | body : SetRiskControlsRequest, request : Request |
| GET | /model-metrics | model_metrics | 1689 | Depends(api_key_header) | timeframe : Optional[str] = None |
| GET | /self-tuning/status | self_tuning_status | 1739 | Depends(api_key_header) | |
| POST | /self-tuning/pause | self_tuning_pause | 1795 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | body : SelfTuningPauseRequest, request : Request |
| POST | /self-tuning/resume | self_tuning_resume | 1824 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | body : SelfTuningPauseRequest, request : Request |
| POST | /self-tuning/rollback/{param_name} | self_tuning_rollback | 1850 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | param_name : str, body : SelfTuningRollbackRequest, request : Request |
| WEBSOCKET | /ws | websocket_endpoint | 2007 | | ws : WebSocket |
| GET | /debug/health | debug_health | 2183 | Depends(api_key_header) | |
| GET | /debug/audit | debug_audit | 2194 | Depends(api_key_header) | limit : Annotated[int, Query(ge=1, le=500)] = 50 |
| GET | /audit/integrity | audit_chain_integrity | 2209 | Depends(api_key_header) | limit : Annotated[int, Query(ge=1, le=500)] = 50 |
| GET | /debug/drift | debug_drift | 2272 | Depends(api_key_header) | |
| GET | /debug/reconcile | debug_reconcile | 2338 | Depends(api_key_header), Depends(require_ready) | |
| POST | /debug/selftest | debug_selftest | 2410 | Depends(api_key_header) | request : Request |
| GET | /orders/{order_id}/status | get_order_status | 2438 | Depends(api_key_header) | order_id : str |
| GET | /ledger | get_unified_ledger_snapshot | 2508 | Depends(api_key_header) | |
| GET | /recovery/status | recovery_status | 2562 | Depends(api_key_header) | |
| POST | /recovery/acknowledge | recovery_acknowledge | 2606 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | body : RecoveryAcknowledgeRequest, request : Request |
| GET | /models/status | models_status | 2663 | Depends(api_key_header), Depends(require_ready) | |
| POST | /models/retrain | trigger_retrain | 2701 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | body : RetrainRequest, request : Request |
| POST | /backfill | trigger_backfill | 2759 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | body : BackfillRequest, request : Request |
| GET | /horizons | horizons | 2790 | Depends(api_key_header) | |
| GET | /capital-floor | capital_floor | 2839 | Depends(api_key_header), Depends(require_ready) | |
| POST | /capital-floor/re-authorize | capital_floor_re_authorize | 2859 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_ready) | body : CapitalFloorReAuthorizeRequest, request : Request |
| GET | /strategies/attribution | get_strategy_attribution | 2904 | Depends(api_key_header) | |
| GET | /strategies/gauntlet | get_strategy_gauntlet | 2934 | Depends(api_key_header), Depends(require_ready) | |
| GET | /strategies/allocation | get_strategy_allocation | 2991 | Depends(api_key_header) | |
| POST | /strategies/{strategy_id}/re-enable | re_enable_strategy | 3060 | Depends(requires(Permission.CHANGE_EXECUTION_MODE)), Depends(require_healthy_security_controls), Depends(require_ready) | strategy_id : str, body : StrategyReEnableRequest, request : Request |
| GET | /strategies/portfolio | get_strategy_portfolio | 3129 | Depends(api_key_header) | timeframe : Optional[str] = None |
| GET | /strategies/stress-test | get_allocation_stress_test | 3190 | Depends(api_key_header) | |
| GET | /performance-drift | get_performance_drift | 3280 | Depends(api_key_header) | |
| GET | /intelligence/coverage | get_intelligence_coverage | 3320 | Depends(api_key_header) | |
| GET | /intelligence/providers | get_intelligence_providers | 3357 | Depends(api_key_header) | |
| POST | /risk/size-check | risk_size_check | 3434 | Depends(api_key_header) | body : SizeCheckRequest, request : Request |
| GET | /venues | venues_status | 3563 | Depends(api_key_header) | |
| POST | /venues/{venue}/reconnect | reconnect_venue | 3583 | | venue : str, body : VenueReconnectRequest, request : Request |
| POST | /venues/{venue}/connect | connect_venue | 3607 | | venue : str, body : VenueReconnectRequest, request : Request |
| POST | /venues/{venue}/disconnect | disconnect_venue | 3631 | | venue : str, body : VenueReconnectRequest, request : Request |
| POST | /venues/{venue}/verify | verify_venue_account | 3657 | | venue : str, body : VenueReconnectRequest, request : Request |
| GET | /controls | control_surface | 3673 | Depends(api_key_header) | |
| POST | /controls/{name} | set_control | 3695 | Depends(api_key_header) | name : str, body : SetControlRequest, request : Request |
| GET | /runtime | runtime_overview | 3849 | | |
| GET | /runtime/components | runtime_components | 3855 | | component_type : Optional[str] = None |
| GET | /runtime/components/{component_id} | runtime_component | 3868 | | component_id : str |
| GET | /runtime/dependencies | runtime_dependencies | 3875 | | |
| GET | /runtime/changes | runtime_changes | 3880 | | |
| GET | /runtime/changes/{change_id} | runtime_change | 3885 | | change_id : str |
| GET | /runtime/traces | runtime_traces | 3891 | | limit : int = Query(default=20, ge=1, le=200) |
| GET | /runtime/traces/{trace_id} | runtime_trace | 3896 | | trace_id : str |
| GET | /runtime/agent | runtime_agent_ledgers | 3902 | | |
| POST | /runtime/changes | runtime_submit_change | 3908 | | body : RuntimeChangeBody, request : Request |
| POST | /runtime/changes/{change_id}/{verb} | runtime_decide | 3917 | | change_id : str, verb : str, body : RuntimeDecisionBody, request : Request |
| POST | /runtime/components/{component_id}/{action} | runtime_component_action | 3928 | | component_id : str, action : str, body : RuntimeActionBody, request : Request |
| POST | /runtime/desired | runtime_set_desired | 3939 | | body : RuntimeDesiredBody, request : Request |
| POST | /runtime/reconcile | runtime_reconcile | 3948 | | body : OperatorFactor, request : Request |

## Route families

- **Trading/history:** status, trades, missed trades, equity, regime, ledger and order status.
- **Approvals/controls:** approvals, execution mode, risk controls and generic control surface.
- **Model/tuning:** model metrics/status/retrain, self-tuning status/pause/resume/rollback and backfill.
- **Risk/recovery/strategies:** capital floor, recovery, size-check, strategy attribution/allocation/portfolio/gauntlet/stress/re-enable and performance drift.
- **Venue/intelligence:** venue connection/reconnect/disconnect/verify, provider coverage, horizon and crypto-box status.
- **Diagnostics/audit:** health, audit, drift, reconcile, selftest, integrity and metrics.
- **Runtime:** components, dependencies, changes, traces, agent ledger, desired state and reconcile.
- **Realtime:** WebSocket /ws.

## Authentication and safe mutation rule

- **src/api/auth.py** handles X-API-Key; API_READONLY_KEY maps to read-only role, API_SECRET_KEY to trade-authorizing role. Bad client keys yield 401; invalid server-side key configuration yields 503.
- Sensitive operations may additionally require OPERATOR_SECRET, readiness, permissions or rate limits. Verify each actual handler and dependency; HTTP method alone is not the policy.
- Browser WS has an api_key query compatibility path with greater access-log exposure than header auth; topic permission filtering still applies.
- /livez, /readyz and /health are distinct.
