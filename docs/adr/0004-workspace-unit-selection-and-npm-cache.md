# ADR 0004: workspace unit selection and the npm download cache

Status: Accepted, 2026-10-22; amended 2026-10-23 when the lane was wired (section "Wiring the lane")

## Context

The gate needs a second stack beside the dotnet sim: a JavaScript monorepo whose unit tests run through Nx. `sim/workspace/` is a synthetic npm-workspaces repository of five projects, inferred by Nx from each `package.json`, with jest tests whose expected values come from the source through `oracle.js`. `adapters/workspace/` holds the selection (`affected.py`), the plan (`plan.py`) and the cache client (`cache.py`) that a later workflow will call, with the same command-line contract as the dotnet adapter.

Three facts shaped the design:
- `nx show projects --affected` maps a changed file that no project owns to no project, so a change to `nx.json` or the lockfile, or a markdown-only change, can select nothing and pass with no test run.
- Nx can replay a cached test result, which is a test that did not run.
- A restored `node_modules` is executable code that nothing re-verifies; the npm download cache is checked by `npm ci` against the lockfile's integrity hashes.

## Decision

Selection reads the project graph from `nx graph --file=` at the head checkout and maps `git diff base..head` onto project roots plus every dependent. It selects every test project when it cannot vouch for a change: a file under the workspace owned by no project (except `*.md`), a non-`*.md` file in a directory above the workspace, or a selection with no test project. A project rooted at the workspace root owns no file.

A graph error exits 2 and the run goes red; it does not fall back to all projects. Without a graph there is no list to call "all", and red cannot be a false green. Exit 2 covers: the checkout is not `--head` or has local changes; `nx graph` fails or returns a malformed graph; a workspace package has a `test` script but no `test` target in the graph. The consuming workflow must not soften that exit with `|| true`, `continue-on-error`, or by treating the step as infrastructure.

The plan keeps the dotnet contract (`--affected`, `--lane`, `--emit build|test`, one path per line, exit 2 on error), adds `--names` for `nx run-many --projects`, accepts only the `unit` lane, and refuses an empty test list.

The cache stores only the npm download cache (`_cacache` under `npm ci --cache DIR`), keyed on the lockfile, the node and npm major versions and the platform. The client refuses a lockfile entry without a sha512 integrity or with a `resolved` outside `https://registry.npmjs.org/`, so a bad store object cannot change what `npm ci` installs. Neither `node_modules` nor the Nx task cache is stored. `nx.json` sets `test.cache: false`, pinned by a test, and runs also pass `--skip-nx-cache`. The consuming workflow must only restore in pull-request runs. `save` uploads only on a GitHub-hosted runner, at the default-branch tip, from a clean tree, with the writer key. That check is a second line of defence, because a step in the same job can set the runner environment; the boundary is that the writer key must exist only in the default-branch environment.

The committed `package-lock.json` had its single `deprecated` field removed by hand, because the field text carried an email address that the leak guard refuses. The field is metadata: integrity hashes are unchanged and `npm ci` neither reads nor rewrites it.

## Consequences

- A change the selection cannot attribute costs a full run, never an empty one; a broken graph costs a red run that someone must read.
- Store objects reach the runner only through `npm ci` integrity checks; the archive client also bounds size and member count and refuses links and paths outside `_cacache`.
- A later `npm install` brings the `deprecated` field back; the leak guard refuses the push until it is removed again.
- Residual: removing a project's `test` script, or adding `jest --passWithNoTests`, drops that project's tests with no signal, and the selection cannot tell an intentional removal from an accident. The wired lane closes the empty-project case with a per-project zero-tests guard (see below); a project set that shrinks against base is left to a scheduled full run, because failing the pull request would also block an intentional removal.
- The cache is exercised here only against a fake store; no rehearsal so far has had store secrets, so the restore hit path and the save on a hosted runner are still unproven.

## Wiring the lane

`tpl-workspace-unit.yml` runs the lane, and `sim-gate.yml` calls it as the `workspace` job when `stack` is `workspace`. That stack runs only validate, the workspace job, the gate, the comment and requeue. The dotnet build and lanes are skipped by `if: inputs.stack == 'dotnet'` on the build job. Validate knows only the `unit` lane for this stack, so a retest that names `integration` is refused before any runner starts. The gate passes the lane as `unit=<verdict>`, and the comment carries its own marker `sim-gate:workspace`.

