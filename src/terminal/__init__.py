"""
Integrated terminal and process service (tradebot-termd).

One host-user daemon owns every pseudo-terminal session and the registry of
processes and jobs started from them. The dashboard (through Electron IPC or a
loopback WebSocket) and the host CLI (``tradebot-term``) are both clients of
it, so they see the same sessions, the same output and the same process state
by construction rather than by synchronisation.

Design and threat model: docs/terminal/ARCHITECTURE.md.
Registry: TERM-001..TERM-008, SEC-0011 (config/quality_registry.json).
"""

from __future__ import annotations

#: Version of the service, reported in the welcome frame so a client can tell
#: an upgraded install from a daemon still running the previous code.
SERVICE_VERSION = "1.0.0"

#: Wire protocol version. A client and a daemon that disagree refuse the
#: connection rather than guessing at each other's frames.
PROTOCOL_VERSION = 1
