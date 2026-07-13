"""Resolve mutation-testing run parameters for the GitHub Actions workflow.

Called once, in the `prepare` job of .github/workflows/mutation-testing.yml,
before any other job needs these values. There are two trigger types:

- workflow_dispatch: parameters come from the run's `inputs.*` (passed in
  here as INPUT_* env vars, since embedding `${{ inputs.* }}` directly in a
  script body is best avoided).
- push (to .github/mutation-trigger.json): parameters come from that file.

Writes the resolved values twice: to $GITHUB_ENV (so later steps in the
*same* job, i.e. prepare, can use them via the `env` context) and to
$GITHUB_OUTPUT (so the job's `outputs:` mapping can expose them to the
mutate/aggregate jobs via `needs.prepare.outputs.*` — $GITHUB_ENV alone does
NOT cross job boundaries, each job runs on its own fresh runner).
"""

import json
import os


def main() -> None:
    if os.environ["GITHUB_EVENT_NAME"] == "push":
        with open(".github/mutation-trigger.json") as f:
            trigger = json.load(f)
        values = {
            "MODULE_PATH": str(trigger["module_path"]),
            "TEST_FILES": str(trigger["test_files"]),
            "SHARD_COUNT": str(trigger["shard_count"]),
            "PER_MUTANT_TIMEOUT_SECONDS": str(trigger["per_mutant_timeout_seconds"]),
        }
    else:
        values = {
            "MODULE_PATH": os.environ["INPUT_MODULE_PATH"],
            "TEST_FILES": os.environ["INPUT_TEST_FILES"],
            "SHARD_COUNT": os.environ["INPUT_SHARD_COUNT"],
            "PER_MUTANT_TIMEOUT_SECONDS": os.environ["INPUT_PER_MUTANT_TIMEOUT_SECONDS"],
        }

    with open(os.environ["GITHUB_ENV"], "a") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        for key, value in values.items():
            f.write(f"{key.lower()}={value}\n")


if __name__ == "__main__":
    main()
