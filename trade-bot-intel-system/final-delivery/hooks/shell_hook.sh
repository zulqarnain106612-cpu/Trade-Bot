#!/usr/bin/env bash
# ============================================================
# Project Intelligence Shell Hook
# Auto-injected into ~/.bashrc and ~/.zshrc by installer.
# On every new terminal: touches session marker, prints primer.
# On every prompt: checks if active project changed.
# ============================================================

# ── Active project tracking ───────────────────────────────────────────────────
export INTEL_ACTIVE_PROJECT=""
export INTEL_LAST_DIR=""

# Called once when shell starts
_intel_session_start() {
    local dir
    dir="$(pwd)"

    # Walk up from current dir to find a .project-intel directory
    local check="$dir"
    while [[ "$check" != "/" ]]; do
        if [[ -d "$check/.project-intel" ]]; then
            INTEL_ACTIVE_PROJECT="$check"
            _intel_mark_new_session "$check"
            return
        fi
        check="$(dirname "$check")"
    done
}

# Called on every cd / directory change (via PROMPT_COMMAND or precmd)
_intel_check_project() {
    local dir
    dir="$(pwd)"

    # Only act if directory actually changed
    [[ "$dir" == "$INTEL_LAST_DIR" ]] && return
    INTEL_LAST_DIR="$dir"

    # Walk up looking for .project-intel
    local check="$dir"
    local found=""
    while [[ "$check" != "/" ]]; do
        if [[ -d "$check/.project-intel" ]]; then
            found="$check"
            break
        fi
        check="$(dirname "$check")"
    done

    if [[ "$found" != "$INTEL_ACTIVE_PROJECT" ]]; then
        INTEL_ACTIVE_PROJECT="$found"
        if [[ -n "$found" ]]; then
            _intel_mark_new_session "$found"
        fi
    fi
}

# Touch the session marker — daemon detects this
_intel_mark_new_session() {
    local project="$1"
    local marker="$project/.project-intel/.session_marker"
    touch "$marker" 2>/dev/null

    # Print primer inline so agent/terminal sees it immediately
    local primer="$project/.project-intel/CONTEXT_PRIMER.md"
    if [[ -f "$primer" ]]; then
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  PROJECT INTEL LOADED: $(basename "$project")"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        cat "$primer"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
    fi

    # Also write to a tmp file that Claude Code / IDE extensions can read
    local session_context="$project/.project-intel/.active_session_context"
    {
        echo "SESSION_START=$(date -Iseconds)"
        echo "PROJECT=$project"
        echo "PRIMER_PATH=$primer"
        cat "$primer"
    } > "$session_context"
}

# ── PROMPT_COMMAND integration (bash) ────────────────────────────────────────
if [[ -n "$BASH_VERSION" ]]; then
    # Append to existing PROMPT_COMMAND
    if [[ -z "$PROMPT_COMMAND" ]]; then
        PROMPT_COMMAND="_intel_check_project"
    else
        PROMPT_COMMAND="${PROMPT_COMMAND};_intel_check_project"
    fi
    # Run once on shell start
    _intel_session_start
fi

# ── precmd integration (zsh) ─────────────────────────────────────────────────
if [[ -n "$ZSH_VERSION" ]]; then
    autoload -Uz add-zsh-hook
    add-zsh-hook precmd _intel_check_project
    # Run once on shell start
    _intel_session_start
fi

# ── Helper commands ───────────────────────────────────────────────────────────

# Show current project intel status
intel-status() {
    if [[ -z "$INTEL_ACTIVE_PROJECT" ]]; then
        echo "No intel project active in current directory"
        return
    fi
    local state="$INTEL_ACTIVE_PROJECT/.project-intel/SESSION_STATE.json"
    echo "Active project: $INTEL_ACTIVE_PROJECT"
    if [[ -f "$state" ]]; then
        python3 -c "
import json, sys
s = json.load(open('$state'))
print(f'  Last updated:  {s.get(\"last_updated\", \"unknown\")}')
print(f'  Current focus: {s.get(\"current_focus\", \"not set\")}')
print(f'  Next task:     {s.get(\"next_recommended_task\", \"check OPEN_TASKS.md\")}')
print(f'  Sessions:      {s.get(\"total_sessions\", 0)}')
"
    fi
}

# Manually reload primer (e.g. after major refactor)
intel-reload() {
    local project="${INTEL_ACTIVE_PROJECT:-$(pwd)}"
    python3 "$project/.project-intel/scripts/extract_intelligence.py" "$project"
    echo "Intel reloaded for $project"
}

# Show what the agent should read next session
intel-next() {
    local project="${INTEL_ACTIVE_PROJECT:-$(pwd)}"
    local state="$project/.project-intel/SESSION_STATE.json"
    if [[ -f "$state" ]]; then
        python3 -c "
import json
s = json.load(open('$state'))
print('Next task:', s.get('next_recommended_task', 'check OPEN_TASKS.md'))
print('Current focus:', s.get('current_focus', 'not set'))
print('Last modified:', s.get('last_files_modified', []))
"
    fi
}

# Route agent output — gaps/issues go to project, chat goes to terminal
# Usage: claude "task" | intel-route
# Or in watch mode: intel-route --watch .project-intel/.agent_output
intel-route() {
    local project="${INTEL_ACTIVE_PROJECT:-$(pwd)}"
    PYTHONPATH="$HOME/.config/project-intel/daemon" \
    python3 "$HOME/.config/project-intel/daemon/output_router.py" \
        --project "$project" "$@"
}

export -f intel-status intel-reload intel-next intel-route 2>/dev/null || true
