#!/usr/bin/env bash
# Trade-Bot-main — Universal AI Workspace Bootstrap
# Idempotent, safe to re-run, never overwrites user files without confirmation.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_DIR="$PROJECT_ROOT/.ai"
TS="$(date +%Y%m%d-%H%M%S)"

RED='\033[0;31m';YEL='\033[1;33m';GRN='\033[0;32m';BLU='\033[0;34m';NC='\033[0m'
ok()   { echo -e "${GRN}  ✓${NC} $*"; }
warn() { echo -e "${YEL}  ⚠${NC} $*"; }
info() { echo -e "${BLU}  →${NC} $*"; }

echo -e "\n==================================================================="
echo -e "  Trade-Bot-main | AI Workspace Bootstrap | $TS"
echo -e "===================================================================\n"

# ── 1. OS Detection ──────────────────────────────────────────────────
OS="linux"
[[ "$OSTYPE" == "darwin"* ]] && OS="mac"
[[ -n "${WSL_DISTRO_NAME:-}${WSL_INTEROP:-}" ]] && OS="wsl"
ok "OS: $OS"

# ── 2. Tool Detection ────────────────────────────────────────────────
HAS_UV=false;HAS_GIT=false;HAS_NODE=false;HAS_DOCKER=false;HAS_AIDER=false
command -v uv     &>/dev/null && { HAS_UV=true;     ok "uv:     $(uv --version)"; }     || warn "uv not found"
command -v git    &>/dev/null && { HAS_GIT=true;    ok "git:    $(git --version)"; }    || warn "git not found"
command -v node   &>/dev/null && { HAS_NODE=true;   ok "node:   $(node --version)"; }   || warn "node not found"
command -v docker &>/dev/null && { HAS_DOCKER=true; ok "docker: $(docker --version | head -1)"; } || warn "docker not found"
command -v aider  &>/dev/null && { HAS_AIDER=true;  ok "aider:  $(aider --version 2>&1|head -1)"; } || warn "aider not found (pip install aider-chat)"

# ── 3. Directory Scaffold (idempotent) ───────────────────────────────
echo ""; info "Ensuring .ai/ workspace scaffold..."
mkdir -p "$AI_DIR"/{prompts/{aider,claude,copilot,codex,gemini,shared},sessions,cache,templates,context,logs,scripts,commands,agents,docs,configs}
ok ".ai/ directories verified"

# ── 4. Aider config symlink → root (idempotent) ──────────────────────
AIDER_CONF="$PROJECT_ROOT/.aider.conf.yml"
AIDER_SRC="$AI_DIR/configs/aider.conf.yml"
if [ ! -e "$AIDER_CONF" ]; then
  ln -s "$AIDER_SRC" "$AIDER_CONF" && ok ".aider.conf.yml symlinked from .ai/configs/"
elif [ -L "$AIDER_CONF" ]; then
  ok ".aider.conf.yml symlink already exists"
else
  warn ".aider.conf.yml exists as regular file — not overwriting. Reference: $AIDER_SRC"
fi

# ── 5. .gitignore entries ────────────────────────────────────────────
echo ""; info "Checking .gitignore..."
GITIGNORE="$PROJECT_ROOT/.gitignore"
touch "$GITIGNORE"
ENTRIES=(".ai/sessions/" ".ai/logs/" ".ai/cache/" ".aider.conf.yml")
for entry in "${ENTRIES[@]}"; do
  if ! grep -qF "$entry" "$GITIGNORE" 2>/dev/null; then
    echo "$entry" >> "$GITIGNORE"
    ok "Added to .gitignore: $entry"
  else
    ok "Already in .gitignore: $entry"
  fi
done

# ── 6. VS Code workspace settings ────────────────────────────────────
echo ""; info "VS Code settings..."
VSCODE_DIR="$PROJECT_ROOT/.vscode"
mkdir -p "$VSCODE_DIR"
VSCODE_SETTINGS="$VSCODE_DIR/settings.json"
if [ ! -f "$VSCODE_SETTINGS" ]; then
  cat > "$VSCODE_SETTINGS" <<'VSJSON'
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.analysis.typeCheckingMode": "basic",
  "editor.formatOnSave": true,
  "editor.codeActionsOnSave": {"source.fixAll.ruff": "explicit", "source.organizeImports.ruff": "explicit"},
  "[python]": {"editor.defaultFormatter": "charliermarsh.ruff"},
  "files.exclude": {".venv": true, ".venv_temp": true, "__pycache__": true, ".mypy_cache": true, ".ruff_cache": true},
  "github.copilot.advanced": {"codeReferenceEnabled": false},
  "github.copilot.editor.enableAutoCompletions": true
}
VSJSON
  ok "VS Code settings.json created"
else
  ok "VS Code settings.json already exists — not overwritten"
fi

# ── 7. Aider install / upgrade ────────────────────────────────────────
echo ""; info "Aider installation check..."
if [ "$HAS_AIDER" = false ]; then
  warn "Installing aider via pip..."
  pip install --quiet aider-chat && ok "aider installed" || warn "aider install failed — install manually"
else
  ok "aider already installed"
fi

# ── 8. Context refresh ───────────────────────────────────────────────
echo ""; info "Running initial context refresh..."
if [ -x "$AI_DIR/scripts/context-refresh" ]; then
  "$AI_DIR/scripts/context-refresh" && ok "Context refreshed"
else
  warn "context-refresh script not found — skipping"
fi

# ── 9. Validate aider config ─────────────────────────────────────────
echo ""; info "Validating aider config..."
if command -v aider &>/dev/null && [ -f "$AIDER_SRC" ]; then
  ok "aider config: $AIDER_SRC"
else
  warn "Could not validate aider config"
fi

# ── 10. Summary ──────────────────────────────────────────────────────
echo -e "\n==================================================================="
echo -e "  Bootstrap complete!  Workspace: $AI_DIR"
echo -e "===================================================================\n"
echo "  Available commands:"
echo "    .ai/scripts/aider-session   # Start aider with project config"
echo "    .ai/scripts/architect       # Architecture planning mode"
echo "    .ai/scripts/review          # AI review of staged changes"
echo "    .ai/scripts/commit          # Lint → test → commit"
echo "    .ai/scripts/lint            # ruff + mypy"
echo "    .ai/scripts/test [filter]   # pytest"
echo "    .ai/scripts/context-refresh # Refresh project index"
echo ""
echo "  Config files:"
echo "    .ai/configs/aider.conf.yml  Claude / aider"
echo "    .ai/configs/claude.md       Claude instructions"
echo "    .ai/configs/copilot.json    GitHub Copilot"
echo "    .ai/configs/cursor.json     Cursor"
echo "    .ai/configs/gemini.md       Gemini CLI"
echo "    .ai/configs/codex.md        OpenAI Codex CLI"
echo "    .ai/configs/ollama.json     Local models (Ollama)"
echo "    .ai/configs/mcp.json        MCP server registry"
echo ""
echo "  Shared prompts:   .ai/prompts/"
echo "  Project context:  .ai/context/project.md"
echo ""
