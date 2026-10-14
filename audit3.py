#!/usr/bin/env python3
"""audit3.py - multi-discipline engineering audit for a Python trading / on-chain bot.

Disciplines: software, python, quality, security, crypto, blockchain, trading, reliability,
observability, performance, data, ml, api, devops, docs, frontend, governance.

Usage (repo root):
  python3 audit3.py                      # fast static audit (~1 min)
  python3 audit3.py --slow               # + pytest coverage, mypy
  python3 audit3.py --only security,crypto,blockchain
  python3 audit3.py --fail-on high       # exit 1 in CI if any finding >= high
  python3 audit3.py --sarif out.sarif    # GitHub code-scanning upload

Outputs: audit_report.md (paste/upload this), audit_findings.json (full).
Re-running diffs against the previous audit_findings.json (NEW / FIXED).
Optional tools, auto-skipped if missing:
  pip install ruff bandit vulture radon pip-audit pytest-cov mypy
  binaries: gitleaks, hadolint, actionlint, shellcheck, trivy
Nothing leaves your machine; secrets are redacted in all output.
"""
import argparse, ast, collections, concurrent.futures as cf, json, math, os, pathlib, re, shutil, subprocess, sys

ap = argparse.ArgumentParser()
ap.add_argument("--slow", action="store_true")
ap.add_argument("--src", default="src")
ap.add_argument("--max", type=int, default=12, help="examples per rule in report")
ap.add_argument("--only", default="", help="comma list of domains")
ap.add_argument("--skip", default="", help="comma list of domains")
ap.add_argument("--no-tools", action="store_true")
ap.add_argument("--fail-on", default="", choices=["", "critical", "high", "medium", "low"])
ap.add_argument("--sarif", default="")
args = ap.parse_args()

ROOT = pathlib.Path(".").resolve()
SKIP = {".git", "venv", ".venv", "env", "node_modules", "__pycache__", "trade_bot.egg-info", ".mypy_cache",
        ".ruff_cache", ".pytest_cache", "build", "dist", ".tox", ".idea", ".vscode", "site-packages"}
SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
W = {"critical": 15, "high": 6, "medium": 2, "low": 0.5, "info": 0}

# rule: (domain, default severity, fix hint)
R = {
 # software
 "god-file": ("software", "medium", "Split by responsibility (router/service/repository)"),
 "long-function": ("software", "medium", "Extract helpers, aim <60 lines"),
 "high-complexity": ("software", "medium", "Cyclomatic >20: split branches, use tables/strategy objects"),
 "deep-nesting": ("software", "low", "Use guard clauses / early returns"),
 "too-many-params": ("software", "low", "Group into a dataclass/config object"),
 "import-cycle": ("software", "medium", "Invert dependency or extract shared module"),
 "layer-violation": ("software", "medium", "Live path must not import research/experimental code"),
 "high-coupling": ("software", "low", "Fan-out >25: introduce a facade / split module"),
 "duplicate-code": ("software", "low", "Extract shared function"),
 "duplicate-module-name": ("software", "low", "Merge or rename (two orchestrator.py etc.)"),
 "unused-definition": ("software", "low", "Delete or wire in (verify no dynamic use first)"),
 "orphan-untested": ("software", "low", "Delete, or import + test it"),
 "wildcard-import": ("software", "low", "Import explicit names"),
 "mutable-global": ("software", "low", "Encapsulate in class / pass explicitly; guard with lock if shared"),
 "deep-loop-nesting": ("software", "low", "Flatten loops / vectorize"),
 "syntax": ("python", "high", "File does not parse"),
 # python
 "mutable-default": ("python", "medium", "Use None + create inside function"),
 "bare-except": ("python", "medium", "Catch specific exceptions"),
 "baseexception": ("python", "medium", "Never catch BaseException unless re-raising"),
 "swallowed-exception": ("python", "high", "Log with traceback or re-raise; never pass silently on money path"),
 "redefined-function": ("python", "high", "Second def shadows the first (a test is silently NOT run)"),
 "assert-in-prod": ("python", "low", "asserts vanish under python -O; raise explicit errors"),
 "float-equality": ("python", "low", "Use math.isclose / Decimal"),
 "is-literal": ("python", "low", "Use == for literals"),
 "naive-datetime": ("python", "low", "Use datetime.now(timezone.utc)"),
 "get-event-loop": ("python", "low", "Use asyncio.run / get_running_loop"),
 "lru-cache-method": ("python", "low", "lru_cache on methods leaks instances; cache on function or per-instance"),
 "open-no-with": ("python", "low", "Use with open(..., encoding=...)"),
 "print-in-src": ("python", "info", "Use logging"),
 # quality
 "assertless-test": ("quality", "medium", "Test without assertion proves nothing; assert behaviour"),
 "coverage-chasing-tests": ("quality", "medium", "Merge into real module tests; assert behaviour, not lines"),
 "skipped-tests": ("quality", "low", "Fix or delete skipped/xfail tests"),
 "sleep-in-test": ("quality", "low", "Use fake clock/event; sleeps cause flakiness"),
 "mock-heavy-test": ("quality", "low", "Mock:assert ratio high - test behaviour at boundaries"),
 "money-path-untested": ("quality", "high", "Money/security module has no test referencing it"),
 "test-config-weak": ("quality", "low", "Add cov-fail-under, --strict-markers, pytest-timeout"),
 "no-property-or-fuzz-tests": ("quality", "low", "Add hypothesis tests for sizing/precision/FSM"),
 "no-mutation-testing": ("quality", "low", "Run mutmut/cosmic-ray on risk/ and execution/"),
 "no-tests": ("quality", "high", "No tests found"),
 # security
 "hardcoded-secret": ("security", "critical", "Move to env/vault, rotate the key"),
 "secret-in-file": ("security", "critical", "Remove, rotate, scrub history"),
 "private-key-in-repo": ("security", "critical", "Remove, rotate, scrub history"),
 "secret-file-in-history": ("security", "critical", "Rotate keys; scrub with git filter-repo"),
 "tracked-secret-file": ("security", "high", "git rm --cached; add to .gitignore; rotate"),
 "eval-exec": ("security", "high", "Remove eval/exec"),
 "unsafe-deserialization": ("security", "high", "Use json/safe formats; never load untrusted pickle/yaml"),
 "shell-exec": ("security", "high", "Use subprocess list args, shell=False"),
 "sql-injection-risk": ("security", "high", "Parameterize queries"),
 "tls-verify-off": ("security", "high", "Never verify=False; pin CA via certifi"),
 "log-secret": ("security", "high", "Redact secrets before logging"),
 "ssrf-candidate": ("security", "medium", "Allowlist hosts; block private ranges"),
 "path-traversal-candidate": ("security", "medium", "Resolve + confine to base dir"),
 "pty-shell-surface": ("security", "high", "Remote shell: loopback only, strong auth, off by default in live"),
 "xxe-risk": ("security", "low", "Use defusedxml"),
 "insecure-tempfile": ("security", "medium", "Use tempfile.mkstemp/NamedTemporaryFile"),
 "chmod-777": ("security", "medium", "Use least privilege (0o600/0o700)"),
 "token-in-url": ("security", "high", "Tokens in URL leak to logs; use header/subprotocol/ticket"),
 "bind-all-interfaces": ("security", "medium", "Bind 127.0.0.1 behind reverse proxy"),
 "debug-enabled": ("security", "medium", "Disable debug in prod"),
 "jwt-misuse": ("security", "high", "Verify signature, pin algorithms, never 'none'"),
 "dependency-confusion": ("security", "high", "Remove extra-index-url; pin hashes"),
 # crypto
 "weak-hash": ("crypto", "medium", "Use SHA-256/BLAKE2 (not MD5/SHA1) for security purposes"),
 "weak-cipher": ("crypto", "high", "Use AES-GCM / ChaCha20-Poly1305 via cryptography"),
 "ecb-mode": ("crypto", "high", "ECB leaks patterns; use AEAD"),
 "weak-random": ("crypto", "high", "Use secrets / os.urandom for keys, nonces, tokens"),
 "static-nonce-iv-salt": ("crypto", "high", "Nonce/IV must be unique per message; salt per secret"),
 "low-kdf-iterations": ("crypto", "medium", "PBKDF2 >=600k (SHA-256) or use scrypt/argon2"),
 "small-rsa-key": ("crypto", "high", "RSA >= 2048 (prefer 3072) or Ed25519/ECDSA"),
 "tls-weak-config": ("crypto", "high", "Use default secure context, TLS>=1.2, verify hostname"),
 "non-constant-time-compare": ("crypto", "medium", "Use hmac.compare_digest for MACs/tokens/signatures"),
 "custom-crypto-primitive": ("crypto", "high", "Don't roll your own; use cryptography/libsodium. Quarantine research code"),
 "pq-claim-without-impl": ("crypto", "medium", "Code/docs claim post-quantum but no PQ library imported - fix claim or implement"),
 "deprecated-crypto-lib": ("crypto", "medium", "pycrypto is dead; use cryptography or pycryptodomex"),
 "pure-python-ecdsa": ("crypto", "low", "python-ecdsa is not side-channel safe; use cryptography/coincurve"),
 # blockchain
 "chain-key-material": ("blockchain", "critical", "Private key/seed/xprv/WIF in repo: move to HSM/vault, rotate funds"),
 "rpc-url-credentials": ("blockchain", "high", "Credentials/API key inside RPC URL: move to env/secret store"),
 "tx-no-chainid": ("blockchain", "high", "Tx without chainId is replayable across chains"),
 "hardcoded-gas": ("blockchain", "low", "Estimate gas / use fee oracle with caps"),
 "float-chain-units": ("blockchain", "medium", "wei/satoshi/lamport math must use int/Decimal, not float"),
 "no-confirmation-wait": ("blockchain", "medium", "Wait for receipt + N confirmations before treating tx as final"),
 "no-reorg-handling": ("blockchain", "medium", "Handle reorgs: confirmations/finalized tag, rollback derived data"),
 "no-block-checkpoint": ("blockchain", "low", "Persist last processed block for idempotent resume"),
 "oracle-no-staleness": ("blockchain", "medium", "Check oracle updatedAt/round and deviation bounds"),
 "swap-no-slippage-deadline": ("blockchain", "high", "Set amountOutMin and deadline (MEV/sandwich protection)"),
 "unlimited-approval": ("blockchain", "medium", "Approve exact amounts; revoke after use"),
 "provider-no-timeout": ("blockchain", "medium", "Set request timeout + fallback provider"),
 "no-checksum-validation": ("blockchain", "low", "Validate EIP-55 checksum addresses"),
 "hardcoded-addresses": ("blockchain", "low", "Move contract addresses to per-chain config"),
 # trading
 "order-no-idempotency": ("trading", "high", "Send client order id; dedupe on retry/restart"),
 "order-no-error-handling": ("trading", "medium", "Handle timeouts/partial fills/rejects explicitly"),
 "retry-no-backoff": ("trading", "high", "while True + except without sleep/backoff = hammering exchange"),
 "no-rate-limit-handling": ("trading", "medium", "Handle 429/Retry-After, enableRateLimit"),
 "no-precision-handling": ("trading", "medium", "Use exchange precision/tick/step; Decimal quantize"),
 "lookahead-shift": ("trading", "high", "shift(-n) leaks future data into features"),
 "tts-shuffle-timeseries": ("trading", "medium", "Use shuffle=False / purged walk-forward split"),
 "scaler-fit-before-split": ("trading", "medium", "Fit scalers on train only (leakage)"),
 "backtest-no-costs": ("trading", "high", "Backtest must model fees + slippage + latency"),
 "float-money": ("trading", "medium", "Use Decimal/integer minor units for money"),
 "capability-missing": ("trading", "medium", "Expected trading-safety capability not found in code"),
 # reliability
 "dangling-task": ("reliability", "medium", "Keep reference + add_done_callback; unhandled task errors vanish"),
 "unbounded-queue": ("reliability", "low", "Set maxsize for backpressure"),
 "unclosed-session": ("reliability", "medium", "Use 'async with ClientSession()'"),
 "http-no-timeout": ("reliability", "medium", "Always pass timeout="),
 "subprocess-no-timeout": ("reliability", "low", "Pass timeout="),
 "blocking-in-async": ("reliability", "high", "Blocks event loop: use asyncio.sleep / async client / to_thread"),
 "cancelled-swallowed": ("reliability", "high", "Re-raise CancelledError or shutdown hangs"),
 # observability
 "logging-fstring": ("observability", "low", "Use logger.info('x %s', v) / structured fields"),
 "log-no-traceback": ("observability", "low", "Use logger.exception / exc_info=True"),
 "no-metrics": ("observability", "low", "Expose Prometheus metrics (latency, fills, rejects, PnL)"),
 "no-tracing": ("observability", "low", "Add trace/correlation ids across signal->order->fill"),
 # performance
 "n-plus-1-query": ("performance", "medium", "Batch queries outside loop"),
 "concat-in-loop": ("performance", "medium", "Collect list then concat once"),
 "regex-in-loop": ("performance", "low", "Compile once at module level"),
 "deepcopy-in-loop": ("performance", "low", "Avoid deepcopy in hot loops"),
 "pandas-iterrows": ("performance", "low", "Vectorize"),
 "pandas-apply-axis1": ("performance", "low", "Vectorize"),
 # data
 "many-storage-backends": ("data", "medium", "Pick a primary store; each backend adds failure modes"),
 "no-migrations": ("data", "low", "Add schema migrations (alembic/sql files)"),
 # ml
 "torch-load-unsafe": ("ml", "high", "torch.load(weights_only=True) or safetensors"),
 "joblib-load": ("ml", "medium", "joblib/pickle artifacts are code execution: verify hash/signature before load"),
 "numpy-pickle": ("ml", "high", "np.load allow_pickle=True is code execution"),
 "model-artifact-tracked": ("ml", "medium", "Store model binaries in registry/LFS, not git"),
 "heavy-import-live-path": ("ml", "medium", "Lazy-import torch/sklearn in live path (startup, memory, attack surface)"),
 "no-seed": ("ml", "low", "Set seeds / random_state for reproducibility"),
 # api
 "route-no-dependency": ("api", "medium", "Verify auth applied (Depends/middleware) on this route"),
 "cors-wildcard": ("api", "medium", "Restrict allow_origins"),
 "cors-credentials-wildcard": ("api", "high", "Wildcard origin + credentials is exploitable"),
 "error-detail-leak": ("api", "medium", "Return generic errors; log details server-side"),
 "no-rate-limit-lib": ("api", "medium", "Add rate limiting (slowapi/ingress)"),
 "no-health-endpoint": ("api", "low", "Add /health + /ready"),
 # devops
 "no-lockfile": ("devops", "high", "Add lock with hashes (uv lock / pip-compile --generate-hashes)"),
 "unpinned-deps": ("devops", "medium", "Pin exact versions in lock; ranges only in manifest"),
 "vcs-dependency": ("devops", "medium", "Pin git deps to commit SHA"),
 "docker-root": ("devops", "medium", "Add non-root USER"),
 "docker-unpinned-base": ("devops", "low", "Pin base image tag/digest"),
 "docker-curl-pipe": ("devops", "high", "Don't curl | sh; verify checksum"),
 "docker-secret-env": ("devops", "high", "Secrets in ENV/ARG are baked into layers; use runtime secrets"),
 "docker-no-healthcheck": ("devops", "low", "Add HEALTHCHECK"),
 "compose-privileged": ("devops", "high", "Remove privileged/host namespaces"),
 "compose-docker-sock": ("devops", "high", "docker.sock mount = root on host"),
 "action-not-sha-pinned": ("devops", "low", "Pin actions to commit SHA"),
 "action-mutable-ref": ("devops", "high", "@main/@master is mutable: pin SHA"),
 "workflow-pr-target": ("devops", "high", "pull_request_target runs untrusted code with secrets"),
 "workflow-no-permissions": ("devops", "medium", "Declare least-privilege permissions:"),
 "workflow-injection": ("devops", "high", "Don't interpolate github.event.* in run:; use env"),
 "workflow-secret-echo": ("devops", "high", "Never echo secrets"),
 "workflow-continue-on-error": ("devops", "low", "Hides failures"),
 "ci-no-tests": ("devops", "medium", "CI must run tests"),
 "no-ci": ("devops", "medium", "Add CI"),
 "no-dependabot": ("devops", "low", "Enable dependabot/renovate"),
 "no-precommit": ("devops", "low", "Add pre-commit (ruff, gitleaks)"),
 "large-tracked-file": ("devops", "medium", "Use LFS/artifact store"),
 "gitignore-missing": ("devops", "low", "Add to .gitignore"),
 "tracked-junk": ("devops", "low", "git rm --cached"),
 "env-undocumented": ("devops", "low", "Document in .env.example"),
 # docs
 "missing-doc-file": ("docs", "low", "Add file"),
 "low-docstring-coverage": ("docs", "low", "Document public APIs"),
 "no-runbook": ("docs", "low", "Add operations runbook (halt, recover, rotate keys)"),
 # frontend
 "js-xss-sink": ("frontend", "medium", "Avoid innerHTML/dangerouslySetInnerHTML/eval; sanitize"),
 "js-token-storage": ("frontend", "medium", "Don't keep tokens in localStorage; use httpOnly cookies"),
 "no-js-lockfile": ("frontend", "low", "Commit package-lock/pnpm-lock"),
 # governance
 "bus-factor": ("governance", "low", "Single author dominates history"),
}
DOMAINS = sorted({v[0] for v in R.values()})
ONLY = {x.strip() for x in args.only.split(",") if x.strip()}
SKIPD = {x.strip() for x in args.skip.split(",") if x.strip()}
F = []


