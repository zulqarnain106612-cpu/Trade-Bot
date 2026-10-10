"""
Read-only views of /proc for process identity and resource usage.

Everything the process center shows about a running process comes from here:
the argv the kernel holds (not what a client claims), its working directory,
its process group and session, when it started, and how much CPU and memory
its group is using. Each reader returns None when the process is gone or not
readable rather than raising, because a process exiting between two reads is
the normal case, not an error -- and "unknown" is reported as unknown, never
filled in.

The root is injectable so the parsing can be tested against a fixture tree.

Registry: TERM-002 (config/quality_registry.json).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")


@dataclass(frozen=True)
class ProcStat:
    pid: int
    comm: str
    ppid: int
    pgrp: int
    session: int
    cpu_ticks: int
    start_ticks: int
    rss_pages: int


@dataclass(frozen=True)
class GroupUsage:
    cpu_ticks: int
    rss_bytes: int
    count: int


def read_stat(pid: int, proc: Path = PROC) -> ProcStat | None:
    try:
        raw = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_stat(raw)


def parse_stat(raw: str) -> ProcStat | None:
    """Parse one /proc/<pid>/stat line. comm may contain spaces and ')'."""
    open_paren = raw.find("(")
    close_paren = raw.rfind(")")
    if open_paren < 0 or close_paren < open_paren:
        return None
    fields = raw[close_paren + 2 :].split()
    try:
        return ProcStat(
            pid=int(raw[:open_paren].strip()),
            comm=raw[open_paren + 1 : close_paren],
            ppid=int(fields[1]),
            pgrp=int(fields[2]),
            session=int(fields[3]),
            cpu_ticks=int(fields[11]) + int(fields[12]),
            start_ticks=int(fields[19]),
            rss_pages=int(fields[21]),
        )
    except (IndexError, ValueError):
        return None


def read_cmdline(pid: int, proc: Path = PROC) -> list[str] | None:
    """The argv the kernel holds for *pid*; None if gone or a kernel thread."""
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    args = [part.decode("utf-8", errors="replace") for part in raw.split(b"\x00") if part]
    return args or None


def read_cwd(pid: int, proc: Path = PROC) -> str | None:
    try:
        return os.readlink(proc / str(pid) / "cwd")
    except OSError:
        return None


def boot_time(proc: Path = PROC) -> float | None:
    try:
        for line in (proc / "stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def clock_ticks() -> int:
    return os.sysconf("SC_CLK_TCK")


def page_size() -> int:
    return os.sysconf("SC_PAGE_SIZE")


def scan(proc: Path = PROC) -> list[ProcStat]:
    """Every process this user can read, in one pass over /proc."""
    stats: list[ProcStat] = []
    try:
        entries = os.listdir(proc)
    except OSError:
        return stats
    for name in entries:
        if name.isdigit():
            parsed = read_stat(int(name), proc)
            if parsed is not None:
                stats.append(parsed)
    return stats


def group_usage(stats: list[ProcStat], pgid: int) -> GroupUsage | None:
    members = [s for s in stats if s.pgrp == pgid]
    if not members:
        return None
    return GroupUsage(
        cpu_ticks=sum(s.cpu_ticks for s in members),
        rss_bytes=sum(s.rss_pages for s in members) * page_size(),
        count=len(members),
    )


def session_members(stats: list[ProcStat], sid: int) -> list[int]:
    """Pids whose session id is *sid* -- everything a PTY session started."""
    return [s.pid for s in stats if s.session == sid]
