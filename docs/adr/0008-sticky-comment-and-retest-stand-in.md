# ADR 0008: one sticky PR comment per stack and a retest stand-in that dispatches a new run

Status: Accepted, 2026-10-23

## Context

The gate wrote its verdict only to the job summary and the `sim-gate-verdict` artifact. An infra verdict stayed `red [infra failed 1x]`, because ADR 0007 removed the in-run cloud retry. The requirements are one PR comment edited in place on every run, and an infra-only failure retried once on the cloud by the queue, as a new run whose title ends `(Retest)`. A second infra failure is `red [infra failed 2x]`. A real test failure is red with no retry. The real queue bot is out of bounds, so a stand-in plays its part.

## Decision

Comment:
- `sim-gate.yml` job `comment` runs after `gate` whenever validate resolved a `pr_number`. It downloads this run's `sim-gate-verdict` and runs `python3 -m core.comment upsert --pr N --marker sim-gate:<stack> --state verdict.json --run-url <run>/attempts/<n>`.
- The tool edits the oldest comment of `github-actions[bot]` whose body starts with `<!-- sim-gate:<stack> -->`, or creates one. On a retest it reads the rows of the lanes not re-run from run `retest_of`'s artifact, so the job has `actions: read` as well as `pull-requests: write`.
- A comment failure turns the run red. A loud failure is preferred over a silent one, and the stand-in then sees a failed run, but the verdict lanes still decide whether it dispatches.

Retest:
- Two signals mean "this run is a retest": `retest_of != ''` (a stand-in dispatch) or `github.run_attempt != '1'` (an in-place re-run, the real queue's model). `core.gate --retest` then turns infra into `red [infra failed 2x]`.
- `gate` exports `retest_lanes`, which lists the infra lanes and the lanes not run because of them. The list is empty after a cache tripwire, and empty when any lane is `test`, so a mixed test and infra failure never requeues.
- Job `requeue` (`actions: write`) runs only on the first attempt of a run that is not a retest and has retest lanes. It runs `gh workflow run sim-retest.yml --ref "$GITHUB_REF_NAME" -f run_id="$GITHUB_RUN_ID"` after checking that the ref is a branch. `workflow_run` stays forbidden, and it would fire only from the default branch anyway. A dispatch made with the run token does create a run.
- `sim-retest.yml` has one job (`actions: write`) under a concurrency group keyed on `run_id`. It runs `python3 -m core.retest_sim run --run-id N --workflow sim-gate.yml`. The tool polls run N (see below) and checks the verdict. It refuses on a `test` lane, a tripwire, a run that is already a retest, a moved branch tip, or an earlier `retest of N` run. It then dispatches `sim-gate.yml` once on the same branch, for the same `pr_sha`, with `retest_of = N`, the original inputs replayed, and `lanes` = the retest lanes minus `build`. The build runs whenever any lane does, and `resolve_refs` knows no `build` lane. If nothing is left, there is no dispatch. Every refusal or API error prints `retest_sim: no dispatch: <reason>` and exits 0.
- Both readers check the artifact against the gate job's own conclusion, so an artifact pre-uploaded by a step holding the runtime token cannot pass for the gate's.
  - The comment job passes `needs.gate.result`; a `pass` verdict needs `success`, any red verdict needs `failure`, and anything else is refused before any write.
  - For the previous run's rows and for the stand-in's decision, the conclusion comes from the jobs API (`filter=latest`, a complete listing, exactly one completed `gate` job). The stand-in waits while that job is still running. A mismatch or a missing gate job means no prior rows (with a note) and no dispatch.
- The run object's completion is advisory. The API has kept a run `in_progress` for about 45 minutes after its last job finished. The stand-in polls the run for up to `--wait` (900 s); if the run completed, its conclusion must be `failure`. If it still says `in_progress` at the deadline, the stand-in goes on: the run's identity fields are present on an in-progress run, and the gate job, which `requeue` needs and which is therefore already finished, is the hard check. A passed-in gate result was considered and dropped, because the dispatcher supplies it and it would add nothing to the jobs-API check.
- A retest is routed to the cloud. Both templates ask the route about `''` instead of their label when `retest` is true, and the route answers `cloud` for an empty label.
- The run name carries `retest of <N> (Retest)`. The stand-in's duplicate guard matches on it.

Faults: `fail_health_once` fails the build's Health step unless the run is a retest, and `fail_health_twice` fails it always. Health is an infra step, so the build verdict is `infra` and every lane is `none`. The fault is replayed through `verdict.json`.

Policy: the permission map gains `comment` (`actions: read`, `pull-requests: write`) and both `requeue` jobs (`actions: write`), and `write` is allowed only where the map says so. The comment, requeue and stand-in jobs are pinned whole, as are the entry job set and order, the stand-in's input and concurrency group, the gate's `retest_lanes` output and the Health step. The Health fault is evaluated over every fault and retest value, and the route call over retest and label.

## Consequences

- The stand-in and the comment run the branch's own code, because the dispatch uses the branch ref. Their checks are correctness guards, not security controls: anyone who can push the branch can rewrite them, and anyone who can dispatch already holds `actions: write`. After the template is in use, the stand-in could be dispatched with `--ref main`; the rehearsal needs the branch.
- A lagging run object delays a retest by up to `--wait` instead of skipping it. A gate job whose jobs-API record is still `in_progress` after `--wait` (a lag seen on other jobs during build-once) means no dispatch: the run stays red, never a false pass.
- The stand-in exits 0 on every refusal. A green `sim-retest` run proves nothing; the proof is the `sim-gate` run titled `retest of N (Retest)`.
- `requeue` skips a re-run attempt. If the dispatch step itself fails, re-running the failed jobs does not requeue (it would be a retest and the stand-in would refuse it); dispatch a new run instead.
- The comment trusts `pr_number`: it checks the number against the verdict, but it does not check that the PR's head is `pr_branch`. A mistyped number comments on the wrong PR of this repository.
- When the gate refuses to write `verdict.json`, the comment job fails at the download, and the existing comment still shows the previous run's result. Two gate runs on one PR at once both edit the comment; the last one to finish wins, even if it tested an older commit.
- The real queue retries up to twice; the stand-in retries once (R12). The difference goes on the owner's handover list.
