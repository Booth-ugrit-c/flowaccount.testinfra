# ADR 0005: split the gate into a language-neutral core and per-stack adapters

Status: Accepted, 2026-10-22

## Context

The sim gate grew in one directory, `tools/sim_gate`, for one stack. Routing a job to a runner, classifying a failed job as `test` or `infra`, joining lane verdicts, resolving the head and base refs, cleaning a self-hosted host before a job and killing its own runner do not depend on the language under test. Selecting affected projects, planning the build and caching build output do: they read a .NET solution today and an Nx workspace next.

A second stack added to the same directory would make every stack-specific change touch the shared verdict path, and nothing would stop the shared tools from quietly calling one stack's code. A rule nobody can see fail is not a rule.

## Decision

Layout:
- `core/` holds the language-neutral tools and their tests (`core/tests/`): route, classify, gate, the ref resolver, the pre-job hygiene, the runner kill and the runner-lost fingerprints.
- `adapters/<stack>/` holds what a stack needs to plug in: affected selection, plan, cache, test runner and counts parser, each with its own tests.
- Direction: adapters may import core; core imports nothing from `adapters` and nothing from the dotnet tools that have not moved yet.
- `core` is a namespace package without `__init__.py`, as the adapter directories are; the layering test pins `core.__path__` to this repository so a package of the same name elsewhere on the path fails a test instead of being run.

Order:
- This change moves the core tools only, with no change in behaviour. The dotnet selection, plan and cache tools stay in `tools/sim_gate` and move into `adapters/dotnet` in a following change, which also splits the generic object store out of the cache client into core.
- The attempt's `run` job runs the pre-job hygiene and the runner kill from its checkout of the head under test. After this change a dispatch whose head predates the move fails the Hygiene step on a self-hosted runner, which classify reads as `infra`. Heads must contain `core/` until the hygiene runs from a trusted checkout of the workflow's own tree.

Checks (`core/tests/test_layering.py`, each shown red on a mutated file or a planted file before green):
- No module under `core/`, tests included, imports `adapters` or the dotnet tools.
- No production file under `core/` names an adapter path or the dotnet tools by text, which covers `python -m` and `sh` calls.
- `pyproject.toml` testpaths contain `core`, every test file in the repository sits under a testpath, and a default `pytest --collect-only` reaches every core test file.

## Consequences

- A new stack adds an adapter directory and its workflow; the verdict path does not change.
- Test file basenames stay unique across the repository: test directories have no `__init__.py`, so two `test_plan.py` files would collide. Adapter tests carry the stack in the name (`test_dotnet_plan.py`).
- Language-specific details still inside core after this move, the `Set up .NET` infra step in classify and the dotnet survivors in the pre-job hygiene, become adapter inputs in a later change.
- The layering check reads literal module names and paths; a name assembled at run time would pass it. Review covers that case.
