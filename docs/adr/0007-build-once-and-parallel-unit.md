# ADR 0007: build dotnet once per run and test every lane from that build

Status: Accepted, 2026-10-23

## Context

Each lane ran one attempt job that planned, restored and built its own closure, then tested it. A run built the shared projects once per lane, the unit lane tested its projects one after another, and the reader key sat in the env of the step that ran the build of the pull request and its package targets. An on-prem infra failure was retried on the cloud inside the run, so the gate took two verdicts per lane.

## Decision

Layout:
- `sim-gate.yml` runs `validate`, then one `build` job (`tpl-dotnet-build.yml`), then one job per selected lane (`tpl-dotnet-lane.yml`), then `gate`. Exactly one job may call the build template.
- `tpl-route.yml` holds the route token and returns `target`. Both templates call it with their own `runner_label`; the caller computes `runs-on` as the label when the target is `onprem` and the label is set, else `ubuntu-24.04`, so a skipped or failed route falls back to the cloud. The build uses `vars.SIM_LAB_LABEL_BUILD`, a label both dotnet lab VMs carry; the lanes keep `SIM_LAB_LABEL_UNIT` and `SIM_LAB_LABEL_INTEGRATION`.
- There is no in-run cloud retry. `core.gate` takes one verdict per lane; a retest is a new dispatch, wired by the retest stand-in change. Until it lands an on-prem infra failure stays `red [infra failed 1x]`: red, never a false pass.

Build job:
- Plans every lane at once (`plan --lane all --emit build`, plus one test list per lane), joins the tailnet, runs the tripwire, then `Restore cache` (`cache_build.sh restore PLAN HITS`, reader key in its env), then `Build` (`cache_build.sh build PLAN HITS BUILT`, no secret in its env; it never asks the store and builds the misses with `-v:normal`), then `Pack` (`build_artifact.sh pack`, a tar inside the artifact so modes and mtimes survive), `Upload build` (`dotnet-build-<run attempt>`, one day), the writer-tier save on the default-branch tip, and `Built`, which touches the `build` marker.
- Its classify passes `--done-marker build`: a build job never writes `test-end`, so it is done at `build`, which is touched after the upload. `Upload build` is an infra step; a failed save after the marker is `test`, as it was after `test-end`.
- The writer environment needs the default-branch tip, a dispatch from the default branch, a cloud runner and no fault at all; the plan covers every project under the same condition.

Lane job:
- Checks out the same `pr_sha`, runs the trusted hygiene on lab runners (it wipes `bin` and `obj`, so it comes first), downloads `inputs.artifact` of this run into `runner.temp`, and unpacks it. Unpack is the first step that runs anything from the head tree; a refused unpack is a plain step failure and classifies as `test`. `Download build` is an infra step.
- `test_lane.sh` runs `dotnet test --no-build -v:normal` per project, unit with one project per processor, integration one at a time (per-project databases are Phase 4). The zero-tests guard runs in the same job with no `if` and no `continue-on-error`, counts executed tests (`Counters/@executed`) per listed project, prints `executed=<n> <project>`, and its sum becomes the lane output `executed`.
- A lane job has no environment, no secret and no `id-token`.

Gate:
- `--lane NAME=VERDICT` once per lane, `--reason`, `--executed`, `--retest`, `--verdict-file`. When the `build` lane did not pass, a lane with an empty verdict is `none` (not run). A build infra failure is `red [infra failed 1x]` (2x on a retest) with every lane in `retest_lanes`, the build included, as the verdict schema says; the stand-in drops `build` before it dispatches, because the build runs whenever any lane does. An empty lane after a passing build is `red [test]`.
- It writes `verdict.json` schema 1 and the `retest_lanes` output; every field is checked against the schema's rules first, and a refused state is `red [test]` with no file. The gate job uploads it as `sim-gate-verdict`.

Policy (`core/tests/workflow_policy.py`): the five sim workflows and their permission map; "lane does not build" (no build, plan, affected, cache or pack step, no `cache_build.sh`, no `dotnet` but `--version`, every `dotnet test` with `--no-build`, the download of this run's artifact only); whole-step pins for the steps that matter, exact step order, the classify jobs and the outputs pinned, Hygiene directly after the trusted checkout, and `shell`, `working-directory` and `defaults` refused.

## Consequences

- The artifact checks in `build_artifact.sh unpack` and the zero-tests guard are correctness checks, not security controls. Both run from the head tree in a job that then runs the head's tests as the same user, so a hostile change can rewrite them or write any count file. They catch accidents: a stray symlink, a member outside `bin` and `obj`, a project that ran no test. The lane holds nothing worth stealing, which is why that is acceptable.
- Moving the reader key out of `Build` protects against a package that runs code at build time, not against the change author: the tripwire and restore steps still run the head's cache client with the reader key, as before. Two residuals stay with the build job: the job has `id-token: write`, so any step, `Build` included, can request an OIDC token, and the tailnet is still joined while `Build` runs; a step can also write `GITHUB_ENV` for the steps after it, including the save step that holds the writer key. On the writer tier the head is the default-branch tip, so only a dependency reaches that path.
- A lane on a different host than the build relies on the same workspace path and an SDK that matches `global.json`; the hosted rehearsal proves it for cloud runners only. Lab runners are Phase 3.
- The build plans every lane even when `lanes` names one; a retest of one lane rebuilds the whole affected set.
- A test project that disappears from the selection between base and head is not flagged; the Phase 4 nightly catches it.
