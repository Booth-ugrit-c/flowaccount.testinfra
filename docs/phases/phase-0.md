# Phase 0: skeleton, leak guard, mechanism CI

## Built

| Part | Files |
|---|---|
| Repo skeleton | `README.md`, `AGENTS.md`, `CLAUDE.md`, `CODEOWNERS`, `.gitignore`, `.gitattributes`, `docs/adr/0001-layout-and-designs.md` |
| Leak guard | `tools/leak_guard/` (`scan`, `push-range`, `ref-name`, `scan-text`), `.githooks/pre-push` |
| Mechanism CI | `.github/workflows/leak-guard.yml` (every branch, tag and pull request), `.github/workflows/ci.yml` (tests and workflow lint), `.github/zizmor.yml`, `requirements-dev.txt` (hash-pinned) |

Repository settings: wiki off, all actions must be pinned to a full commit SHA, `main` protected against deletion and force-push.

## How the leak guard works

- `scan` reads every path from `git ls-files`, never a glob, and prints the count it scanned so the count can be checked against `git ls-files | wc -l`.
- `push-range` walks every object a push would send (`git rev-list --objects`): each blob in raw and NUL-stripped form (UTF-16 files), each commit and tag object with all headers and its message, and each path name.
- `ref-name` checks branch and tag names; the hook runs it before anything else, deletions included.
- Generic rules: 12-digit ids, IPv4 outside loopback and documentation ranges, private-key headers, GitHub tokens, cloud access key ids, instance-size runner labels, email addresses outside noreply and example domains. A private deny-list is added from a file locally and from a base64 secret in CI.
- Output never prints a deny-list entry or the text it matched.

## Evidence

| Check | Result |
|---|---|
| Test suite | 199 passed |
| Tree scan count | equals `git ls-files \| wc -l` on every run (29 at the run below) |
| Leak guard on the phase branch push | [run 39221023373](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39221023373): deny-list loaded from the secret, 0 findings in tree, ref name and 13 pushed commits |
| `actionlint`, `zizmor`, `shellcheck` | exit 0 locally |

## Red first

| Check | Broken input | Result |
|---|---|---|
| Leak guard rules | each of the 7 generic rules removed in turn from a copy of the package | only that rule's tests failed (4 to 12 tests each); repeated on the final tree for one rule: 4 failed |
| Dot directories | invented token in a nested dot directory | exit 1, path reported, token not printed |
| Add-then-remove | UTF-16 file with an invented token added in one commit and removed in the next | exit 1 on `push-range` (was exit 0 before the object walk) |
| Ref names | branch name, tag name and tag-of-tag message with an invented token | refused (were accepted before `ref-name` and the object walk) |
| Pre-push hook | push carrying an invented token to a bare remote | push refused |
| CI deny-list secret | branch adding a canary that exists only in the CI list | [run 39221206742](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39221206742) failed: `docs/canary-probe.txt:1 deny-list entry #232` |
| Action pinning | pull request switching one action to a tag | [run 39221240970](https://github.com/Booth-ugrit-c/flowaccount.testinfra/actions/runs/39221240970) failed twice over: zizmor `unpinned-uses` exit 14, and the repository policy refused the action |

## Known limits

- The hook protects only clones that ran `git config core.hooksPath .githooks`. CI runs after a push is already public, so a red leak-guard run means the content is exposed: rename or rotate first, investigate second.
- Fork pull requests get no secrets; their leak-guard run applies the generic rules only.
- Commits GitHub writes itself (pull request test merges, merges from the web interface, web edits) never pass the hook; their author identity follows the account's email privacy setting. A scheduled full-history scan on `main` follows in phase 1.
- Encoded content (gzip, base64 inside a file) is not decoded before scanning.
- Public surfaces outside git (pull request and issue text) are checked by piping the text through `python -m tools.leak_guard scan-text` before posting.
