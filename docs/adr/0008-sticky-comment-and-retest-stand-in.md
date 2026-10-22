# ADR 0008: one sticky PR comment per stack and a retest stand-in that dispatches a new run

Status: Accepted, 2026-10-23

## Context

The gate wrote its verdict only to the job summary and the verdict artifact. An infra verdict stayed `red [infra failed 1x]`, because ADR 0007 removed the in-run cloud retry. The requirements:
- one PR comment, edited in place on every run;
- an infra-only failure retried once on the cloud by the queue, as a new run whose title ends `(Retest)`;
- a second infra failure is `red [infra failed 2x]`;
- a real test failure is red, with no retry.

The real queue bot is out of bounds, so a stand-in plays its part.

## Decision

Verdict artifact:
- The gate uploads `sim-gate-verdict-<run attempt>`, so an in-place re-run never collides with attempt 1.
- A reader trusts a verdict only through `core.comment.verified_state(run, attempt)`. It requires all of the following:
  - the jobs of that attempt (`attempts/<n>/jobs`) form a complete listing, with exactly one completed `gate` job;
  - that job's `Upload verdict` step is `success`;
  - the artifact's `created_at` falls inside that step's window. The API gives whole seconds and the artifact time was measured equal to the step's end, so the end gets one second of slack.
  - the verdict's run id and attempt match;
  - the verdict agrees with the gate job's conclusion: `pass` needs `success`, a red verdict needs `failure`.
- An artifact uploaded early by a step holding the runtime token therefore fails the window check. A forged artifact of the other colour fails the conclusion check.

Comment:
- `sim-gate.yml` job `comment` runs after `gate` whenever validate resolved a `pr_number`. It downloads this run's verdict and runs `python3 -m core.comment upsert --pr N --marker sim-gate:<stack> --state verdict.json --run-url <run>/attempts/<n> --gate-result <needs.gate.result>`.
- The tool refuses, before any write, a verdict that disagrees with `--gate-result`.
- It edits the oldest comment of `github-actions[bot]` whose body starts with `<!-- sim-gate:<stack> -->`, or creates one. The body names the tested commit.
- On a retest, it reads attempt 1 of run `retest_of` through `verified_state`, so the job has `actions: read` as well as `pull-requests: write`. It keeps that run's rows for the lanes it did not rerun.
- On a retest, the headline is `pass` only when the previous run verified and every merged row is `pass`. Otherwise the headline is the red result or `red [test]`, with a note.
- A comment failure turns the run red.

