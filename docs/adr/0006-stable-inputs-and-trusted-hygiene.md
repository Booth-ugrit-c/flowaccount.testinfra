# ADR 0006: take the four pull request inputs at the entry and run lab hygiene from the trusted tree

Status: Accepted, 2026-10-22

## Context

A queue bot starts the gate with `workflow_dispatch` on the pull request's head branch and passes `pr_number`, `pr_branch`, `pr_sha` and `pr_author`, dropping any input the workflow does not declare. The entry took a free `head` instead, two lab-fault inputs, and no way to name lanes, turn the cache off or mark a retest. A retest that is a new dispatch needs a visible marker, and a retest stand-in needs something to dispatch before it exists.

Lab hygiene (`core/prejob.sh`) ran from the head checkout, so the change under test chose what cleaned the self-hosted host before it. The tailnet join step had no step timeout, so a hanging join could spend about ten minutes of the attempt budget before the cold build started. Classify named the dotnet setup step as infra inside core, and the prejob survivor list was dotnet process names inside core.

## Decision

Entry contract (`sim-gate.yml`, exactly ten dispatch inputs, all optional):
- `pr_number`, `pr_branch` (default `main`), `pr_sha`, `pr_author`, `base` (default `origin/main`), `stack` (choice: `dotnet`), `lanes` (comma list, empty = every lane), `cache` (choice: `use`, `off`), `fault` (choice: `none`, `fail_test`, `kill_onprem`, `kill_both`; later faults are added by the change that implements them), `retest_of`.
- `validate` runs `core/resolve_refs.sh` on all of them and is the only job that reads the raw values. Head = `pr_sha` when given, else the tip of `pr_branch`. With both given, `pr_sha` must be an ancestor of `origin/<pr_branch>`. When the dispatch ref is the pull request branch itself (`GITHUB_REF` = `refs/heads/<pr_branch>`), the tested commit must also equal `GITHUB_SHA`: the `gate` check is posted on the dispatched commit, so an older `pr_sha`, or a branch tip that moved after the dispatch, would put a green check on a commit no lane tested. A dispatch from any other ref keeps the ancestor rule alone; its check lands on that other ref's commit, never on the pull request head, so it cannot vouch for code it did not run. `base` must be a commit on the default branch, so a feature-branch commit cannot be its own base and select nothing. `lanes` is refused unless `retest_of` is set: a first run tests every lane, and only a retest may narrow to the lanes that failed on infrastructure. `pr_sha` is 40 lowercase hex or empty; `pr_number` is up to 10 digits or empty; `pr_author` is a login of up to 39 letters, digits or dashes, optionally followed by `[bot]`, or empty; `retest_of` is digits or empty; every lane named in `lanes` must be in the workflow's `KNOWN_LANES`. A value containing a newline is refused before it can reach `GITHUB_OUTPUT`.
- Outputs: `pr_sha` (resolved), `base_sha`, `default_tip`, `pr_number`, `pr_branch`, `pr_author`, `retest_of`, and `lane_<name>=true|false` per known lane.
- Lane jobs run only when `validate` selects them. The gate needs `validate` and every lane; for each lane it passes `--lane` when selected, writes `lane <name>: not selected` to the summary when not, and exits red on any other value, so a failed `validate` can never yield a green gate.
- `run-name` is one expression. It names the lanes (` lanes <list>`) whenever `lanes` is set, so a partial run is visible in the title, and it ends ` retest of <id> (Retest)` exactly when `retest_of` is set; the retest stand-in should match that whole suffix, since `retest of 4` is also a prefix of `retest of 42`. The title is rendered from the raw input before `validate` runs, so it is display, not data: a crafted value can only show a retest marker on a run that `validate` then fails. Code that must know whether a run is a retest reads the validated `retest_of` output; the gate's retest signal, wired with the retest stand-in, also counts `github.run_attempt`.
- `cache: off` reaches the Build and Save steps as `SIM_CACHE_MODE`.
- `sim-retest.yml` lands as a stub: `workflow_dispatch` with `run_id`, `contents: read`, a concurrency group per `run_id`, one job that prints that it is not wired. A dispatchable workflow has to exist on the default branch before a later change can wire it.

