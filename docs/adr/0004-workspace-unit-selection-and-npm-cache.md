# ADR 0004: workspace unit selection and the npm download cache

Status: Accepted, 2026-10-22

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

The cache stores only the npm download cache (`_cacache` under `npm ci --cache DIR`), keyed on the lockfile, the node and npm major versions and the platform. The client refuses a lockfile entry without a sha512 integrity or an https `resolved`, so a bad store object cannot change what `npm ci` installs. Neither `node_modules` nor the Nx task cache is stored. `nx.json` sets `test.cache: false`, pinned by a test, and runs also pass `--skip-nx-cache`. The consuming workflow must only restore in pull-request runs. `save` uploads only on a GitHub-hosted runner, at the default-branch tip, from a clean tree, with the writer key. That check is a second line of defence, because a step in the same job can set the runner environment; the boundary is that the writer key must exist only in the default-branch environment.

The committed `package-lock.json` had its single `deprecated` field removed by hand, because the field text carried an email address that the leak guard refuses. The field is metadata: integrity hashes are unchanged and `npm ci` neither reads nor rewrites it.

## Consequences

- A change the selection cannot attribute costs a full run, never an empty one; a broken graph costs a red run that someone must read.
- Store objects reach the runner only through `npm ci` integrity checks; the archive client also bounds size and member count and refuses links and paths outside `_cacache`.
- A later `npm install` brings the `deprecated` field back; the leak guard refuses the push until it is removed again.
- Residual: removing a project's `test` script, or adding `jest --passWithNoTests`, drops that project's tests with no signal, and the selection cannot tell an intentional removal from an accident. A zero-tests guard on the jest results (`numTotalTests == 0` is red) closes the empty-project case when the lane is wired; a project set that shrinks against base is left to a scheduled full run, because failing the pull request would also block an intentional removal.
- The cache is exercised here only against a fake store; the restore hit path on a hosted runner is proven when the lane is wired.
