# Cloud-only PR review + retrieval (Component 5)

Source: [Managed Agents: CMA with MongoDB Atlas](https://platform.claude.com/cookbook/managed-agents-cma-with-mongodb-atlas)

## Why this can't be the cookbook's actual agent

The cookbook's fraud-review agent runs on Claude Managed Agents (CMA) --
Anthropic's hosted, stateful agent runtime. CMA requires an
`ANTHROPIC_API_KEY` and Managed Agents beta access. This project has neither
(Pro/Max subscription only), so the CMA session/environment/resources model
itself has no local or cloud-subscription equivalent -- same blocker as the
Managed Agents plan-big/execute-small cookbook (see `docs/ORCHESTRATOR.md`).

## What IS liftable: the four retrieval patterns

The cookbook explicitly frames vector / full-text / hybrid / graph search as
"liftable building blocks" independent of CMA. `review/retrieval.py`
implements exactly these four as plain MongoDB Atlas queries:

| Pattern | Cookbook | This repo |
|---|---|---|
| Vector | `$vectorSearch` via CMA tool | `review.retrieval.vector()` -> reuses `rag_mongo.retrieve.vector_search` |
| Full-text | Atlas `$search` | `review.retrieval.full_text()` -- new `fulltext_index` (see `rag_cli.py init`) |
| Hybrid | RRF-combined vector+text | `review.retrieval.hybrid()` -- reciprocal rank fusion, bounded to top_k |
| Graph | Traversal via CMA tool | `review.retrieval.graph()` -> reuses `kg` component's edge collection |

## What replaces the CMA runtime: GitHub Actions + Claude Code Action

Per your explicit requirement -- review/test happens ONLY in GitHub's cloud
service, never locally -- this component is a GitHub Actions workflow
(`.github/workflows/claude-review.yml`), not a script you run yourself:

1. **`retrieve-context` job**: checks out the PR, runs
   `review/build_context.py` against your Atlas cluster (bounded to
   `REVIEW_MAX_CONTEXT_ITEMS`), uploads `context.json` as an artifact.
2. **`claude-review` job**: downloads that artifact, runs
   `anthropics/claude-code-action@v1` authenticated via your Pro/Max
   subscription (`CLAUDE_CODE_OAUTH_TOKEN`, not an API key), posts an
   advisory review comment on the PR.

This mirrors the cookbook's human-in-the-loop fraud-review design: Claude
never approves or merges -- it flags issues for a human reviewer, same
division of labor as the CMA agent's `decide()`/`escalate()` gate.

## One-time setup (required before this workflow can run)

1. Install the Claude GitHub App: https://github.com/apps/claude (grant it
   access to this repo).
2. Generate a subscription OAuth token, once, locally:
   `claude setup-token` (opens a browser; requires Pro/Max login). This
   token is valid ~1 year and is NOT an API key.
3. Add two repository secrets (Settings -> Secrets and variables -> Actions):
   - `CLAUDE_CODE_OAUTH_TOKEN` -- the token from step 2
   - `MONGODB_URI` -- the same Atlas connection string from your `.env`
4. Confirm workflow permissions (Settings -> Actions -> General ->
   Workflow permissions) allow "Read and write permissions" -- a
   read-only default silently blocks the review comment from posting.
5. Run `rag_cli.py init` once (locally, with `.env` filled in) so the
   `fulltext_index` this workflow depends on actually exists in Atlas.

## Known limitation to watch for

Anthropic's `claude-code-action` has had OAuth-token propagation issues
after a Pro-to-Max plan change (upstream issue #1281) -- if the action
starts failing auth after a plan change, regenerate the token with
`claude setup-token` and update the secret.

## What was NOT tested locally, and why

Per your instruction, this component's correctness can only be verified by
GitHub's own cloud runners -- there is no local Actions runner in this
project's toolchain to execute against. What WAS verified before delivery:
YAML structure parses correctly, and every Python file in `review/`
byte-compiles and imports cleanly. The actual `$search`/`$vectorSearch`
queries, the OAuth handshake, and the posted PR comment can only be
confirmed by opening a real pull request after completing the setup above.