Trusted hygiene:
- On self-hosted runners only, after the head checkout, "Checkout trusted" checks out the dispatch commit (no `ref`) into `.sim-trusted` with `sparse-checkout: core` and `persist-credentials: false`.
- Hygiene refuses unless `.sim-trusted` is a real directory with its own `.git` whose `HEAD` equals `GITHUB_SHA`, then runs `sh .sim-trusted/core/prejob.sh` with `SIM_SURVIVORS` from the adapter, and removes `.sim-trusted` whatever the result.
- `prejob.sh` takes the survivor names from `SIM_SURVIVORS` and fails before any cleanup when the list is missing, blank or outside `A-Za-z0-9._-` and spaces.

Classify:
- `core.classify` treats `Set up job`, `Checkout trusted`, `Hygiene`, `Health` and `Services` as infra steps (the trusted checkout is part of the neutral hygiene, so a failed fetch on a lab runner is `infra`, not `test`); an adapter adds its setup step with a repeatable `--infra-step NAME`. The dotnet attempt passes `--infra-step "Set up .NET"`. A name outside `A-Za-z0-9 ._-`, or `Test` itself, is ignored with a message.

Tailnet join: the join step has `timeout-minutes: 3`. GitHub documents that a step under `continue-on-error` lets the job continue when the step fails; the hosted rehearsal shows that for a failed join, not yet for a timed-out one.

Policy (`core/tests/workflow_policy.py`, every rule shown red on a mutated copy of the shipped workflows):
- Loaded set `sim-gate*.yml`, `sim-retest.yml`, `tpl-*.yml`; the expected set has no `tpl-*` file yet, so one fails the policy until the change that adds it also adds it to the expected set; `sim-gate.yml` and `sim-retest.yml` are `workflow_dispatch` only; `workflow_run`, `pull_request_target` and `issue_comment` stay forbidden.
- Every job's `permissions` equals the permission map, or is absent and inherits `contents: read`.
- The ten-input contract, the fault-to-lane mapping for every fault option, the run-name with and without `lanes` and `retest_of`, and one lane set across lane jobs, `KNOWN_LANES`, `validate` outputs and the gate.
- Exact `run` text for the Classify step (with its env), the Build, Cache tripwire and Save steps, the Hygiene step and the gate's Decide step. A substring check let an appended `--run-result success` or an extra command pass.
- Every file in `.github/workflows` must be a known workflow the policy can parse. Anywhere in that directory: no YAML escape sequences, no `toJSON(secrets)` or `secrets[...]`, no `secrets: inherit`, no expression inside `run`, an `environment` only where the allowlist names it, and the cache writer key or environment named only in the attempt's environment expression and its save step.
- Every `python -m` (flags before `-m` included, as in `python3 -I -m`), `python FILE.py`, `sh`/`bash`/`.` script, `./script.sh` and local `uses:` in any workflow resolves to a file at the head, and the scan must see the known core and dotnet entry points.

## Consequences

- The hosted rehearsal never runs the trusted checkout or Hygiene; they are proven by the policy and by tests that run the shipped Hygiene script against a local repository, and first run for real on a lab runner.
- A head without `core/` no longer fails Hygiene: the trusted copy comes from the dispatch commit. A dispatch from a branch still runs that branch's workflow text, so trusted hygiene gives parity with the workflow's own trust, not protection from the branch author.
- In-workflow hygiene cannot clean state that a previous job left inside the workspace `.git` (config, hooks, replace refs), because the head checkout reuses it before Hygiene runs, nor state in the runner user's home (a global git config with a hooks path, the tool cache, `PATH`), which can act during either checkout without moving `HEAD`, nor a process that survived an earlier job and edits `.sim-trusted` between the checkout and `sh`: the `HEAD` check proves the commit, not the files on disk. The control for all three is the runner's job-started hook or an ephemeral runner, not this workflow.
- A retest with `lanes` gives a partial verdict under the same `gate` check name; the title and the step summary say which lanes ran, and the retest stand-in is the only intended caller.
- Branch pushers: every guard in these files runs from the dispatcher's own branch, which can edit them, including the `environment:` expression; expression string compares are case-insensitive. The deployment-branch rule on `sim-cache-write` (selected branches, exactly `main`, no tag rule, created before any secret) is the only control against a branch pusher reaching the writer key.
- Read tier: the read tier trusts every branch of the repository, including branches a dependency bot or a merge queue creates. Refusing those prefixes in `resolve_refs.sh` is optional template work.
- The policy parser accepts some YAML GitHub rejects (a plain scalar containing `: `); `actionlint` in CI covers that.
