"""Merge per-shard cosmic-ray session DBs (from mutation_shard_runner.py)
into one master session and print a summary report.

Each shard DB was seeded from the identical `cosmic-ray init` output, so
every DB has the full set of work items but results only for the job_ids
that shard computed (job_ids are disjoint across shards by construction —
see mutation_shard_runner.shard_of). Merging is therefore a plain union of
each shard's completed_work_items into a fresh master DB.

Usage: python mutation_aggregate.py <master_session.sqlite> <shard_db1> [shard_db2 ...]
"""

import sys

from cosmic_ray.work_db import WorkDB, use_db


def main() -> None:
    master_path, *shard_paths = sys.argv[1:]

    with use_db(master_path, mode=WorkDB.Mode.open) as master:
        total_work_items = master.num_work_items
        merged = 0
        for shard_path in shard_paths:
            with use_db(shard_path, mode=WorkDB.Mode.open) as shard_db:
                for job_id, result in shard_db.results:
                    assert (
                        result is not None
                    ), f"shard {shard_path} yielded null result for {job_id}"
                    master.set_result(job_id, result)
                    merged += 1

        killed = survived = incompetent = other = 0
        survivors = []
        for work_item, result in master.completed_work_items:
            outcome = result.test_outcome
            if outcome is not None and outcome.value == "killed":
                killed += 1
            elif outcome is not None and outcome.value == "survived":
                survived += 1
                m = work_item.mutations[0]
                survivors.append(
                    f"{m.module_path}:{m.start_pos} in {m.operator_name} (job {work_item.job_id[:12]})"
                )
            elif outcome is not None and outcome.value == "incompetent":
                incompetent += 1
            else:
                other += 1

        completed = master.num_results
        print(f"total work items: {total_work_items}")
        print(f"merged results:    {merged}")
        print(f"completed:         {completed}/{total_work_items}")
        print(f"killed:            {killed}")
        print(f"survived:          {survived}")
        print(f"incompetent:       {incompetent}")
        print(f"other:             {other}")
        if killed + survived:
            score = killed / (killed + survived) * 100
            print(f"mutation score:    {score:.1f}%")
        if survivors:
            print("\nsurvivors:")
            for s in survivors:
                print(f"  {s}")

        if completed < total_work_items:
            print(
                f"\nWARNING: {total_work_items - completed} work item(s) have no result "
                "— a shard may have failed or been misconfigured.",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
