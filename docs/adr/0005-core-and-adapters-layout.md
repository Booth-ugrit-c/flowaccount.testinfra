# ADR 0005: split the gate into a language-neutral core and per-stack adapters

Status: Accepted, 2026-10-22

## Context

The sim gate grew in one directory under `tools/` for one stack. Routing a job to a runner, classifying a failed job as `test` or `infra`, joining lane verdicts, resolving the head and base refs, cleaning a self-hosted host before a job and killing its own runner do not depend on the language under test. Selecting affected projects, planning the build and caching build output do: they read a .NET solution today and an Nx workspace next.

A second stack added to the same directory would make every stack-specific change touch the shared verdict path, and nothing would stop the shared tools from quietly calling one stack's code. A rule nobody can see fail is not a rule.

## Decision

Layout:
- `core/` holds the language-neutral tools and their tests (`core/tests/`): route, classify, gate, the ref resolver, the pre-job hygiene, the runner kill, the runner-lost fingerprints and the object store client (`core/store.py`: S3 SigV4, credentials from the environment, the cache tripwire).
- `adapters/<stack>/` holds what a stack needs to plug in: affected selection, plan, cache, test runner and counts parser, each with its own tests. `adapters/dotnet` and `adapters/workspace` both reach the store through `core.store`.
- Direction: adapters may import core, by public names only; core imports nothing from `adapters`.
- `core` is a namespace package without `__init__.py`, as the adapter directories are; the layering test pins `core.__path__` to this repository so a package of the same name elsewhere on the path fails a test instead of being run.

Adapters package:
- `adapters` is a regular package with an `__init__.py`, for the same reason as `core`: Python prefers a regular package of the same name anywhere on `sys.path` over a namespace directory, and the workflows now run `python3 -m adapters.dotnet.*`. The stack directories inside `adapters/` stay namespace directories. A layering test runs `python -m adapters.dotnet.affected` with a shadow `adapters` package on `PYTHONPATH` and checks the repository copy ran.

Order:
- The first change moved the core tools only, with no change in behaviour. The second moves the dotnet selection, plan and cache tools into `adapters/dotnet` and splits the generic object store out of the dotnet cache client into `core/store.py`; behaviour changes only in the error-message prefixes and in `core.store.credentials` refusing a role other than `READ` or `WRITE`. `tools/` keeps only the leak guard.
- The attempt's `run` job runs the pre-job hygiene, the runner kill and the dotnet tools from its checkout of the head under test, so a dispatch whose head predates a move fails on a file that head does not have. A head without `core/` fails the Hygiene step on a self-hosted runner, an infra step, so classify reads `infra`; with `simulate_kill: both` it also fails the Test step at `sh core/kill_own_runner.sh` after the `test-start` marker, which classify reads as `test`. A head without `adapters/dotnet/` fails the Affected step, which is not an infra step, so classify reads `test`. Heads must contain both directories until these steps run from a trusted checkout of the workflow's own tree.

Checks (`core/tests/test_layering.py`, each shown red on a mutated file or a planted file before green):
- No module under `core/`, tests included, imports `adapters`.
- No production file under `core/` names an adapter path by text, which covers `python -m` and `sh` calls.
- No production module under `adapters/` uses a `_`-prefixed name from a `core` module, in any import or attribute spelling.
- `pyproject.toml` testpaths contain `core`, every test file in the repository sits under a testpath, and a default `pytest --collect-only` reaches every core test file.

## Consequences

- A new stack adds an adapter directory and its workflow; the verdict path does not change.
- Test file basenames stay unique across the repository: the test directories under `core/` and `adapters/` have no `__init__.py`, so two `test_plan.py` files there would collide. Adapter tests carry the stack in the name (`test_dotnet_plan.py`).
- Language-specific details still inside core after this move, the `Set up .NET` infra step in classify and the dotnet survivors in the pre-job hygiene, become adapter inputs in a later change.
- The layering check reads literal module names and paths; a name assembled at run time would pass it. Review covers that case.