def add(rule, f, line, msg, sev=None):
    dom, dsev, hint = R[rule]
    if (ONLY and dom not in ONLY) or dom in SKIPD:
        return
    F.append(dict(rule=rule, domain=dom, sev=sev or dsev, file=str(f), line=line, msg=msg, fix=hint))


def rel(p):
    p = pathlib.Path(p)
    return p.relative_to(ROOT) if p.is_absolute() else p


def red(s):
    s = str(s)
    return s[:4] + "***" if len(s) > 4 else "***"


def read(p, limit=1_500_000):
    try:
        if p.stat().st_size > limit:
            return ""
        t = p.read_text(errors="ignore")
        return "" if "\x00" in t[:2000] else t
    except Exception:
        return ""


def run(cmd, t=300):
    if shutil.which(cmd.split()[0]) is None:
        return None
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t, cwd=ROOT, stdin=subprocess.DEVNULL)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"(error {e})"


# ------------------------------------------------------------------ file discovery
tracked_out = run("git ls-files")
TRACKED = set(tracked_out.splitlines()) if tracked_out else set()
PYS, OTHER = [], []
for dp, dn, fn in os.walk(ROOT):
    dn[:] = [d for d in dn if d not in SKIP]
    for f in fn:
        p = pathlib.Path(dp) / f
        if f.endswith(".py"):
            PYS.append(p)
        elif (not TRACKED or str(rel(p)) in TRACKED) and not f.endswith((".png", ".jpg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".whl", ".pt", ".pth", ".pkl", ".joblib", ".onnx", ".h5", ".parquet", ".duckdb", ".db", ".sqlite", ".woff", ".woff2", ".lock", ".svg")):
            OTHER.append(p)


def is_test(p):
    r = rel(p)
    return "tests" in r.parts or "test" in r.parts or p.name.startswith("test_") or p.name == "conftest.py"


SRC = [p for p in PYS if not is_test(p)]
TESTS = [p for p in PYS if is_test(p)]
TEXT, TREES = {}, {}
for p in PYS:
    t = read(p, 3_000_000)
    TEXT[p] = t
    try:
        TREES[p] = ast.parse(t)
    except SyntaxError as e:
        add("syntax", rel(p), e.lineno or 0, "SyntaxError")
SRC_TEXT = "\n".join(TEXT[p] for p in SRC)
TEST_TEXT = "\n".join(TEXT[p] for p in TESTS)
if not TESTS:
    add("no-tests", ".", 0, "no test files found")

# ------------------------------------------------------------------ constants
MONEY = re.compile(r"(price|qty|quantity|amount|pnl|fee|balance|notional|cost|equity|margin)", re.I)
SECRET_NAME = re.compile(r"(api[_-]?key|secret|token|passw(or)?d|private[_-]?key|priv[_-]?key|mnemonic|seed[_-]?phrase)", re.I)
SECRETISH = re.compile(r"(^|_)(signature|sig|hmac|digest|token|secret|api_?key|password|passwd|mac|otp)($|_)", re.I)
PLACEHOLDER = re.compile(r"(changeme|change_me|example|xxx|your[_-]|test|dummy|placeholder|<|\$\{|\{\{|env|none|null|todo|fake|sample|redacted|\*\*\*)", re.I)
LIVE_DIRS = ("execution", "risk", "api", "security", "runtime", "workers", "engine")
QUAR_DIRS = ("mathcore", "research", "experimental", "ecc", "kg", "rag_mongo", "agent_control", "scratch", "notebooks")
ORDER_CALLS = {"create_order", "place_order", "submit_order", "cancel_order", "createOrder", "cancelOrder", "new_order"}
TX_SEND = {"send_raw_transaction", "sendRawTransaction", "send_transaction", "sendTransaction", "sendrawtransaction"}
SWAP_RE = re.compile(r"^(swapExact\w+|swapTokens\w*|exactInput\w*|exactOutput\w*)$")
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "request", "head"}
ROUTE_DECOS = {"get", "post", "put", "delete", "patch", "websocket", "api_route"}
CRYPTO_FN = re.compile(r"^(modinv|mod_inverse|inv_mod|scalar_mult(iply)?|point_(add|mul|double)|ecdsa_(sign|verify)|aes_(en|de)crypt|sha256|sha3|keccak\w*|hmac_\w+|rsa_(en|de)crypt|chacha\w*|poly1305\w*)$", re.I)
KEYFN = re.compile(r"(key|nonce|token|secret|salt|iv|password|otp|session)", re.I)
STAT = collections.Counter()
IMPORTS = collections.defaultdict(set)
CHAIN_LIBS = {"web3", "eth_account", "eth_abi", "solana", "solders", "bitcoin", "bitcoinlib", "bitcoinrpc", "bit", "ccxt", "brownie", "ape", "hexbytes", "bip_utils", "coincurve", "base58"}
STORE_LIBS = {"sqlite3", "aiosqlite", "asyncpg", "psycopg2", "psycopg", "duckdb", "pymongo", "motor", "redis", "sqlalchemy", "clickhouse_connect"}
HEAVY = {"torch", "tensorflow", "transformers", "sentence_transformers", "xgboost", "lightgbm", "sklearn", "hmmlearn", "optuna"}


def dotted(n):
    if isinstance(n, ast.Attribute):
        d = dotted(n.value)
        return f"{d}.{n.attr}" if d else n.attr
    if isinstance(n, ast.Name):
        return n.id
    return ""


def unparse(n):
    try:
        return ast.unparse(n)
    except Exception:
        return ""


def complexity(fn):
    c = 1
    for n in ast.walk(fn):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.comprehension)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
    return c


