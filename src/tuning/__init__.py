"""Self-tuning subsystem — see docs/SELF_TUNING_DESIGN.md.

Registration (src/tuning/bootstrap.py) and promotion (src/tuning/runner.py,
gated behind SELF_TUNING_ENABLED + SELF_TUNING_SHADOW_MODE=false) are both
still explicit, operator-driven steps -- nothing here becomes live merely
by importing this package. Once a parameter IS registered and promoted,
src/tuning/live_overrides.py is the seam that surfaces the promoted value
to the live regime/risk/features/model code paths.
"""
