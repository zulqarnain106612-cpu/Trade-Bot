"""
Bash integration: an rcfile that behaves like the user's normal ~/.bashrc and
additionally reports each command's start, text and exit status.

A session's bash is started as ``bash --rcfile <this file> -i``. ``--rcfile``
only replaces ~/.bashrc (bash still reads /etc/bash.bashrc first), and the
first thing this file does is source ~/.bashrc, so the user's prompt, aliases
and PATH are exactly what any other terminal gives them. The hooks are added
afterwards, so a .bashrc that rewrites PROMPT_COMMAND or PS0 cannot remove
them:

* ``PS0`` -- expanded after a command line is read and before it runs --
  prints ``OSC 133;E;<command>`` and ``OSC 133;C``.
* ``PROMPT_COMMAND`` runs first at every prompt, while ``$?`` still holds the
  finished command's status, and prints ``OSC 133;D;<status>``.

The command text is taken from history and has control characters replaced,
so it can never terminate the escape sequence early or inject a new one.

Registry: TERM-002 (config/quality_registry.json).
"""

from __future__ import annotations

import os
from pathlib import Path

RCFILE = r"""# managed-by: tradebot-terminal -- regenerated on every daemon start.
if [ -f "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi

__tb_term_preexec() {
  local h
  h=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)
  h="${h#"${h%%[![:space:]]*}"}"
  h="${h#*[[:space:]]}"
  h="${h#"${h%%[![:space:]]*}"}"
  h="${h//[$'\001'-$'\037'$'\177']/ }"
  builtin printf '\033]133;E;%s\007\033]133;C\007' "${h:0:1024}"
}

__tb_term_precmd() {
  local status=$?
  builtin printf '\033]133;D;%s\007' "$status"
  return $status
}

# History must be on for the command text; it is per-session and in memory,
# HISTFILE is left exactly as the user's configuration set it.
set -o history
# Explicitly enable job control for reliable foreground process-group tracking.
set -m
PROMPT_COMMAND="__tb_term_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
PS0="${PS0:-}"'$(__tb_term_preexec)'
"""


def write_rcfile(path: Path) -> Path:
    """Write the integration rcfile, readable only by this user."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(RCFILE)
    os.chmod(path, 0o600)
    return path


def shell_argv(shell: str, rcfile: Path) -> list[str]:
    """argv for an interactive session of *shell*, integrated when it is bash."""
    if Path(shell).name == "bash":
        return [shell, "--rcfile", str(rcfile), "-i"]
    return [shell, "-i"]
