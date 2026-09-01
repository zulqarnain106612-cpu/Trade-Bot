## What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

## How it was verified

<!-- The commands you ran and what they reported. For a bug fix, say how you
     confirmed the test fails on the unfixed code. -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest -q` passes, including the 99% coverage gate
- [ ] No new network, database, or exchange access in tests
- [ ] Any deliberately unreachable code is called out above
