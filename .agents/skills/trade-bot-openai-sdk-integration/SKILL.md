---
name: trade-bot-openai-sdk-integration
description: Use when evaluating or implementing OpenAI API, Responses API, Agents SDK, MCP, tool calling, structured outputs, SDK upgrades, or an LLM-assisted workflow for Trade-Bot.
---

# OpenAI SDK and agent integration

1. First establish a concrete user-facing use case. Do not add an OpenAI API or Agents SDK dependency to the trading core merely because a Cookbook example exists. Keep optional LLM workflows outside signal generation, risk authorization, sizing, and order execution unless a separately reviewed design explicitly changes that boundary.
2. Consult current official references: the OpenAI Cookbook adoption map, current OpenAI API docs, the official `openai-python` SDK, and the official Agents SDK docs. Some Cookbook recipes are archived and may use old endpoints or signatures; never treat them as current SDK specifications.
3. Keep provider transport, request normalization, structured response parsing, typed run context, tool registration/execution, and domain logic in distinct layers. Hide provider-specific behavior behind an adapter and keep deterministic behavior available without API credentials.
4. Prefer a Pydantic model or strict JSON Schema for structured results. Explicitly handle refusals, incomplete responses, malformed data, unsupported tool requests, timeouts, and provider errors before consuming output. Schema-valid output is not proof that a financial conclusion is correct.
5. Define tools as narrow typed contracts. Validate arguments; authenticate and authorize at execution time; make read-only tools the default; require explicit approval for state-changing tools. Never expose exchange credentials, raw shell access, arbitrary Git writes, or order submission to an LLM tool.
6. Set explicit request timeouts and bounded retry budgets appropriate to the call path. The official Python SDK currently retries selected connection/408/409/429/5xx failures by default; account for that behavior instead of wrapping it in an unbounded second retry loop. Do not automatically retry non-idempotent side effects.
7. Trace model/provider/version, prompt/schema version, latency, token/cost usage when available, validated tool names, result status, refusal/timeout category, and correlation ID. Redact keys, account identifiers, exchange credentials, and sensitive payloads. Do not send repository secrets or private market/order data to a hosted model without an explicit reviewed data-handling decision.
8. Evaluate with frozen deterministic fixtures, known expected outcomes, negative examples, tool-call assertions, and regression sets. Keep evaluator-only ground truth out of the model input. Use fakes in CI so tests never require network access, billing, or API keys.
9. Add the SDK as an optional extra only when the feature needs it. Document environment variables and costs, set safe defaults, and ensure disabling or losing the LLM service leaves the trading and risk path unchanged.

## Required design review

Before merging a runtime integration, document its threat model, data egress, permission surface, timeouts/retries, rate limits, budget limits, trace retention/redaction, fallback behavior, evaluation set, and proof that the agent cannot bypass risk or approval controls.