NEST = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)


def depth(n, d=0):
    m = d
    for ch in ast.iter_child_nodes(n):
        m = max(m, depth(ch, d + 1 if isinstance(ch, NEST) else d))
    return m


def names_in(n):
    return {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}


FUNC_BODIES = collections.defaultdict(list)
COMPLEX = []


class V(ast.NodeVisitor):
    def __init__(s, p, src):
        s.p, s.f, s.src = p, str(rel(p)), src
        s.fn, s.cls, s.loop = [], [], 0
        s.withc, s.route_params = set(), []
        s.live = any(d in rel(p).parts for d in LIVE_DIRS)
        s.quar = any(d in rel(p).parts for d in QUAR_DIRS)
        s.in_test_file = not src

    def a(s, rule, node, msg, tests=False, sev=None):
        if s.src or tests:
            add(rule, s.f, getattr(node, "lineno", 0), msg, sev)

    # ---- structure
    def redefs(s, body):
        seen = {}
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.decorator_list:
                if n.name in seen:
                    s.a("redefined-function", n, f"{n.name} redefined (first at line {seen[n.name]})", tests=True)
                seen[n.name] = n.lineno

    def visit_Module(s, n):
        s.redefs(n.body)
        s.generic_visit(n)

    def visit_ClassDef(s, n):
        s.redefs(n.body)
        s.cls.append(n)
        if s.src and not n.name.startswith("_"):
            STAT["pub_total"] += 1
            STAT["pub_doc"] += bool(ast.get_docstring(n))
        s.generic_visit(n)
        s.cls.pop()

    def visit_FunctionDef(s, n):
        for d in n.args.defaults + n.args.kw_defaults:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                s.a("mutable-default", n, f"{n.name}: mutable default argument")
        nparams = len(n.args.args) + len(n.args.kwonlyargs)
        if nparams > 9:
            s.a("too-many-params", n, f"{n.name}: {nparams} params")
        if s.src:
            STAT["fn_total"] += 1
            allargs = [a for a in n.args.args + n.args.kwonlyargs if a.arg not in ("self", "cls")]
            STAT["fn_typed"] += bool(n.returns) and all(a.annotation for a in allargs)
            if not n.name.startswith("_"):
                STAT["pub_total"] += 1
                STAT["pub_doc"] += bool(ast.get_docstring(n))
            L = (n.end_lineno or n.lineno) - n.lineno
            c = complexity(n)
            COMPLEX.append((c, s.f, n.lineno, n.name))
            if L > 120:
                s.a("long-function", n, f"{n.name}: {L} lines")
            if c > 40:
                s.a("high-complexity", n, f"{n.name}: complexity {c}", sev="high")
            elif c > 20:
                s.a("high-complexity", n, f"{n.name}: complexity {c}")
            if depth(n) > 6:
                s.a("deep-nesting", n, f"{n.name}: nesting depth {depth(n)}")
            if L >= 10:
                FUNC_BODIES[hash(ast.dump(ast.Module(body=n.body, type_ignores=[])))].append((s.f, n.lineno, n.name))
            if s.cls and n.args.args and n.args.args[0].arg == "self" and any(dotted(d.func if isinstance(d, ast.Call) else d) in ("lru_cache", "functools.lru_cache", "cache", "functools.cache") for d in n.decorator_list):
                s.a("lru-cache-method", n, f"{n.name}: lru_cache on method")
            if CRYPTO_FN.match(n.name) and not s.quar:
                s.a("custom-crypto-primitive", n, f"{n.name}(): hand-rolled crypto primitive outside quarantine dirs")
            s.check_route(n)
            s.check_order_tx(n)
        else:
            if n.name.startswith("test"):
                has_assert = any(isinstance(x, ast.Assert) or (isinstance(x, ast.Call) and re.search(r"(raises|warns|assert|expect|fail\b)", dotted(x.func), re.I)) or isinstance(x, ast.With) and any("raises" in unparse(i.context_expr) for i in x.items) for x in ast.walk(n))
                if not has_assert:
                    add("assertless-test", s.f, n.lineno, f"{n.name} has no assertion")
                STAT["tests"] += 1
                for d in n.decorator_list:
                    ds = unparse(d)
                    if re.search(r"mark\.(skip|skipif|xfail)", ds):
                        STAT["skipped"] += 1
                body = unparse(n)
                STAT["mocks"] += len(re.findall(r"\b(Mock|MagicMock|AsyncMock|patch|monkeypatch)\b", body))
                STAT["asserts"] += sum(isinstance(x, ast.Assert) for x in ast.walk(n))
        s.fn.append(n)
        sv = s.loop
        s.loop = 0
        s.generic_visit(n)
        s.loop = sv
        s.fn.pop()
        if s.route_params and s.route_params[-1][0] is n:
            s.route_params.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def check_route(s, n):
        for d in n.decorator_list:
            c = d.func if isinstance(d, ast.Call) else d
            if isinstance(c, ast.Attribute) and c.attr in ROUTE_DECOS:
                STAT["routes"] += 1
                path = d.args[0].value if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant) else "?"
                body = unparse(n)
                s.route_params.append((n, {a.arg for a in n.args.args + n.args.kwonlyargs}))
                kw = [k.arg or "" for k in (d.keywords if isinstance(d, ast.Call) else [])]
                if not ("Depends(" in body or "Security(" in body or any("dependencies" in k for k in kw)):
                    s.a("route-no-dependency", n, f"{c.attr.upper()} {path} ({n.name}): no Depends/Security - confirm auth middleware covers it")
                if re.search(r"query_params|Query\(", body) and re.search(r"(token|key|auth|secret)", body, re.I):
                    s.a("token-in-url", n, f"{n.name}: auth-like value read from query string")
                return

    def check_order_tx(s, n):
        calls = [c for c in ast.walk(n) if isinstance(c, ast.Call)]
        lasts = {dotted(c.func).split(".")[-1] for c in calls}
        body = None
        if lasts & ORDER_CALLS:
            body = unparse(n)
            names = sorted(lasts & ORDER_CALLS)
            if not re.search(r"client_?order_?id|clientOrderId|idempot|newClientOrderId|clientOid", body, re.I):
                s.a("order-no-idempotency", n, f"{n.name} calls {names} without client order id / idempotency key")
            if not any(isinstance(x, ast.Try) for x in ast.walk(n)):
                s.a("order-no-error-handling", n, f"{n.name} calls {names} with no try/except")
        if lasts & TX_SEND:
            body = body or unparse(n)
            if not re.search(r"receipt|confirmations|wait_for", body, re.I):
                s.a("no-confirmation-wait", n, f"{n.name} sends tx without receipt/confirmation handling")
        sw = [x for x in lasts if SWAP_RE.match(x)]
        if sw:
            body = body or unparse(n)
            if not (re.search(r"deadline", body, re.I) and re.search(r"amountOutMin|minAmountOut|amountOutMinimum|slippage|min_out", body, re.I)):
                s.a("swap-no-slippage-deadline", n, f"{n.name} swaps via {sw} without deadline+min-out/slippage")

    # ---- loops / flow
    def _loop(s, n):
        s.loop += 1
        if s.loop == 3:
            s.a("deep-loop-nesting", n, "3+ nested loops")
        s.generic_visit(n)
        s.loop -= 1

    visit_For = visit_AsyncFor = _loop

    def visit_While(s, n):
        if isinstance(n.test, ast.Constant) and n.test.value is True:
            has_try = any(isinstance(x, ast.Try) for x in ast.walk(n))
            pause = any(isinstance(x, ast.Call) and re.search(r"sleep|backoff|wait|delay|retry|recv|accept", dotted(x.func), re.I) for x in ast.walk(n)) or any(isinstance(x, ast.Await) for x in ast.walk(n))
            if has_try and not pause:
                s.a("retry-no-backoff", n, "while True + try/except without sleep/backoff/await")
        s._loop(n)

    def visit_With(s, n):
        for i in n.items:
            if isinstance(i.context_expr, ast.Call):
                s.withc.add(id(i.context_expr))
        s.generic_visit(n)

    visit_AsyncWith = visit_With

    def visit_Expr(s, n):
        if isinstance(n.value, ast.Call) and dotted(n.value.func).split(".")[-1] in ("create_task", "ensure_future"):
            s.a("dangling-task", n, "task created and reference dropped")
        s.generic_visit(n)

    def visit_Try(s, n):
        for h in n.handlers:
            tname = dotted(h.type) if h.type is not None and not isinstance(h.type, ast.Tuple) else ""
            trivial = all(isinstance(x, (ast.Pass, ast.Continue)) or (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)) for x in h.body)
            if h.type is None:
                s.a("bare-except", h, "bare except")
            if tname == "BaseException":
                s.a("baseexception", h, "except BaseException")
            if trivial and (h.type is None or tname in ("Exception", "BaseException")):
                s.a("swallowed-exception", h, "broad exception swallowed (pass/continue)")
            if tname.endswith("CancelledError") and not any(isinstance(x, ast.Raise) for x in ast.walk(h)):
                s.a("cancelled-swallowed", h, "CancelledError caught without re-raise")
            for x in ast.walk(h):
                if isinstance(x, ast.Call) and dotted(x.func).split(".")[-1] in ("error", "warning") and re.match(r"(self\.)?(_?log|_?logger|logging|LOG)", dotted(x.func)) and not any(k.arg == "exc_info" for k in x.keywords):
                    s.a("log-no-traceback", x, "error logged inside except without traceback (use logger.exception)")
                    break
        s.generic_visit(n)

    def visit_Assert(s, n):
        if s.src and s.live:
            s.a("assert-in-prod", n, "assert in live/runtime path")
        s.generic_visit(n)

    # ---- imports
    def visit_Import(s, n):
        for a in n.names:
            s.imp(a.name, n, [])
        s.generic_visit(n)

    def visit_ImportFrom(s, n):
        s.imp(n.module or "", n, [a.name for a in n.names])
        if any(a.name == "*" for a in n.names):
            s.a("wildcard-import", n, f"from {n.module} import *")
        s.generic_visit(n)

    def imp(s, mod, node, names):
        top = mod.split(".")[0]
        IMPORTS[top].add(s.f)
        if not s.src:
            return
        if top in HEAVY and not s.fn and s.live:
            s.a("heavy-import-live-path", node, f"module-scope import of {top} in live path")
        if mod.startswith("Crypto.") or top == "Crypto":
            s.a("deprecated-crypto-lib", node, "pycrypto (Crypto.*) is unmaintained")
        if top == "ecdsa":
            s.a("pure-python-ecdsa", node, "python-ecdsa (pure Python)")
        if any(x in ("DES", "ARC4", "RC4", "Blowfish", "TripleDES", "CAST5", "IDEA", "SEED") for x in names + mod.split(".")):
            s.a("weak-cipher", node, f"weak cipher import {mod} {names}")

    # ---- assignments
    def visit_Assign(s, n):
        s.assign(n.targets, n.value, n)
        s.generic_visit(n)

    def visit_AnnAssign(s, n):
        if n.value is not None:
            s.assign([n.target], n.value, n)
        s.generic_visit(n)

    def assign(s, targets, value, node):
        for t in targets:
            name = dotted(t) if not isinstance(t, (ast.Tuple, ast.List)) else ""
            if not name:
                continue
            last = name.split(".")[-1]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                v = value.value
                if SECRET_NAME.search(last) and len(v) >= 12 and not PLACEHOLDER.search(v) and " " not in v:
                    s.a("hardcoded-secret", node, f"{name} = '{red(v)}' (len {len(v)})", tests=True, sev=None if s.src else "low")
                if re.search(r"(mnemonic|seed_?phrase)", last, re.I) and len(v.split()) >= 12:
                    s.a("chain-key-material", node, f"{name}: looks like a mnemonic (redacted)", tests=True)
            if isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes)) and re.search(r"^(iv|nonce|salt)$|_(iv|nonce|salt)$", last, re.I) and len(value.value) >= 8:
                s.a("static-nonce-iv-salt", node, f"{name} is a constant")
            if last == "check_hostname" and isinstance(value, ast.Constant) and value.value is False:
                s.a("tls-weak-config", node, "check_hostname = False")
            if last == "verify_mode" and "CERT_NONE" in unparse(value):
                s.a("tls-weak-config", node, "verify_mode = CERT_NONE")
            if not s.fn and not s.cls and isinstance(t, ast.Name) and not t.id.isupper() and not t.id.startswith("__"):
                if isinstance(value, (ast.List, ast.Dict, ast.Set)) or (isinstance(value, ast.Call) and dotted(value.func) in ("list", "dict", "set", "defaultdict", "collections.defaultdict", "deque", "collections.deque")):
                    s.a("mutable-global", node, f"module-level mutable '{t.id}'")

    # ---- expressions
    def visit_Attribute(s, n):
        if n.attr in ("PROTOCOL_SSLv2", "PROTOCOL_SSLv3", "PROTOCOL_TLSv1", "PROTOCOL_TLSv1_1"):
            s.a("tls-weak-config", n, n.attr)
        if n.attr == "CERT_NONE" and dotted(n).startswith("ssl"):
            s.a("tls-weak-config", n, "ssl.CERT_NONE")
        if n.attr == "MODE_ECB" or (n.attr == "ECB" and "modes" in dotted(n)):
            s.a("ecb-mode", n, "ECB mode")
        s.generic_visit(n)

    def visit_Compare(s, n):
        parts = [n.left] + n.comparators
        if any(isinstance(o, (ast.Eq, ast.NotEq)) for o in n.ops):
            if any(isinstance(x, ast.Constant) and isinstance(x.value, float) for x in parts):
                s.a("float-equality", n, "float == comparison")
            cand = [x for x in parts if isinstance(x, (ast.Name, ast.Attribute, ast.Subscript)) and SECRETISH.search(dotted(x) if not isinstance(x, ast.Subscript) else dotted(x.value))]
            if cand and not any(isinstance(x, ast.Constant) for x in parts):
                s.a("non-constant-time-compare", n, f"{unparse(n)[:60]}")
        if any(isinstance(o, (ast.Is, ast.IsNot)) for o in n.ops) and any(isinstance(x, ast.Constant) and isinstance(x.value, (str, int, bytes)) and not isinstance(x.value, bool) for x in n.comparators):
            s.a("is-literal", n, "'is' with literal")
        s.generic_visit(n)

    def visit_Dict(s, n):
        keys = {k.value for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "to" in keys and ("value" in keys or "data" in keys) and keys & {"nonce", "gas", "gasPrice", "maxFeePerGas"}:
            if "chainId" not in keys and "chain_id" not in keys:
                s.a("tx-no-chainid", n, "transaction dict without chainId")
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in ("gas", "gasPrice") and isinstance(v, ast.Constant) and isinstance(v.value, int):
                    s.a("hardcoded-gas", n, f"hardcoded {k.value}")
        s.generic_visit(n)

    def visit_BinOp(s, n):
        if isinstance(n.op, (ast.Mult, ast.Div)) and isinstance(n.right, ast.Constant) and isinstance(n.right.value, float) and n.right.value in (1e18, 1e9, 1e8, 1e6):
            s.a("float-chain-units", n, f"float unit conversion by {n.right.value:g}")
        if isinstance(n.op, ast.Div) and isinstance(n.right, ast.BinOp) and isinstance(n.right.op, ast.Pow) and isinstance(n.right.left, ast.Constant) and n.right.left.value == 10 and isinstance(n.right.right, ast.Constant) and n.right.right.value in (6, 8, 9, 18):
            s.a("float-chain-units", n, f"true division by 10**{n.right.right.value}")
        s.generic_visit(n)

    def visit_Call(s, n):
        d = dotted(n.func)
        last = d.split(".")[-1]
        kws = {k.arg: k.value for k in n.keywords if k.arg}
        fn = s.fn[-1] if s.fn else None
        in_async = isinstance(fn, ast.AsyncFunctionDef)
        top = d.split(".")[0]
        a0 = n.args[0] if n.args else None
        # security
        if d in ("eval", "exec"):
            s.a("eval-exec", n, f"{d}()")
        if d in ("pickle.load", "pickle.loads", "cPickle.loads", "marshal.loads", "shelve.open", "jsonpickle.decode", "pandas.read_pickle", "pd.read_pickle", "dill.load", "dill.loads"):
            s.a("unsafe-deserialization", n, d)
        if d == "yaml.load" and "Loader" not in kws and len(n.args) < 2:
            s.a("unsafe-deserialization", n, "yaml.load without Loader")
        if d.startswith("subprocess.") or d in ("os.system", "os.popen"):
            sh = kws.get("shell")
            if d in ("os.system", "os.popen") or (isinstance(sh, ast.Constant) and sh.value is True):
                s.a("shell-exec", n, f"{d} with shell")
            if last in ("run", "check_output", "call", "check_call") and "timeout" not in kws:
                s.a("subprocess-no-timeout", n, d)
        if isinstance(kws.get("verify"), ast.Constant) and kws["verify"].value is False:
            s.a("tls-verify-off", n, f"{d}(verify=False)")
        if d == "ssl._create_unverified_context":
            s.a("tls-weak-config", n, d)
        if last in ("execute", "executemany", "executescript") and a0 is not None:
            if isinstance(a0, ast.JoinedStr) or (isinstance(a0, ast.BinOp) and isinstance(a0.op, (ast.Add, ast.Mod))) or (isinstance(a0, ast.Call) and dotted(a0.func).endswith(".format")):
                s.a("sql-injection-risk", n, "dynamic SQL string in execute()")
            if s.loop:
                s.a("n-plus-1-query", n, "query inside loop")
        if d in ("pty.fork", "pty.spawn", "os.openpty", "os.forkpty"):
            s.a("pty-shell-surface", n, d)
        if d in ("tempfile.mktemp",):
            s.a("insecure-tempfile", n, d)
        if d == "os.chmod" and len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and n.args[1].value in (0o777, 0o666):
            s.a("chmod-777", n, "chmod 777/666")
        if d.startswith(("xml.etree", "xml.dom", "xml.sax", "lxml.etree")) and last in ("parse", "fromstring", "parseString"):
            s.a("xxe-risk", n, d)
        if last in ("run", "run_app") and isinstance(kws.get("host"), ast.Constant) and kws["host"].value == "0.0.0.0":
            s.a("bind-all-interfaces", n, "host=0.0.0.0")
        if isinstance(kws.get("debug"), ast.Constant) and kws["debug"].value is True:
            s.a("debug-enabled", n, f"{d}(debug=True)")
        if d.endswith("jwt.decode") or d == "jwt.decode":
            opts = unparse(kws.get("options")) if kws.get("options") else ""
            if (isinstance(kws.get("verify"), ast.Constant) and kws["verify"].value is False) or re.search(r"verify_signature.*False", opts):
                s.a("jwt-misuse", n, "jwt.decode with signature verification disabled", sev="critical")
            elif "algorithms" not in kws:
                s.a("jwt-misuse", n, "jwt.decode without algorithms=")
        if d.endswith("jwt.encode") and isinstance(kws.get("algorithm"), ast.Constant) and str(kws["algorithm"].value).lower() == "none":
            s.a("jwt-misuse", n, "alg=none", sev="critical")
        if last == "add_middleware" and a0 is not None and "CORSMiddleware" in unparse(a0):
            if "*" in unparse(kws.get("allow_origins")) if kws.get("allow_origins") else False:
                cred = isinstance(kws.get("allow_credentials"), ast.Constant) and kws["allow_credentials"].value is True
                s.a("cors-credentials-wildcard" if cred else "cors-wildcard", n, "allow_origins=['*']")
        if last == "HTTPException" and kws.get("detail") is not None:
            ds = unparse(kws["detail"])
            if re.search(r"\bstr\((e|exc|err|ex|error)\b|\{(e|exc|err|ex|error)\}|format_exc", ds):
                s.a("error-detail-leak", n, "exception text returned to client")
        # route-aware heuristics
        if s.route_params and a0 is not None:
            params = s.route_params[-1][1]
            if (top in ("requests", "httpx") and last in HTTP_VERBS or last in ("urlopen",)) and names_in(a0) & params:
                s.a("ssrf-candidate", n, f"{d}() with request-controlled URL")
            if d in ("open", "Path", "pathlib.Path", "os.path.join") and names_in(a0) & params:
                s.a("path-traversal-candidate", n, f"{d}() with request-controlled path")
        # crypto
        if d in ("hashlib.md5", "hashlib.sha1") or (d == "hashlib.new" and a0 is not None and unparse(a0).strip("'\"").lower() in ("md5", "sha1")):
            s.a("weak-hash", n, d)
        if top == "random" and fn is not None and KEYFN.search(fn.name):
            s.a("weak-random", n, f"{d} inside {fn.name}()")
        if last == "pbkdf2_hmac" and len(n.args) > 3 and isinstance(n.args[3], ast.Constant) and isinstance(n.args[3].value, int) and n.args[3].value < 100000:
            s.a("low-kdf-iterations", n, f"pbkdf2 iterations={n.args[3].value}")
        if last == "PBKDF2HMAC" and isinstance(kws.get("iterations"), ast.Constant) and kws["iterations"].value < 100000:
            s.a("low-kdf-iterations", n, f"iterations={kws['iterations'].value}")
        if last == "generate_private_key" and isinstance(kws.get("key_size"), ast.Constant) and kws["key_size"].value < 2048:
            s.a("small-rsa-key", n, f"RSA {kws['key_size'].value}")
        if d == "RSA.generate" and a0 is not None and isinstance(a0, ast.Constant) and a0.value < 2048:
            s.a("small-rsa-key", n, f"RSA {a0.value}")
        # ml
        if d == "torch.load" and not (isinstance(kws.get("weights_only"), ast.Constant) and kws["weights_only"].value is True):
            s.a("torch-load-unsafe", n, "torch.load without weights_only=True")
        if d in ("joblib.load",):
            s.a("joblib-load", n, d)
        if d in ("np.load", "numpy.load") and isinstance(kws.get("allow_pickle"), ast.Constant) and kws["allow_pickle"].value is True:
            s.a("numpy-pickle", n, "allow_pickle=True")
        # trading / data science
        if d == "float" and a0 is not None and MONEY.search(unparse(a0)):
            s.a("float-money", n, f"float({unparse(a0)[:40]})")
        if last == "shift" and a0 is not None and ((isinstance(a0, ast.UnaryOp) and isinstance(a0.op, ast.USub)) or (isinstance(a0, ast.Constant) and isinstance(a0.value, int) and a0.value < 0)) :
            s.a("lookahead-shift", n, f"{unparse(n)[:50]}", sev="high" if re.search(r"(feature|model|backtest|signal|engine|tuning|train|label)", s.f) else "medium")
        if last == "train_test_split" and not (isinstance(kws.get("shuffle"), ast.Constant) and kws["shuffle"].value is False):
            s.a("tts-shuffle-timeseries", n, "train_test_split shuffles by default")
        # reliability
        if top in ("requests", "httpx") and last in HTTP_VERBS and "timeout" not in kws:
            s.a("http-no-timeout", n, f"{d}() without timeout")
        if in_async and (d == "time.sleep" or (top == "requests" and last in HTTP_VERBS) or d in ("subprocess.run", "subprocess.check_output")):
            s.a("blocking-in-async", n, f"{d}() inside async def")
        if last == "ClientSession" and id(n) not in s.withc:
            s.a("unclosed-session", n, "ClientSession outside 'async with'")
        if d == "open" and id(n) not in s.withc and fn is not None:
            s.a("open-no-with", n, "open() without with")
        if d in ("asyncio.Queue", "queue.Queue", "Queue", "asyncio.PriorityQueue") and not n.args and "maxsize" not in kws:
            s.a("unbounded-queue", n, d)
        if d == "asyncio.get_event_loop":
            s.a("get-event-loop", n, d)
        if last in ("HTTPProvider", "AsyncHTTPProvider", "Web3") and "timeout" not in unparse(n):
            s.a("provider-no-timeout", n, f"{d}() without timeout")
        # observability
        if last in ("debug", "info", "warning", "error", "exception", "critical") and re.match(r"(self\.)?(_?log|_?logger|logging|LOG)\b", d):
            if a0 is not None and isinstance(a0, ast.JoinedStr):
                s.a("logging-fstring", n, "f-string in logging call")
            for arg in n.args[1:] + [k.value for k in n.keywords]:
                for x in ast.walk(arg):
                    nm = x.id if isinstance(x, ast.Name) else x.attr if isinstance(x, ast.Attribute) else ""
                    if nm and SECRET_NAME.search(nm):
                        s.a("log-secret", n, f"logging value named '{nm}'")
                        break
            if a0 is not None and isinstance(a0, ast.JoinedStr):
                for x in ast.walk(a0):
                    nm = x.id if isinstance(x, ast.Name) else x.attr if isinstance(x, ast.Attribute) else ""
                    if nm and SECRET_NAME.search(nm):
                        s.a("log-secret", n, f"f-string logs '{nm}'")
                        break
        if d == "print":
            s.a("print-in-src", n, "print()")
        if d in ("datetime.utcnow", "datetime.datetime.utcnow") or (d in ("datetime.now", "datetime.datetime.now") and not n.args and "tz" not in kws):
            s.a("naive-datetime", n, d)
        # performance
        if s.loop:
            if d in ("re.compile",):
                s.a("regex-in-loop", n, d)
            if d in ("copy.deepcopy", "deepcopy"):
                s.a("deepcopy-in-loop", n, d)
            if d in ("pd.concat", "pandas.concat"):
                s.a("concat-in-loop", n, d)
        if last == "iterrows":
            s.a("pandas-iterrows", n, "iterrows()")
        if last == "apply" and isinstance(kws.get("axis"), ast.Constant) and kws["axis"].value in (1, "columns"):
            s.a("pandas-apply-axis1", n, "apply(axis=1)")
        s.generic_visit(n)


for p in PYS:
    if p in TREES:
        V(p, p in SRC).visit(TREES[p])

# duplicate code
for k, v in FUNC_BODIES.items():
    if len({x[0] + str(x[1]) for x in v}) > 1:
        add("duplicate-code", v[0][0], v[0][1], "identical body: " + ", ".join(f"{a}:{b} {c}" for a, b, c in v[:4]))

# ------------------------------------------------------------------ import graph
MODS = {}
for p in SRC:
    parts = list(rel(p).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts:
        MODS[".".join(parts)] = p
ALT = {}
for m in MODS:
    sp = m.split(".")
    if len(sp) > 1 and sp[0] == args.src:
        ALT[".".join(sp[1:])] = m
G = collections.defaultdict(set)


def resolve(name):
    parts = name.split(".")
    while parts:
        c = ".".join(parts)
        if c in MODS:
            return c
        if c in ALT:
            return ALT[c]
        parts.pop()
    return None


for p in SRC:
    pm = list(rel(p).with_suffix("").parts)
    me = ".".join(pm[:-1] if pm[-1] == "__init__" else pm)
    pkg = pm if pm[-1] == "__init__" and False else (pm[:-1])
    for n in ast.walk(TREES.get(p, ast.Module(body=[], type_ignores=[]))):
        tg = []
        if isinstance(n, ast.Import):
            tg = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg[:len(pkg) - (n.level - 1)] if n.level > 1 else pkg
                mod = base + (n.module.split(".") if n.module else [])
                bname = ".".join(mod)
            else:
                bname = n.module or ""
            tg = [bname] + [f"{bname}.{a.name}" for a in n.names]
        for t in tg:
            r = resolve(t)
            if r and r != me:
                G[me].add(r)
sys.setrecursionlimit(20000)
idx, low, onst, st, sccs, ctr = {}, {}, set(), [], [], [0]


def strong(v):
    idx[v] = low[v] = ctr[0]; ctr[0] += 1; st.append(v); onst.add(v)
    for w in G.get(v, ()):
        if w not in idx:
            strong(w); low[v] = min(low[v], low[w])
        elif w in onst:
            low[v] = min(low[v], idx[w])
    if low[v] == idx[v]:
        comp = []
        while True:
            w = st.pop(); onst.discard(w); comp.append(w)
            if w == v:
                break
        if len(comp) > 1:
            sccs.append(comp)


for v in list(MODS):
    if v not in idx:
        strong(v)
for comp in sccs:
    add("import-cycle", comp[0], 0, f"cycle of {len(comp)}: " + " <-> ".join(sorted(comp)[:6]))
for a, bs in G.items():
    if len(bs) > 25:
        add("high-coupling", a, 0, f"imports {len(bs)} internal modules")
    for b in bs:
        ap_, bp_ = a.split("."), b.split(".")
        if any(d in ap_ for d in LIVE_DIRS) and any(d in bp_ for d in QUAR_DIRS) and not any(d in ap_ for d in QUAR_DIRS):
            add("layer-violation", a, 0, f"{a} (live) imports {b} (research/quarantine)")
imported = set().union(*G.values()) if G else set()
for m, p in MODS.items():
    if p.name == "__init__.py":
        continue
    leaf = p.stem
    referenced = re.search(rf"\b{re.escape(leaf)}\b", TEST_TEXT) is not None
    moneyp = re.search(r"(execution|risk|security|order|wallet|signer|vault|ledger)", str(rel(p)))
    if moneyp and not referenced:
        add("money-path-untested", rel(p), 0, "no test references this module")
    elif m not in imported and "__main__" not in TEXT.get(p, "") and not referenced:
        add("orphan-untested", rel(p), 0, "never imported by src, never referenced in tests")

# unused definitions (cheap word-count heuristic)
WORDS = collections.Counter()
for p in PYS:
    WORDS.update(re.findall(r"[A-Za-z_]\w{3,}", TEXT[p]))
for p in OTHER[:3000]:
    if p.suffix in (".md", ".yml", ".yaml", ".toml", ".json", ".cfg", ".ini", ".txt", ".sh"):
        WORDS.update(re.findall(r"[A-Za-z_]\w{3,}", read(p, 300000)))
for p in SRC:
    for n in TREES.get(p, ast.Module(body=[], type_ignores=[])).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.decorator_list and not n.name.startswith(("test", "_", "main")) and len(n.name) > 3 and WORDS[n.name] == 1:
            add("unused-definition", rel(p), n.lineno, f"{n.name} referenced nowhere else")

# god files, duplicate names
for p in SRC:
    n = TEXT[p].count("\n")
    if n > 1500:
        add("god-file", rel(p), 0, f"{n} lines")
names = collections.defaultdict(list)
for p in SRC:
    if p.name != "__init__.py":
        names[p.name].append(str(rel(p)))
for k, v in names.items():
    if len(v) > 1:
        add("duplicate-module-name", v[0], 0, f"{k}: {', '.join(v)}")

# ------------------------------------------------------------------ text-file scans (secrets, blockchain, devops, frontend)
SECRET_PATS = [
    ("private-key-in-repo", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"), "private key block"),
    ("secret-in-file", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    ("secret-in-file", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "GitHub token"),
    ("secret-in-file", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    ("secret-in-file", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{24,}"), "API secret key (sk-...)"),
    ("secret-in-file", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}"), "Stripe live key"),
    ("secret-in-file", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
    ("chain-key-material", re.compile(r"\bxprv[1-9A-HJ-NP-Za-km-z]{100,}"), "BIP32 xprv"),
    ("chain-key-material", re.compile(r"\b[5KL][1-9A-HJ-NP-Za-km-z]{50,51}\b"), "possible WIF private key"),
    ("chain-key-material", re.compile(r"(?i)(priv(ate)?[_-]?key|secret[_-]?key)\W{0,6}(0x)?[0-9a-f]{64}\b"), "hex private key"),
    ("rpc-url-credentials", re.compile(r"https?://[^/\s:@'\"]+:[^/\s@'\"]+@[\w.\-]+"), "credentials in URL"),
    ("rpc-url-credentials", re.compile(r"(?i)(infura\.io/v3|alchemy\.com/v2|quiknode\.pro|ankr\.com/\w+|getblock\.io|nodereal\.io/v1)/[A-Za-z0-9_\-]{16,}"), "provider API key in RPC URL"),
    ("unlimited-approval", re.compile(r"(?i)2\s*\*\*\s*256\s*-\s*1|MaxUint256|0x[f]{64}\b|uint256\(-1\)"), "unlimited approval constant"),
]
GENERIC = re.compile(r"""(?i)\b(api[_-]?key|secret|token|passw(or)?d|private[_-]?key)\b["']?\s*[:=]\s*["']([A-Za-z0-9/+_\-=.]{16,})["']""")
ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
for p in list(OTHER) + PYS:
    r = rel(p)
    t = TEXT.get(p) or read(p)
    if not t:
        continue
    lowprio = is_test(p) or p.suffix == ".md" or "docs" in r.parts or "example" in p.name.lower() or "fixtures" in r.parts
    for i, line in enumerate(t.splitlines(), 1):
        if len(line) > 2000:
            continue
        for rule, rx, label in SECRET_PATS:
            m = rx.search(line)
            if m and not PLACEHOLDER.search(line[max(0, m.start() - 20):m.end() + 20]):
                add(rule, r, i, f"{label}: {red(m.group(0))}", sev="low" if lowprio else None)
        if p.suffix != ".py":
            m = GENERIC.search(line)
            if m and not PLACEHOLDER.search(m.group(3)):
                add("secret-in-file", r, i, f"{m.group(1)} = {red(m.group(3))}", sev="low" if lowprio else None)
    if p.suffix == ".py" and p in SRC:
        addrs = ADDR.findall(t)
        if len(addrs) > 3:
            add("hardcoded-addresses", r, 0, f"{len(addrs)} hardcoded 0x addresses")

# ------------------------------------------------------------------ blockchain file-level
STAT["web3"] = 0
for p in SRC:
    t, r = TEXT[p], rel(p)
    if re.search(r"get_?block\b|getblock|eth_getBlock|get_logs|getLogs|getblockhash", t, re.I):
        if not re.search(r"reorg|finaliz|confirmations|\"safe\"|'safe'", t, re.I):
            add("no-reorg-handling", r, 0, "reads blocks/logs without reorg/finality/confirmation handling")
        if not re.search(r"last_?(processed_?)?block|checkpoint|cursor|from_block|resume|height", t, re.I):
            add("no-block-checkpoint", r, 0, "block ingestion without visible checkpoint/resume")
    if re.search(r"latestRoundData|price_?feed|oracle", t, re.I) and not re.search(r"updatedAt|stale|staleness|max_age|heartbeat", t, re.I):
        add("oracle-no-staleness", r, 0, "oracle/price-feed use without staleness check")
    if re.search(r"order|exchange|ccxt|create_order", t, re.I) and re.search(r"\bcreate_order|place_order|ccxt", t):
        if not re.search(r"\b429\b|RateLimit|rate_?limit|Retry-After|enableRateLimit", t):
            add("no-rate-limit-handling", r, 0, "exchange calls without 429/rate-limit handling")
        if not re.search(r"precision|quantize|step_?size|tick_?size|lot_?size|round\(", t, re.I):
            add("no-precision-handling", r, 0, "orders without precision/tick/step handling")
    if re.search(r"backtest", str(r), re.I) and not re.search(r"fee|slippage|commission|spread", t, re.I):
        add("backtest-no-costs", r, 0, "backtest module with no fee/slippage/commission terms")
    if re.search(r"fit_transform|\.fit\(", t) and re.search(r"train_test_split|TimeSeriesSplit", t):
        if t.find("fit_transform") != -1 and 0 <= t.find("fit_transform") < t.find("train_test_split") if "train_test_split" in t else False:
            add("scaler-fit-before-split", r, 0, "fit_transform appears before train_test_split")
    if re.search(r"torch|sklearn|xgboost|numpy\.random|np\.random", t) and not re.search(r"seed|random_state", t):
        add("no-seed", r, 0, "ML/random usage with no seed/random_state")
    if re.search(r"web3|Web3", t) and "web3" in t and not re.search(r"to_checksum_address|toChecksumAddress|is_checksum_address", t):
        add("no-checksum-validation", r, 0, "web3 usage without checksum address validation")
if re.search(r"(?i)post[- ]?quantum|\bpqc\b|kyber|dilithium|ml-kem|ml-dsa", SRC_TEXT) and not re.search(r"\b(import|from)\s+(oqs|pqcrypto|liboqs|pqclean|kyber_py|dilithium_py)", SRC_TEXT):
    add("pq-claim-without-impl", "src", 0, "post-quantum referenced but no PQ library (oqs/pqcrypto/kyber_py) is imported")

# ------------------------------------------------------------------ capabilities (trading safety map)
CAP = {
    "kill_switch": (r"kill_?switch|\bhalt", "high"),
    "idempotency": (r"idempoten|client_?order_?id", "high"),
    "position_limits": (r"max_?position|position_?limit|max_?notional|exposure", "high"),
    "drawdown_stop": (r"drawdown|stop_?loss|circuit_?breaker", "medium"),
    "rate_limit_backoff": (r"backoff|tenacity|jitter|rate_?limit", "medium"),
    "reconciliation": (r"reconcil", "medium"),
    "audit_trail": (r"audit", "medium"),
    "graceful_shutdown": (r"SIGTERM|add_signal_handler|graceful", "medium"),
    "paper_mode": (r"paper", "info"),
    "secret_vault": (r"vault|keyring|kms|secretsmanager|credential", "medium"),
    "metrics": (r"prometheus", "info"),
    "structured_logging": (r"structlog|json_?log", "info"),
    "tracing": (r"opentelemetry|trace_?id|correlation_?id", "info"),
    "health_endpoint": (r"/health|healthz|readyz", "info"),
    "decimal_money": (r"\bDecimal\b", "info"),
}
trading = bool(re.search(r"create_order|ccxt|place_order|exchange", SRC_TEXT))
CAPS = {}
for k, (rx, sv) in CAP.items():
    CAPS[k] = bool(re.search(rx, SRC_TEXT, re.I))
    if trading and not CAPS[k] and sv in ("high", "medium"):
        add("capability-missing", "src", 0, f"no '{k}' capability found", sev=sv)
if not CAPS["metrics"]:
    add("no-metrics", "src", 0, "no prometheus metrics")
if not CAPS["tracing"]:
    add("no-tracing", "src", 0, "no trace/correlation id")
if not CAPS["health_endpoint"] and STAT["routes"]:
    add("no-health-endpoint", "src", 0, "API without /health")
if STAT["routes"] and not re.search(r"slowapi|limiter|ratelimit|rate_limit", SRC_TEXT, re.I):
    add("no-rate-limit-lib", "src", 0, "API without rate limiting")
stores = sorted(x for x in IMPORTS if x in STORE_LIBS and any(f for f in IMPORTS[x] if not is_test(pathlib.Path(f))))
if len(stores) >= 3:
    add("many-storage-backends", "src", 0, f"{len(stores)} storage libs: {', '.join(stores)}")
if stores and not any(re.search(r"migrat|alembic", str(rel(p)), re.I) for p in OTHER + PYS):
    add("no-migrations", "src", 0, "storage in use, no migrations found")

# ------------------------------------------------------------------ quality repo-level
chase = [p for p in TESTS if re.search(r"(coverage|boost|residual|gaps?|lowest_covered|small_module|mid_batch)", p.name)]
if chase:
    add("coverage-chasing-tests", rel(chase[0]), 0, f"{len(chase)} coverage-padding test files: " + ", ".join(x.name for x in chase[:8]))
if STAT["skipped"]:
    add("skipped-tests", "tests", 0, f"{STAT['skipped']} skip/xfail markers")
for p in TESTS:
    if re.search(r"time\.sleep\(", TEXT[p]):
        add("sleep-in-test", rel(p), 0, "time.sleep in test")
if STAT["asserts"] and STAT["mocks"] / max(1, STAT["asserts"]) > 1.5:
    add("mock-heavy-test", "tests", 0, f"mock refs {STAT['mocks']} vs asserts {STAT['asserts']}")
CFG = "\n".join(read(ROOT / n) for n in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", ".coveragerc", "noxfile.py", "Makefile"))
for pat, label in ((r"fail[_-]under", "cov-fail-under"), (r"strict-markers", "--strict-markers"), (r"timeout", "pytest-timeout")):
    if TESTS and not re.search(pat, CFG):
        add("test-config-weak", "pytest config", 0, f"missing {label}")
if TESTS and not re.search(r"hypothesis|atheris|fuzz", TEST_TEXT + CFG):
    add("no-property-or-fuzz-tests", "tests", 0, "no hypothesis/fuzz usage")
if TESTS and not re.search(r"mutmut|cosmic.ray|mutation", CFG + "\n".join(read(p) for p in OTHER if ".github" in str(p))):
    add("no-mutation-testing", "tests", 0, "no mutation testing configured")

# ------------------------------------------------------------------ devops / docs / governance
reqs = [p for p in ROOT.glob("requirements*.txt")]
lock = any((ROOT / n).exists() for n in ("poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock", "requirements.lock")) or any("--hash=" in read(r) for r in reqs)
unpinned = 0
for r in reqs:
    for line in read(r).splitlines():
        s_ = line.split("#")[0].strip()
        if not s_:
            continue
        if re.match(r"--?(extra-)?index-url|--extra-index-url", s_):
            if "extra-index-url" in s_:
                add("dependency-confusion", rel(r), 0, "--extra-index-url present")
        elif s_.startswith("--trusted-host"):
            add("dependency-confusion", rel(r), 0, "--trusted-host present")
        elif s_.startswith(("git+", "-e git+")) and not re.search(r"@[0-9a-f]{40}", s_):
            add("vcs-dependency", rel(r), 0, "VCS dependency not pinned to SHA: " + s_[:60])
        elif not s_.startswith("-") and "==" not in s_:
            unpinned += 1
if reqs and not lock:
    add("no-lockfile", "requirements*.txt", 0, f"{unpinned} non-exact requirement lines and no lock/hash file")
elif unpinned and not lock:
    add("unpinned-deps", "requirements*.txt", unpinned, f"{unpinned} unpinned")
for df in list(ROOT.glob("**/Dockerfile*"))[:10]:
    if set(rel(df).parts) & SKIP:
        continue
    t = read(df)
    r = rel(df)
    if not re.search(r"^\s*USER\s+(?!root)", t, re.M):
        add("docker-root", r, 0, "no non-root USER")
    for m in re.finditer(r"^FROM\s+(\S+)", t, re.M):
        img = m.group(1)
        if "@sha256" not in img and (":" not in img or img.endswith(":latest")) and img.lower() != "scratch":
            add("docker-unpinned-base", r, 0, f"{img}")
    if re.search(r"(curl|wget)[^\n|]*\|\s*(ba)?sh", t):
        add("docker-curl-pipe", r, 0, "curl|sh")
    for m in re.finditer(r"^(ENV|ARG)\s+(\w+)[= ]\s*(\S+)", t, re.M):
        if SECRET_NAME.search(m.group(2)) and len(m.group(3)) > 8 and not PLACEHOLDER.search(m.group(3)) and not m.group(3).startswith("$"):
            add("docker-secret-env", r, 0, f"{m.group(1)} {m.group(2)} has literal value")
    if "HEALTHCHECK" not in t:
        add("docker-no-healthcheck", r, 0, "no HEALTHCHECK")
for cf_ in list(ROOT.glob("**/docker-compose*.y*ml"))[:10] + list(ROOT.glob("**/compose*.y*ml"))[:5]:
    if set(rel(cf_).parts) & SKIP:
        continue
    t = read(cf_)
    r = rel(cf_)
    if re.search(r"privileged:\s*true|network_mode:\s*[\"']?host|pid:\s*[\"']?host", t):
        add("compose-privileged", r, 0, "privileged/host namespace")
    if "docker.sock" in t:
        add("compose-docker-sock", r, 0, "docker.sock mounted")
wf = [p for p in (ROOT / ".github" / "workflows").glob("*.y*ml")] if (ROOT / ".github" / "workflows").exists() else []
if not wf:
    add("no-ci", ".github/workflows", 0, "no CI workflows")
else:
    allwf = "\n".join(read(p) for p in wf)
    if not re.search(r"pytest|tox|nox|unittest", allwf):
        add("ci-no-tests", ".github/workflows", 0, "no test command in workflows")
    for p in wf:
        t, r = read(p), rel(p)
        if "pull_request_target" in t:
            add("workflow-pr-target", r, 0, "pull_request_target")
        if not re.search(r"^permissions:", t, re.M):
            add("workflow-no-permissions", r, 0, "no top-level permissions")
        if re.search(r"continue-on-error:\s*true", t):
            add("workflow-continue-on-error", r, 0, "continue-on-error: true")
        for i, line in enumerate(t.splitlines(), 1):
            m = re.search(r"uses:\s*([^@\s]+)@(\S+)", line)
            if m and not m.group(1).startswith("./") and not re.fullmatch(r"[0-9a-f]{40}", m.group(2)):
                add("action-mutable-ref" if m.group(2) in ("main", "master") else "action-not-sha-pinned", r, i, f"{m.group(1)}@{m.group(2)}")
            if re.search(r"run:.*\$\{\{\s*github\.event\.(issue|pull_request|comment|head_commit|review|pages)", line):
                add("workflow-injection", r, i, "github.event.* interpolated in run:")
            if re.search(r"echo\s+.*\$\{\{\s*secrets\.", line):
                add("workflow-secret-echo", r, i, "secret echoed")
            if re.search(r"(curl|wget)[^\n|]*\|\s*(ba)?sh", line):
                add("docker-curl-pipe", r, i, "curl|sh in workflow")
if not any((ROOT / n).exists() for n in (".github/dependabot.yml", ".github/dependabot.yaml", "renovate.json", ".github/renovate.json")):
    add("no-dependabot", ".github", 0, "no dependabot/renovate config")
if not (ROOT / ".pre-commit-config.yaml").exists():
    add("no-precommit", ".", 0, "no .pre-commit-config.yaml")
gi = read(ROOT / ".gitignore")
for pat in (".env", "*.pem", "*.egg-info", "__pycache__", "*.db", "*.sqlite", "*.key"):
    if pat not in gi:
        add("gitignore-missing", ".gitignore", 0, f"'{pat}' not ignored")
for bad in TRACKED:
    if re.search(r"(^|/)\.env(\.[\w]+)?$", bad) and not re.search(r"(example|sample|template)$", bad):
        add("tracked-secret-file", bad, 0, "env file tracked in git")
    elif re.search(r"(\.pem|id_rsa|id_ed25519|\.p12|\.pfx|\.keystore)$", bad):
        add("tracked-secret-file", bad, 0, "key material tracked in git")
    elif re.search(r"(\.egg-info/|__pycache__/|\.pyc$|\.sqlite3?$|\.db$)", bad):
        add("tracked-junk", bad, 0, "build/runtime artifact tracked")
    if re.search(r"\.(pt|pth|pkl|joblib|onnx|h5|ckpt|safetensors)$", bad):
        try:
            if (ROOT / bad).stat().st_size > 1_000_000:
                add("model-artifact-tracked", bad, 0, f"{(ROOT / bad).stat().st_size // 1_000_000} MB model binary in git")
        except OSError:
            pass
    try:
        if (ROOT / bad).is_file() and (ROOT / bad).stat().st_size > 5_000_000:
            add("large-tracked-file", bad, 0, f"{(ROOT / bad).stat().st_size // 1_000_000} MB")
    except OSError:
        pass
hist = run(r"git log --all --name-only --pretty=format: | grep -Ei '(^|/)\.env$|\.pem$|id_rsa|id_ed25519|secrets?\.(json|ya?ml)$|wallet\.json$|keystore' | sort -u")
for h in (hist or "").splitlines()[:15]:
    add("secret-file-in-history", h, 0, "secret-like file exists in git history")
used = set()
for p in SRC:
    for n in ast.walk(TREES.get(p, ast.Module(body=[], type_ignores=[]))):
        if isinstance(n, ast.Call) and dotted(n.func) in ("os.getenv", "os.environ.get") and n.args and isinstance(n.args[0], ast.Constant):
            used.add(n.args[0].value)
        if isinstance(n, ast.Subscript) and dotted(n.value) == "os.environ" and isinstance(n.slice, ast.Constant):
            used.add(n.slice.value)
ex = ROOT / ".env.example"
doc = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", read(ex), re.M)) if ex.exists() else set()
for k in sorted(x for x in used if isinstance(x, str) and x not in doc):
    add("env-undocumented", ".env.example", 0, f"{k} used in code, not documented")
# docs
for label, pats in (("README", r"^README"), ("LICENSE", r"^LICEN[SC]E"), ("SECURITY.md", r"^SECURITY"), ("CONTRIBUTING", r"^CONTRIBUTING"), ("CHANGELOG", r"^CHANGELOG")):
    if not any(re.match(pats, f.name, re.I) for f in ROOT.iterdir() if f.is_file()) and not (label == "SECURITY.md" and (ROOT / ".github" / "SECURITY.md").exists()):
        add("missing-doc-file", label, 0, f"{label} not found")
if not any(re.search(r"runbook|incident|playbook|disaster|operations", str(rel(p)), re.I) for p in OTHER):
    add("no-runbook", "docs", 0, "no runbook/incident/DR document found")
if STAT["pub_total"] and STAT["pub_doc"] / STAT["pub_total"] < 0.3:
    add("low-docstring-coverage", "src", 0, f"{STAT['pub_doc']}/{STAT['pub_total']} public items documented")
# frontend
js = [p for p in OTHER if p.suffix in (".js", ".jsx", ".ts", ".tsx", ".vue", ".html")][:3000]
for p in js:
    t = read(p, 500000)
    for i, line in enumerate(t.splitlines(), 1):
        if re.search(r"dangerouslySetInnerHTML|\.innerHTML\s*=|document\.write\(|\beval\(", line):
            add("js-xss-sink", rel(p), i, line.strip()[:80])
        if re.search(r"localStorage\.setItem\([^)]*(token|key|secret|jwt)", line, re.I):
            add("js-token-storage", rel(p), i, line.strip()[:80])
if (ROOT / "package.json").exists() or list(ROOT.glob("*/package.json")):
    if not any(list(ROOT.glob(f"**/{n}")) for n in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")):
        add("no-js-lockfile", "package.json", 0, "no JS lockfile")
sh_ = run("git shortlog -sn --all --no-merges HEAD")
if sh_:
    rows = [int(x.split()[0]) for x in sh_.splitlines() if re.match(r'\s*\d+\s', x)]
    if len(rows) > 1 and rows[0] / sum(rows) > 0.9:
        add("bus-factor", "git", 0, f"top author {rows[0]}/{sum(rows)} commits")
churn = run("git log --since=90.days --name-only --pretty=format: | grep '\\.py$' | sort | uniq -c | sort -rn | head -10")

# ------------------------------------------------------------------ external tools
TOOLS = {}
if not args.no_tools:
    TOOLS = {
        "ruff": "ruff check . --statistics --exit-zero",
        "bandit": f"bandit -r {args.src} -ll -q -f txt",
        "vulture": f"vulture {args.src} --min-confidence 80",
        "radon-cc": f"radon cc {args.src} -n D -s",
        "pip-audit": "pip-audit -r requirements.txt --progress-spinner off" if (ROOT / "requirements.txt").exists() else "pip-audit --progress-spinner off",
        "gitleaks": "gitleaks detect --no-banner --redact",
        "hadolint": "hadolint Dockerfile" if (ROOT / "Dockerfile").exists() else "",
        "actionlint": "actionlint" if wf else "",
        "trivy": "trivy fs --quiet --scanners vuln,secret,misconfig --severity HIGH,CRITICAL .",
    }
    if args.slow:
        TOOLS["pytest-cov"] = f"python3 -m pytest -q -x --maxfail=5 --cov={args.src} --cov-report=term-missing:skip-covered"
        TOOLS["mypy"] = f"mypy {args.src} --ignore-missing-imports --no-error-summary"
    TOOLS = {k: v for k, v in TOOLS.items() if v}
tool_out = {}
if TOOLS:
    with cf.ThreadPoolExecutor(6) as ex_:
        futs = {k: ex_.submit(run, v, 3600 if "pytest" in k else 600) for k, v in TOOLS.items()}
        tool_out = {k: f.result() for k, f in futs.items()}

# ------------------------------------------------------------------ scoring + baseline
prev = []
pf = ROOT / "audit_findings.json"
if pf.exists():
    try:
        prev = json.loads(pf.read_text()).get("findings", [])
    except Exception:
        prev = []
key = lambda f: (f["rule"], f["file"], f["msg"])
prevk, curk = {key(f) for f in prev}, {key(f) for f in F}
new = [f for f in F if key(f) not in prevk] if prev else []
fixed = [f for f in prev if key(f) not in curk] if prev else []
F.sort(key=lambda x: (SEV[x["sev"]], x["domain"], x["rule"], x["file"], x["line"]))
score = {}
for d in DOMAINS:
    items = [f for f in F if f["domain"] == d]
    by_rule = collections.Counter((f["rule"], f["sev"]) for f in items)
    pen = sum(W[sv] * (1 + math.log2(n)) for (r_, sv), n in by_rule.items())
    score[d] = (max(0, round(100 - min(100, pen))), collections.Counter(f["sev"] for f in items))
overall = round(sum(v[0] for v in score.values()) / len(score))
json.dump(dict(findings=F, scores={d: v[0] for d, v in score.items()}, caps=CAPS, tools=tool_out), open("audit_findings.json", "w"), indent=1)

# ------------------------------------------------------------------ report
loc = sum(TEXT[p].count("\n") for p in SRC)
tloc = sum(TEXT[p].count("\n") for p in TESTS)
typed = 100 * STAT["fn_typed"] // max(1, STAT["fn_total"])
chains = sorted(x for x in IMPORTS if x in CHAIN_LIBS)
out = [f"# Engineering audit\n\nsrc: {len(SRC)} files / {loc} LOC | tests: {len(TESTS)} files / {tloc} LOC / {STAT['tests']} test fns | "
       f"type-hinted fns: {typed}% | docstring coverage: {100 * STAT['pub_doc'] // max(1, STAT['pub_total'])}% | routes: {STAT['routes']}\n"
       f"chain/exchange libs: {', '.join(chains) or 'none'} | storage libs: {', '.join(stores) or 'none'}\n",
       f"## Overall score: {overall}/100\n\n| domain | score | crit | high | med | low |\n|---|---|---|---|---|---|"]
for d in sorted(score, key=lambda x: score[x][0]):
    c = score[d][1]
    out.append(f"| {d} | {score[d][0]} | {c['critical']} | {c['high']} | {c['medium']} | {c['low']} |")
if prev:
    out.append(f"\nSince last run: **{len(new)} new**, **{len(fixed)} fixed**")
out.append("\n## Fix first (top 20 by severity)")
seen = set()
top = []
for f in F:
    if f["sev"] in ("critical", "high") and f["rule"] not in seen:
        seen.add(f["rule"]); top.append(f)
for f in top[:20]:
    n = sum(1 for x in F if x["rule"] == f["rule"])
    out.append(f"- [{f['sev']}] **{f['rule']}** x{n} ({f['domain']}): {f['fix']}. e.g. {f['file']}:{f['line']} {f['msg']}")
out.append("\n## Trading-safety capability map\n" + ", ".join(f"{'OK' if v else 'MISSING'} {k}" for k, v in CAPS.items()))
cx = sorted(COMPLEX, reverse=True)[:10]
out.append("\n## Most complex functions\n" + "\n".join(f"- {c}: {f}:{l} {n}" for c, f, l, n in cx))
sz = sorted(((TEXT[p].count("\n"), str(rel(p))) for p in SRC), reverse=True)[:10]
out.append("\n## Largest files\n" + "\n".join(f"- {n}: {f}" for n, f in sz))
out.append("\n## Git churn (90d)\n```\n" + (churn if churn and not churn.startswith("fatal") else "n/a") + "\n```")
for d in DOMAINS:
    items = [f for f in F if f["domain"] == d]
    if not items:
        continue
    out.append(f"\n## {d} (score {score[d][0]})")
    rules = collections.OrderedDict()
    for f in items:
        rules.setdefault((f["sev"], f["rule"]), []).append(f)
    for (sv, rule), lst in sorted(rules.items(), key=lambda kv: (SEV[kv[0][0]], -len(kv[1]))):
        if sv == "info":
            out.append(f"- [info] {rule}: {len(lst)}")
            continue
        out.append(f"\n### [{sv}] {rule} x{len(lst)}\nFix: {lst[0]['fix']}")
        for f in lst[:args.max]:
            out.append(f"- {f['file']}:{f['line']} {f['msg']}")
        if len(lst) > args.max:
            out.append(f"- ... +{len(lst) - args.max} more (audit_findings.json)")
if new:
    out.append("\n## NEW since last run (top 40)\n" + "\n".join(f"- [{f['sev']}] {f['rule']} {f['file']}:{f['line']} {f['msg']}" for f in new[:40]))
for k, v in tool_out.items():
    body = "(skipped: tool not installed)" if v is None else "\n".join(v.splitlines()[:50])
    out.append(f"\n## tool: {k}\n```\n{body}\n```")
pathlib.Path("audit_report.md").write_text("\n".join(out))

if args.sarif:
    lv = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
    sarif = {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "runs": [{
        "tool": {"driver": {"name": "audit3", "rules": [{"id": k, "shortDescription": {"text": v[2]}} for k, v in R.items()]}},
        "results": [{"ruleId": f["rule"], "level": lv[f["sev"]], "message": {"text": f["msg"]},
                     "locations": [{"physicalLocation": {"artifactLocation": {"uri": f["file"]}, "region": {"startLine": max(1, f["line"])}}}]} for f in F]}]}
    json.dump(sarif, open(args.sarif, "w"))
cnt = collections.Counter(f["sev"] for f in F)
print(f"wrote audit_report.md + audit_findings.json | score {overall}/100 | " + " ".join(f"{k}:{cnt.get(k, 0)}" for k in SEV))
if args.fail_on and any(SEV[f["sev"]] <= SEV[args.fail_on] for f in F):
    sys.exit(1)
