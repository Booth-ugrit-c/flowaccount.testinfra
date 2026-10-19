# testinfra

Trials of self-hosted CI runner designs: each design runs the same synthetic workload on the same lab host with the same resource budget, and is measured the same way, so the designs can be compared on numbers rather than opinion.

## Designs

| Design | Backend | Shape |
|---|---|---|
| Persistent per role | `backends/container/` | one persistent machine per test role, one runner each, first in first out |
| Persistent per role, tuned | `backends/container/` | same machines; several runners per machine under cgroup limits, separate labels for pull-request and scheduled work, warm caches, right-sized split, token lease |
| Kubernetes pool | `backends/kube/` | one shared pool, a runner pod per job via Actions Runner Controller, non-preempting priority, lease queue with heartbeat and reaper |
| VM per job (later) | `backends/vm/` | a VM cloned from a template for every job |

## Layout

| Path | Holds | From phase |
|---|---|---|
| `tools/leak_guard/` | pre-push and CI scan that keeps internal data out of this public repo | 0 |
| `.github/workflows/` | mechanism CI and the trial dispatch workflow | 0 |
| `workload/` | synthetic job generator and its profile schema | 1 |
| `trials/` | dispatch driver, samplers, simulator, analysis | 1 |
| `lab/` | lab host and VM definitions | 2 |
| `backends/` | one directory per design | 2 |
| `core/` | lease queue, token lease, reaper | 4 |
| `conformance/` | correctness invariants with red-first fixtures, shared by every design | 4 |
| `docs/adr/`, `docs/phases/` | decisions and per-phase evidence | 0 |

## Where numbers come from

GitHub-hosted runners prove the mechanisms on every pull request. Performance numbers come only from trials on the lab host. The workload is synthetic: jobs burn CPU-seconds and hold memory instead of sleeping, so wall time and queue wait come from the platform under test. Calibration inputs are passed by path at run time and never committed.

## Setup

```
git config core.hooksPath .githooks
python -m pytest
```

The pre-push hook runs the leak guard and refuses the push on any finding. Contribution rules: [AGENTS.md](AGENTS.md).
