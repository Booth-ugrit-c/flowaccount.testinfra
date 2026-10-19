# ADR 0001: one repository, one backend directory per design, shared core

Status: Accepted, 2026-10-19

## Context

The repository trials several self-hosted runner designs and compares them. A comparison is only fair when every design runs the same workload through the same dispatch driver and is judged by the same correctness checks and metrics. The designs differ in how jobs reach compute: a persistent machine per role, a Kubernetes pool with a pod per job, a VM per job.

Two layout constraints were measured or documented before choosing:
- Dot directories are skipped by glob-based tooling: bash `globstar` and Python `glob` were measured skipping a `.root/` directory while `find` listed it. A layout under `.root/` would hide files from any check that walks by glob.
- GitHub reads workflows only from `.github/workflows/`: "Subdirectories of the `workflows` directory are not supported." Per-design workflows cannot live in per-design folders.

## Decision

One repository. Per-design code lives in `backends/<design>/`; everything the designs share sits beside it and is used unchanged by all of them.

| Directory | Role |
|---|---|
| `backends/container/` | persistent machine per role, as is and tuned |
| `backends/kube/` | Kubernetes pool, runner pod per job |
| `backends/vm/` | VM cloned per job (after the other three) |
| `core/` | lease queue, token lease, reaper |
| `workload/` | synthetic job generator, same binary on every design |
| `trials/` | dispatch driver, samplers, simulator, analysis |
| `conformance/` | one invariant suite every design must pass |
| `lab/` | lab host and VM definitions |
| `.github/workflows/` | all workflows, named per purpose rather than per design |

Checks that walk the tree enumerate `git ls-files`, never a glob.

## Consequences

- A new design is a new `backends/` directory plus labels; it inherits the workload, driver, invariants and metrics without copying them.
- Correctness invariants are pass/fail for every design; efficiency metrics are compared, not gated.
- Workflow files carry the design as an input or a label, not a directory.
