# Contributing

## Environment

Python version is pinned in `.python-version`; Node 22 for the frontend.

```bash
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
```

`requirements-optional.txt` holds the heavy ML extras (mlflow, torch_geometric,
stable_baselines3, …). CI does **not** install them: code paths behind those
imports must degrade gracefully, and their suites are expected to skip rather
than fail.

## Checks

The three checks CI runs, in the order it runs them:

```bash
ruff check .
ruff format --check .
pytest -q
```

`ruff format --check` runs before the tests, so an unformatted file blocks the
run before a single test executes. Run `ruff format .` before pushing, or let
the pre-commit hook do it.

## Coverage gate

`pyproject.toml` puts `--cov --cov-branch --cov-fail-under=99` in pytest's
`addopts`, so **every** `pytest` invocation enforces the gate. Two consequences:

- Running a single file fails on coverage unless you pass `--no-cov`:
  `pytest tests/test_foo.py --no-cov -q`
- To check one module's coverage:
  `pytest tests/test_foo.py --cov=src.foo --cov-fail-under=0 -q`

Measured packages are listed under `[tool.coverage.run] source`.

## Tests

- Never contact a real network, database, or exchange. Inject fakes with
  `patch.dict(sys.modules, {...})` to reach the "dependency installed" branch
  of an optional import.
- Patch `asyncio.sleep` in anything that paces itself.
- `filterwarnings` promotes `DeprecationWarning` raised from `src.*` to an
  error. A test that fakes an entire third-party module also fakes away its
  deprecations — fake only the transport when the deprecation is the point.
- When a guard is genuinely unreachable, say so in the commit message instead
  of contorting a test to reach it.

## Pull requests

- Branch off `main`; do not push to `main` directly.
- One logical change per PR, with a commit message that explains *why*.
- All checks must pass before merge, including the aggregate `CodeQL` status.
- CodeQL runs `security-and-quality`, so quality notices in **test** files fail
  the check too.
