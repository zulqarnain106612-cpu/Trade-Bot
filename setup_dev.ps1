<#
.SYNOPSIS
    One-shot dev environment setup + auto-fix for Trade Bot.
    Run from project root: D:\Trade-Bot\Trade-Bot\
    PowerShell: .\setup_dev.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT = $PSScriptRoot
Set-Location $ROOT

Write-Host "`n=== Trade Bot Dev Setup ===" -ForegroundColor Cyan

# ── 1. Activate venv ─────────────────────────────────────────────────────────
$VENV_ACTIVATE = Join-Path $ROOT ".venv\Scripts\Activate.ps1"
if (Test-Path $VENV_ACTIVATE) {
    Write-Host "`n[1/8] Activating .venv..." -ForegroundColor Yellow
    & $VENV_ACTIVATE
} else {
    Write-Host "`n[1/8] Creating .venv..." -ForegroundColor Yellow
    python -m venv .venv
    & $VENV_ACTIVATE
}

# ── 2. Upgrade pip + install core deps ───────────────────────────────────────
Write-Host "`n[2/8] Upgrading pip + installing core dependencies..." -ForegroundColor Yellow
python -m pip install --upgrade pip --quiet

$CORE_DEPS = @(
    "fastapi", "uvicorn[standard]", "websockets",
    "ccxt",
    "xgboost", "hmmlearn", "scikit-learn",
    "pandas", "numpy", "scipy", "statsmodels",
    "aiosqlite", "pydantic-settings",
    "structlog", "rich", "joblib",
    "pytest", "pytest-asyncio", "pytest-cov"
)
foreach ($dep in $CORE_DEPS) {
    Write-Host "  Installing $dep..." -ForegroundColor DarkGray
    python -m pip install $dep --quiet
}

# ── 3. Install linting / formatting / type-checking tools ────────────────────
Write-Host "`n[3/8] Installing dev tools (ruff, pyright, pre-commit)..." -ForegroundColor Yellow
python -m pip install ruff pyright pre-commit --quiet

# ── 4. Install type stubs for packages that have them ────────────────────────
Write-Host "`n[4/8] Installing type stubs..." -ForegroundColor Yellow
$STUBS = @(
    "pandas-stubs",
    "types-requests",
    "types-PyYAML",
    "types-python-dateutil"
)
foreach ($stub in $STUBS) {
    Write-Host "  Installing $stub..." -ForegroundColor DarkGray
    python -m pip install $stub --quiet
}

# ── 5. Run ruff auto-fix on all Python source ─────────────────────────────────
Write-Host "`n[5/8] Running ruff --fix on src/ and tests/..." -ForegroundColor Yellow

# Fix all auto-fixable issues
ruff check src/ tests/ --fix --unsafe-fixes --quiet
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
    Write-Host "  ruff check encountered errors (non-fixable issues remain — see below)" -ForegroundColor DarkYellow
}

# Format all files
ruff format src/ tests/
Write-Host "  ruff format complete" -ForegroundColor Green

# ── 6. Run pyright to count remaining real errors ────────────────────────────
Write-Host "`n[6/8] Running pyright (basic mode)..." -ForegroundColor Yellow
pyright src/ --project pyrightconfig.json 2>&1 | Select-String -Pattern "error|warning|information|0 error" | Select-Object -Last 5
Write-Host "  (Stubs/import noise suppressed by pyrightconfig.json)" -ForegroundColor DarkGray

# ── 7. Install pre-commit hooks ───────────────────────────────────────────────
Write-Host "`n[7/8] Installing pre-commit hooks..." -ForegroundColor Yellow
pre-commit install
Write-Host "  pre-commit installed — hooks run on every git commit" -ForegroundColor Green

# ── 8. Run test suite ─────────────────────────────────────────────────────────
Write-Host "`n[8/8] Running test suite..." -ForegroundColor Yellow
$env:EXECUTION_MODE = "automatic"
python -m pytest tests/ -q --tb=short 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  All tests passed" -ForegroundColor Green
} else {
    Write-Host "  Some tests failed — check output above" -ForegroundColor Red
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host @"

=== Setup Complete ===

What was done:
  ✓ .venv activated + all deps installed
  ✓ Type stubs installed (pandas-stubs, types-requests, etc.)
  ✓ ruff auto-fixed all fixable issues in src/ + tests/
  ✓ ruff formatted all files
  ✓ pyrightconfig.json suppresses stub/import noise (keeps real errors)
  ✓ .vscode/settings.json sets format-on-save + ruff as formatter
  ✓ pre-commit hooks installed (ruff runs on every commit)
  ✓ Test suite executed

VS Code — reload window now:
  Ctrl+Shift+P → 'Developer: Reload Window'
  Then check Problems panel — should be near zero.

To manually re-run fixes at any time:
  ruff check src/ tests/ --fix --unsafe-fixes
  ruff format src/ tests/

"@ -ForegroundColor Cyan
