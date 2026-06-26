# Tool Version Registry — Debt-006 canonical source

This file is the **single source of truth** for pinned tool versions.
All of `.pre-commit-config.yaml`, `.trunk/trunk.yaml`, and CI must match.

| Tool | Pinned Version | Rationale |
|---|---|---|
| ruff | 0.4.4 | Matches trunk.yaml + pre-commit (stable, tested) |
| mypy | 1.10.0 | Matches trunk.yaml + pre-commit |
| bandit | 1.9.4 | Installed version; trunk pinned 1.7.8 (stale) |
| pyright | 1.1.360 | Matches trunk.yaml |
| trufflehog | 3.78.0 | Matches trunk.yaml + pre-commit |
| semgrep | 1.72.0 | Matches pre-commit |

## Update procedure
1. Update version here
2. Update `.trunk/trunk.yaml` enabled tools list
3. Update `.pre-commit-config.yaml` rev: lines
4. Update `requirements-dev.txt` pin
5. Verify `ci.yml` installs from requirements-dev.txt (picks up change automatically)
