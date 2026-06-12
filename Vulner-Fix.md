# Vulner-Fix.md — Vulnerability & Issue Tracker
<!-- AUTO-MANAGED: Claude / Copilot agents append to this file. Never overwrite. -->
<!-- FORMAT: Each finding is one block. Status changes from OPEN → IN_PROGRESS → APPLIED. -->
<!-- RULE: Agents MUST append new findings at the LAST LINE. Never insert above existing entries. -->
<!-- RULE: When a fix is successfully applied, update status to `Applied` on that entry only. -->

---

## How to read this file

| Field | Meaning |
|-------|---------|
| `ID` | Unique sequential ID |
| `Severity` | CRITICAL / HIGH / MEDIUM / LOW / INFO |
| `Tool` | Which tool detected it (CodeQL / bandit / semgrep / manual / Claude / Copilot) |
| `Status` | `Open` → `In Progress` → `Applied` |
| `File` | Source file and line number |
| `Fix` | Exact fix applied or to apply |

---

## Entries

<!-- AGENTS: append new entries below this line, one block per finding, newest at bottom -->

### [VF-001] — 2025-06-01 — Initial audit baseline
- **Severity:** INFO
- **Tool:** Manual audit (VULNERABILITY_AUDIT_AND_FIXES.md)
- **Status:** Applied
- **Summary:** 30 vulnerabilities (9 critical, 9 high, 8 medium, 4 low) identified in initial SCAN3 audit.
- **Fix:** All 30 vulnerabilities fixed across 13 files. See git log for full diff.
- **Verified:** `python3 -m py_compile src/**/*.py` → ALL_OK

<!-- NEW FINDINGS BELOW THIS LINE -->
### [VF-002] — 2026-06-12 04:35 UTC
- **Severity:** LOW
- **Tool:** test
- **File:** `test:1`
- **Status:** Applied
- **Summary:** Autocommit test entry
- **Fix:** No fix needed

