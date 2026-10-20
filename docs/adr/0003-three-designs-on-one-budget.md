# ADR 0003: three runner designs on one compute budget

Status: Accepted, 2026-10-20

## Context

ADR 0001 names the designs; this ADR fixes what each one is when trialled, so that a difference in the comparison comes from the design and not from extra hardware. Every design gets the same budget: 8 vCPU and 15 GiB across its hosts, on the same lab hypervisor, with only one design's hosts running at a time.

Jobs come in two lanes (unit, integration) and two gate kinds (`pr`, `schedule`, see ADR 0002). A pull-request gate has someone waiting on it; a scheduled gate does not.

## Decision

| | as is | improved | kube |
|---|---|---|---|
| Hosts | one VM per lane, 4 vCPU each | one VM per lane, 4 vCPU each | one VM, 8 vCPU, single-node k3s |
| Runners | one persistent runner per VM | two persistent runners per VM | one ephemeral runner pod per job, scale sets from zero to four per lane and kind |
| Order | first in, first out per lane | `schedule` gates confined to the last runner on each VM; `pr` gates may use every runner | `pr` pods outrank `schedule` pods in the scheduler queue (priority 1000 against 100, no preemption) |
| Memory isolation | none | systemd `MemoryMax` per runner = (VM memory - 1.5 GiB reserve) / runners, swap off; service containers on the host Docker daemon sit outside it | pod requests and limits; kubelet reserves 300m CPU and 768 MiB for itself and the system |
| CPU isolation | none | none: a CPU quota would cap a lone job at a fraction of the VM | requests only, no CPU limit |
| Images | pulled on first use, then cached on the VM | job images pulled when the VM is set up | runner image built locally; integration jobs pull through a node-local registry mirror, warmed before the trial |
| Integration services | host Docker | host Docker | Docker-in-Docker sidecar per pod |

The improved design is measured as one bundle (runner count, memory limits, schedule lane and pre-pulled images together); the trial answers whether the bundle helps, not which part does.

The kube pool runs on one node. Lab guests cannot reach each other, so a second node could not join; a two-node pool is a later change, not part of this comparison.

The namespace quota is 16 CPU and 32 GiB of requests. A full pool is four scale sets of up to four runners: eight unit pods at 1.5 CPU and 1.5 GiB and eight integration pods at 1.75 CPU and 3 GiB with their Docker-in-Docker sidecar, 26 CPU and 36 GiB in all. So once roughly nine to ten runner pods are admitted, the API refuses further runner pods at admission instead of leaving them pending, while the node itself runs only four to five such pods at once. The trial ran with these values; raising the quota above the full pool is a later change.

## Consequences

- The as-is design models the reference setup: one job per machine at a time, first in first out. It is the baseline for every delta.
- The improved design keeps persistent runners and their warm caches; a runner that exhausts its memory limit is killed by systemd without taking the other runner down. A job's service containers run under the Docker daemon, outside that limit, and can still exhaust the VM; the kube design bounds them through the Docker-in-Docker sidecar limit.
- The kube design trades warm runner state for per-job isolation and request-based packing; its Docker-in-Docker sidecar is privileged and shares the node with the scale-set listener, which holds a registration credential. That credential is created for the trial and deleted with the pool.
- Scheduling priority without preemption only reorders waiting pods; a running scheduled job is never evicted for a pull-request job.
