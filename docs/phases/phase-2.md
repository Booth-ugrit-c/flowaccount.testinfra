# Phase 2: three designs on the lab, validated replay, comparison

## Built

| Part | Files |
|---|---|
| As-is design | `backends/container/asis/`: one persistent runner per VM, registration token read from stdin, sampler installed as a service |
| Improved design | `backends/container/improved/`: several runners per VM from one pristine runner copy, systemd memory limit per runner, schedule label on the last runner, job images pulled at setup |
| Kube design | `backends/kube/`: single-node k3s, scale sets per lane and gate kind, priority classes, namespace quota above the pool, Docker-in-Docker sidecar for integration jobs behind a node-local pull-through mirror, every version and image pinned in `versions.env` |
| Lab host | `lab/pve01/`: start the lab hypervisor only when the host has the memory for it, resize it only when it is off, switch the running design with a memory check before starting it |
| Dispatch by kind | `trials/dispatch/`: `--labels lane:kind=label` routes `pr` and `schedule` gates to different labels; a kind-routed replay refuses a window without kinds |
| Per-job cleanup | `workload leftovers --job <id>` removes only that job's containers, so two runners sharing one Docker daemon do not remove each other's services; the dispatch workflow passes the job id and skips the step for jobs without services |
| Measurement tooling | merged separately: sampler, windows, validate, compare (ADR 0002) |
| Decisions | ADR 0002 (validate before compare), ADR 0003 (three designs on one budget) |

## Evidence

| Check | Result |
|---|---|
| Smoke, synthetic two-gate window (`pr`: 3 unit + 2 integration, `schedule`: 1 + 1), each design | as is: [run 39400408698](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39400408698), [run 39400412308](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39400412308), 7 of 7 jobs succeeded, first in first out; improved: [run 39400036744](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39400036744), [run 39400040410](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39400040410), 7 of 7, schedule jobs only on the last runner, two integration jobs at once on one daemon both succeeded; kube: [run 39399420644](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39399420644), [run 39399425320](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39399425320), 7 of 7, schedule jobs on the schedule scale sets, one pod per job |
| Test suite | 592 passed, 11 skipped (Linux-only and large-image tests) on Windows; Linux CI PENDING |
| shellcheck `-S warning` on `backends/`, `lab/` and the pre-push hook | 0 findings in 13 files; the same binary reports 6 findings on a deliberately broken script |
| Comparison | ran on replays of private recorded history (ADR 0002); its numbers and the calibration verdict they rest on stay outside the repository |

## Red first

| Check | Broken input | Result |
|---|---|---|
| Routing by kind | `lane:kind` lookup ignored | test fails |
| Per-job cleanup | job prefix dropped from the leftovers filter | test fails |
| Kind-routed replay without kinds | window with no `kind` field | refused before dispatch |
| Gate kind from the default branch | default-branch check removed | test fails |
| Lab host start | free host memory below the VM size plus margin | start refused, exit 2, VM stayed off |
| Design switch | available lab memory below the design's guests plus margin | switch refused before any guest started |

## Known limits

- The kube pool is one node: lab guests cannot reach each other, so a second node could not join.
- The scale-set credential can register runners for this repository and read what its token can read; it lives only as a Kubernetes secret on the lab VM for the trial and is deleted with the pool. The Docker-in-Docker sidecar is privileged on the same node.
- Persistent runners (as is, improved) keep state between jobs; a job that leaves a process or a modified tool cache behind can skew later trials. Fork pull requests need approval before any workflow runs.
- Jobs on the persistent designs reach the VM's Docker daemon, so a job is effectively root on its VM. The sampler service also runs as root from root-owned directories; it adds no privilege a job does not already have, and a trial VM holds no credential other than its runner registration.
- Deferred to a later phase: lease queue, token lease, the shared conformance suite and an adaptive design.
