"""Shared process-resource budgets for training and runtime health monitoring."""

from typing import Final

MEMORY_WARN_MB: Final[float] = 512.0
MEMORY_CRITICAL_MB: Final[float] = 1024.0

# Conservative XGBoost profile for the repository's 1 GiB process RSS budget.
# These are effective caps, not edits to user-provided .env values. The
# unchanged CPCV/live gates determine whether a capped model is acceptable.
XGB_1GIB_MAX_ESTIMATORS: Final[int] = 300
XGB_1GIB_MAX_DEPTH: Final[int] = 4
XGB_1GIB_MAX_JOBS: Final[int] = 1