Retest:
- Two signals mean "this run is a retest": `retest_of != ''` (a stand-in dispatch) or `github.run_attempt != '1'` (an in-place re-run, the real queue's model). `core.gate --retest` then turns infra into `red [infra failed 2x]`.
- `gate` exports `retest_lanes`: the infra lanes plus the lanes not run because of them. The list is empty after a cache tripwire, and empty when any lane is `test`.
- Job `requeue` (`actions: write`) runs only on the first attempt of a run that is not a retest and has retest lanes. It runs `gh workflow run sim-retest.yml --ref "$GITHUB_REF_NAME" -f run_id="$GITHUB_RUN_ID"` after checking that the ref is a branch. `workflow_run` stays forbidden.
- `sim-retest.yml` has one job (`actions: write`) under a concurrency group keyed on `run_id`. It runs `python3 -m core.retest_sim run --run-id N --workflow sim-gate.yml`, which works in this order:
  1. Polls run N for up to `--wait` (900 s). The run object's completion is advisory: the API has kept a run `in_progress` for about 45 minutes after its last job. A completed run must be `failure`.
  2. Reads that attempt's verdict through `verified_state`, waiting while the gate job is still running.
  3. Refuses on a `test` lane, a tripwire, a run that is already a retest, a moved branch tip, or an earlier `retest of N` run.
  4. Dispatches `sim-gate.yml` once, on the same branch and the same `pr_sha`, with `retest_of = N`, the original inputs, and `lanes` set to the retest lanes minus `build`.
- A refusal prints `retest_sim: no dispatch: <reason>`. An API error prints the same as a `::warning::` annotation. Both exit 0.
- `validate` binds a retest to its previous run before any lane runs. When `retest_of` is set, it runs `python3 -m core.prior check --run-id N --workflow sim-gate.yml --stack S --selected unit=<bool> --selected integration=<bool>` with `actions: read`. The check refuses unless all of the following hold:
  - N is not this run;
  - attempt 1 of N completed within 120 s, is a `sim-gate.yml` dispatch, concluded `failure`, and tested `GITHUB_SHA`;
  - its verdict verifies, is not itself a retest, and is for the same stack and commit;
  - it selected exactly `build` plus the known lanes;
  - every lane this run does not rerun was `pass`;
  - every lane it listed for retest is rerun.

  A refusal fails `validate`, so build and the lanes skip and the gate is `red [test]`. On success, `validate` outputs `prior_<lane>=pass` for each lane not rerun. The gate passes those as `--prior-lane`, and the result covers the rerun lanes and the prior lanes together. A prior lane counts only when it is `pass` and `retest_of` is set, and `build` or a rerun lane is refused as a prior lane. A forged `lanes=<passing lanes> retest_of=<any run>` is therefore red.
- A retest is routed to the cloud: both templates ask the route about `''` when `retest` is true.
- The run name carries `retest of <N> (Retest)`. The stand-in's duplicate guard matches on it.

Faults:
- `fail_health_once` fails the build's Health step unless the run is a retest.
- `fail_health_twice` fails it on every run.
- `fail_unit_health_once` fails the unit lane's Health step unless the run is a retest. The retest then reruns `unit` only and keeps the integration row of the previous run.
- Health is an infra step. Faults are replayed through `verdict.json`.

Policy:
- Permissions: the map gains `validate` (`actions: read`), `comment` (`actions: read`, `pull-requests: write`) and both `requeue` jobs (`actions: write`). `write` is allowed only where the map says so.
- Pinned whole: the comment, requeue and stand-in jobs; the previous-run check step and its outputs; the Decide step with `--prior-lane`; the entry job set and order; the stand-in's input and concurrency group; the gate output; both Health steps; and the per-attempt artifact name.
- The fault expressions are evaluated over every fault, lane and retest value, and the route call over retest and label.

## Consequences

- The stand-in, the previous-run check and the comment run the branch's own code, because the dispatch uses the branch ref. They stop wrong inputs and a forged artifact, not code edited on the branch: anyone who can push the branch can rewrite `core/`. After the template is in use, the stand-in could be dispatched with `--ref main`; the rehearsal needs the branch.
- A lagging run object delays the stand-in by up to `--wait`. On the previous-run check, the same lag past 120 s makes the retest red. A gate-job record still `in_progress` after the wait means no dispatch. Every one of these fails closed: the run stays red, never a false pass.
- A green `sim-retest` run proves nothing; the proof is the `sim-gate` run titled `retest of N (Retest)`.
- `requeue` skips a re-run attempt. An in-place re-run is a retest of itself: its own attempt's verdict is read, and the previous-run check does not apply, because `retest_of` is empty.
- `verdict.json` lists only the lanes the run ran, even though its `result` covers the prior lanes too. A later reader must verify the previous run itself, as the comment does.
- The comment trusts `pr_number`. It checks the number against the verdict and shows the commit, but it does not check that the PR's head is that commit. When `validate` fails before outputs exist, or the gate writes no verdict, the old comment stays. Two runs at once end with the last finisher's comment.
- Hosted rehearsals prove the merge of prior rows only with `fail_unit_health_once`. A lab lane that loses its runner mid-test, and is then retested alone on the cloud, is a Phase 3 rehearsal.
- The real queue retries up to twice; the stand-in retries once (R12). The difference goes on the owner's handover list.
