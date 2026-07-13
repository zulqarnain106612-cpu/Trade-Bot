"""Run one shard of a cosmic-ray mutation-testing session.

cosmic-ray's `exec` command has no built-in way to run a subset of a
session's mutants, so GitHub Actions matrix parallelism needs this: each
matrix job downloads the SAME pre-initialised session DB (built once by a
"prepare" job so every shard mutates identically), deterministically selects
its slice of the pending work items by hashing job_id, and calls cosmic-ray's
own `mutate_and_test` worker function directly for just that slice. Each
shard's result is written back into its own local copy of the session DB —
never shared across runners — and a later aggregation job merges all shard
DBs into one report.

Usage: python mutation_shard_runner.py <config.toml> <session.sqlite> <shard_index> <shard_count>
"""

import hashlib
import sys

from cosmic_ray.config import load_config
from cosmic_ray.mutating import mutate_and_test
from cosmic_ray.work_db import WorkDB, use_db


def shard_of(job_id: str, shard_count: int) -> int:
    digest = hashlib.sha256(job_id.encode()).hexdigest()
    return int(digest, 16) % shard_count


def main() -> None:
    config_file, session_file, shard_index_s, shard_count_s = sys.argv[1:5]
    shard_index = int(shard_index_s)
    shard_count = int(shard_count_s)

    config = load_config(config_file)
    test_command = config.test_command
    timeout = config.timeout

    with use_db(session_file, mode=WorkDB.Mode.open) as db:
        my_items = [
            item
            for item in db.pending_work_items
            if shard_of(item.job_id, shard_count) == shard_index
        ]
        print(f"shard {shard_index}/{shard_count}: {len(my_items)} mutant(s) assigned")

        for i, item in enumerate(my_items, start=1):
            result = mutate_and_test(
                mutations=item.mutations,
                test_command=test_command,
                timeout=timeout,
            )
            db.set_result(item.job_id, result)
            print(
                f"  [{i}/{len(my_items)}] {item.job_id[:12]} -> {result.worker_outcome}/{result.test_outcome}"
            )


if __name__ == "__main__":
    main()
