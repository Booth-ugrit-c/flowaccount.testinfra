# Synthetic Nx workspace

Five npm-workspace packages with jest unit tests, inferred as Nx projects from their `package.json`:

| Project | Depends on |
|---|---|
| `libs/shared` | nothing |
| `libs/alpha` | `libs/shared` |
| `libs/beta` | `libs/shared` |
| `apps/gamma` | `libs/alpha`, `libs/beta` |
| `libs/solo` | nothing |

Each test derives its expected value from the source literals through `oracle.js`, so a change to one project's literal keeps every test green, and a failing test means the code broke.

```
npm ci --ignore-scripts
npx nx run-many -t test --skip-nx-cache
```

Node 22 or later. Set `NX_DAEMON=false` and `NX_NO_CLOUD=true` in CI.

## Selection: `adapters/workspace/affected.py`

Reads the project graph from `nx graph --file=` at the head checkout, then maps `git diff base..head` onto project roots and adds every dependent. It selects every test project when it cannot vouch for the change:

- a file under the workspace owned by no project (`nx.json`, `package.json`, `package-lock.json`, `oracle.js`, any unknown path), except `*.md`
- a non-`*.md` file in a directory above the workspace
- a selection with no project that has a `test` target, so the list is never empty

It exits 2, and the run goes red, when the checkout is not `--head` or has local changes, when `nx graph` fails, or when the graph is malformed or misses a workspace that has a `test` script. Without a graph there is no list to call "all".

`nx show projects --affected` is not used: Nx maps a changed file that no project owns to no project, so a root-level change could select nothing.

`adapters/workspace/plan.py` keeps the dotnet plan contract (`--affected`, `--lane`, `--emit build|test`, one path per line) and adds `--names` for `nx run-many --projects`.

## Cache: `adapters/workspace/cache.py`

What is cached is the npm download cache (`_cacache` under `npm ci --cache DIR`). The key covers the lockfile, the node and npm major versions and the platform.

- Not `node_modules`. Restored `node_modules` is executable code trusted as-is. With the download cache, `npm ci` checks every tarball against the lockfile's sha512 integrity, and the client refuses a lockfile entry that lacks one. A bad store entry therefore cannot change what is installed.
- Not the Nx task cache. A replayed result is a test that did not run, and affected projects miss the cache anyway. Tests run with `--skip-nx-cache`.

Pull-request runs only restore. `save` uploads only on a GitHub-hosted runner, at the tip of `origin/$SIM_DEFAULT_BRANCH`, from a clean tree, with the writer key. The code check is a second line of defence: the boundary is that the writer key exists only in the default-branch environment.
