# Phase 1: workload and calibration

## Built

| Part | Files |
|---|---|
| Synthetic job generator | `workload/`: burns a requested number of CPU-seconds on worker processes, holds resident memory, starts generic service containers pinned by digest (sql, document, cache, search, queue), checks for leftovers |
| Dispatch path | `.github/workflows/trial-gate.yml` (one gate fans out to matrix jobs on labels given as input), `trials/dispatch/` (replays gates at recorded arrival offsets, collects every job's timeline, fails on an incomplete or failed gate) |
| Calibration extraction | `trials/calibration/`: reads job history of named runners through the GitHub API (read only) into a private file, with a chronological fit/holdout split and a cutoff; every specific comes from a private config passed at run time |
| Simulator | `trials/sim/`: capacity simulator ported without its original data; expected values derived by hand |
| Leak guard additions | `scan-all` (every object reachable from chosen refs), `.github/workflows/leak-guard-scheduled.yml` (daily: branches and tags with the private deny-list, pull request refs with generic rules only), rejection of deny-lists that are not UTF-8 text, 12-digit rule exception inside long hex tokens |

## Evidence

| Check | Result |
|---|---|
| Test suite | all pass; Linux-only and large-image tests run in CI or behind `WORKLOAD_DOCKER_FULL=1` |
| Live gate, 5 jobs on hosted runners | [run 39240976321](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39240976321): 5 of 5 jobs collected, integration jobs started sql, cache and queue containers, measured CPU-seconds within 0.1% of requested |
| Generator on hosted runners, 10 jobs of 30 CPU-seconds | [run 39241136749](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39241136749): measured CPU-seconds median 30.006, worst error 0.043%; wall time coefficient of variation 1.31% |
| Run id discovery | taken from the dispatch response |

## Red first

| Check | Broken input | Result |
|---|---|---|
| Generator CPU target | workers stopping at half their share | run exits 1 |
| Generator memory | allocation failure swallowed (copy outside the repo) | test fails; real code reports `ok: false` and exits 1 |
| Completeness | collected gate with one expected job id added | `INCOMPLETE ... missing=['unit-004']`, exit 1, on the live run's data |
| Failed control job or run | complete job set, `validate` failed | collect exits 1 |
| Calibration zero match | runner filter matching nothing | exits non-zero, writes no file |
| Calibration cutoff | gates straddling the cutoff | later gates were split into fit/holdout before the change |
| Simulator | 16 single-point mutants | every mutant fails at least one test |
| Scheduled scan scope | deny-listed line reachable only from a pull request ref | printed a deny-list finding before scoping; none after, while the same blob on a branch is still found |

## Known limits

- The dispatch workflow sends job specs as workflow inputs, so their values appear in public run logs.
- The validate job checks input shape; it is not an access control. Only maintainers can dispatch.
- Containers are matched by a global name prefix; several runners sharing one Docker daemon need per-job labels first.
