---
name: trade-bot-evaluation-optimization
description: Use when benchmarking, optimizing models or runtime paths, evaluating prompts/agents, tuning parameters, comparing SDKs, or introducing regression evaluations in Trade-Bot.
---

# Evaluation and optimization workflow

1. State the target outcome and protected invariants before changing anything: correctness, risk limits, causality, reproducibility, resource usage, latency, availability, and operator control.
2. Define a repeatable baseline and a representative dataset before optimization. For trading/model changes, prefer time-aware walk-forward or out-of-sample replay and deterministic PAPER fixtures. Avoid training/evaluation leakage and never tune on the final holdout.
3. Separate the task input, candidate output, evaluator-only expected result, score, textual feedback, trace, and optimizer state. Do not leak hidden expected outcomes into an LLM prompt or candidate generator.
4. Score more than parsing or test success: include domain correctness, risk-related outcomes, calibration, failure categories, regressions, latency, resource use, and cost when applicable. Do not use aggregate PnL or one benchmark number as the only safety measure.
5. Record candidate lineage and enough provenance to reproduce the result: commit/config/schema/model/prompt version, data window, seed, run ID, metric definitions, baseline/candidate results, and rejected regressions.
6. Optimize a measured bottleneck. Compare end-to-end effects and regression risk; prefer bounded experiments, caching only with correct invalidation, batching only for non-time-critical work, and smaller models only when evaluation preserves required quality.
7. For agents and LLM features, include positive, negative, adversarial, refusal, malformed-tool-input, timeout, rate-limit, and privacy tests. Model-judge feedback is evidence to triage, not authority; keep deterministic assertions for contractual behavior.
8. Use GitHub Actions for all repository tests, benchmarks, lint, build, and security/quality gates. Local checks are prohibited; the permitted diagnostic route is the SHA-bound CI notice via `python3 scripts/local_checks.py prepare`.
9. Report measured deltas, uncertainty, dataset limitations, failed scenarios, and checks not run. Do not claim a speedup or quality improvement without current reproducible evidence.
