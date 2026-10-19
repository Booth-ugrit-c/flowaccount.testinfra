# Contributing (people and coding agents)

## This repository is public
- Every push to any branch is public at once and cannot be withdrawn.
- Never commit internal data of any organisation: hostnames, internal addresses, account ids, real runner labels, project or job names, measured numbers from real systems, workload files derived from real jobs. The workload here is synthetic; calibration files live outside the repo and are passed by path at run time.
- Confidential values go in `secrets.*`, never `vars.*`: variables are printed unmasked in logs.
- Job names in logs are opaque (`unit-017`, `integration-004`).
- Run `git config core.hooksPath .githooks` once per clone. The pre-push hook runs `python -m tools.leak_guard`; never bypass it.

## Evidence
- Red first: every check, invariant and validation is shown failing on a deliberately broken input before it is shown passing. A check never seen red is not done.
- Numbers come from measurement. A gap stays a gap; never fill it with a plausible value.
- During a change run its own tests; the full suite, lint and workflow lint run before the pull request.

## Code
- Python 3.12, standard library plus pytest.
- Comments: none by default; one line naming a trap when unavoidable. The reason for a change goes in the commit message.
- Workflows: every action pinned to a full commit SHA, `permissions: contents: read` unless a job needs more, no `${{ }}` expression inside a `run:` script.

## Pull requests
- Title matches `^(feat|maintenance|refactor|fix|config|infra|chore|e2e|test): <subject>`, no scope in parentheses.
- At most 30 files per pull request.
- One pull request per phase into `main`; maintainers merge. No force-push.
- Decisions that shape the design get an ADR in `docs/adr/` in the same pull request.
