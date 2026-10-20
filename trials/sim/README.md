# trials.sim

Discrete-event simulator of three ways to schedule a pull-request gate made of unit work and end-to-end (E2E) work. Standard library only. Time is in model seconds; CPU is in node-CPU, held internally as integer milli node-CPU.

| Arm | Shape |
|---|---|
| A | fixed unit and E2E partitions, each an exclusive runner; unit work runs in steps (build, affected, ui) with a barrier between steps |
| B | same partitions, no barriers, longest task first |
| C | one pool; unit pods first, non-preempting; E2E worker pods backfill what unit pods leave free |

Idle CPU is split by cause (`startup`, `step_barrier`, `partition_boundary`, `account_cap`, `e2e_prereq`, `fragmentation`, `sched_latency`, `tail`, `other`). Vacuum is the idle share while some task of an arrived gate has not started yet.

## Model files

Both are JSON, passed by path. Nothing measured from a real system is committed; the tests build tiny hand-computable models in a temporary directory.

```
unit: {"runs": [{"run_id": 1, "build_s": 6, "projects": [{"step": "affected" | "ui", "test_s": 3, "cache_hit": false, "no_tests": false}]}]}
e2e:  {"runs": [{"run_id": 1, "build_s": 8, "startup_s": 3, "tests": [{"id": "t0", "start_offset_s": 2, "dur_s": 3}]}]}
```

A project with `cache_hit`, `no_tests` or a null `test_s` contributes no task. E2E tests are ordered by `(start_offset_s, id)`.

## Run

```
python -m trials.sim runs unit.json
python -m trials.sim run --arm C --unit-model unit.json --unit-run 1 --e2e-model e2e.json --e2e-run 1 --pool 8 --unit-cpu 4 --w 1 --accounts 4
```

`--offsets 0,45` adds a second gate arriving 45 s later. `--timeline-step S` adds per-step averages.