The `run` job follows the dotnet lane pattern:
- it routes through `tpl-route.yml` on `vars.SIM_LAB_LABEL_WORKSPACE`;
- on lab runners it runs hygiene from the trusted tree, bound to the dispatch commit, with `SIM_SURVIVORS=node`;
- it pins `setup-node` by commit, reads the version from `.nvmrc` and turns the action's own cache off;
- it runs tripwire, restore, `npm ci --ignore-scripts` with the download cache, affected, plan, `test_lane.sh` and the zero-tests guard.

The workflow policy pins the job whole. It refuses `|| true`, a following command, `if` or `continue-on-error` on Affected, so a graph error (exit 2) is red. Classify names only `Set up Node` as an infra step, so a failure in Affected, Install or Test is the change's. That includes a registry outage during `npm ci`: lockfile mistakes are the common cause, and a requeue would hide them.

The cache save runs in its own `save` job. That job runs only on a GitHub-hosted runner at the default-branch tip with no fault, in `sim-cache-write`, and does checkout, Node, the tailnet join, the tripwire, `npm ci --ignore-scripts` and the save, nothing else. The `run` job uses only `sim-cache-read` or `sim-cache-lab`. The reason is that a later step in the same job can read a secret scoped to an earlier step from the runner process, so ordering is not a boundary. In the run job, nx and jest from the lockfile run after the cache steps. The policy refuses the writer key in any other job, and refuses nx, jest, npx or the test lane in the job that holds it. The npm download cache depends only on the lockfile, so a fresh install in the save job stores what the lane used.

On the default-branch tip the plan passes `--all`, under the same condition as the dotnet full build. That full run is the detection signal for every selection gap listed below.

Selection must-fixes from the first security review:
- `affected.py` scans every tracked file under the workspace. In JS and TS files it reads `require`, `require.resolve`, `import`, `export ... from`, dynamic `import()` and the `jest` module calls. In JSON files (the lockfile excepted) it reads relative path strings, with `<rootDir>/` stripped. Any of these selects every project:
  - a reference that resolves into another project with no graph path from the owner;
  - a reference outside the workspace, or an absolute path;
  - a project package name not in the owner's dependency closure;
  - a non-project file that references a project;
  - a non-literal `require`/`import`, or `createRequire`;
  - unreadable JSON;
  - a tracked symlink.

  It scans all projects, not only the changed ones and their dependents: an unselected project's hidden reference to a changed project is visible only in the unselected project's own files.
- `sim/workspace/.npmrc` sets `ignore-scripts=true`. The jobs also point the npm user and global config at two paths under `/dev/null/` that cannot exist (npm refuses `/dev/null` itself for both: it loads one file twice), so a persistent lab runner's `~/.npmrc` cannot change an install or the `npm run` behind each test.
- `npm audit` reported one high (undici 7.29.0 under nx). nx 23.3.0 and 24.0.0 both pin that exact version, so `package.json` overrides undici to 7.30.1 and the high clears (`npm audit --audit-level=high` exit 1 before, 0 after). The `ci.yml` `workspace` job installs with `--ignore-scripts`, runs `nx run-many -t test --skip-nx-cache`, and runs `npm audit --audit-level=high --package-lock-only` as its own step.
- The zero-tests guard is per project. `jest_counts.py` writes `numPassedTests + numFailedTests` per project, and `core.zero_tests` turns an all-skipped or report-less project red.

Consequences and residuals of the wiring:
- `npm audit` changes over time: a new advisory turns `main` and unrelated pull requests red with no code change. That is the intended signal, and the named step shows its cause.
- The scan does not see file reads (`fs.readFileSync(path.join(__dirname, "../../x"))`), an aliased `require`, `eval` or computed names. It guards against mistakes, not against the pull-request author, who can edit their own tests anyway. The main-tip full run catches what it misses.
- In the run job the tailnet stays joined after the cache steps, and `id-token: write` is job-scoped, so test code can still mint an OIDC token for the read environment, as in the dotnet build job. The trust policies that accept that subject belong on the owner setup list. Leaving the tailnet before Affected would not close this path.
- On a lab runner the `setup-node` tool cache persists between jobs, as `setup-dotnet` already does.
- A passing lane with no executed count still passes the gate (GATE-PASS-WITHOUT-COUNT). The fix in `core/gate.py` is planned for PR 9 for every stack. In this lane a pass needs the guard step to succeed, and that step writes the count.
