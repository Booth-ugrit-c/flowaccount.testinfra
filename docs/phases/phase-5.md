# Phase 5: comparison of the three designs on validated replays

## What this phase is

The three designs of [ADR 0003](../adr/0003-three-designs-on-one-budget.md) replay the same window of gates and are compared on the same metrics, on replays that passed the validation of [ADR 0002](../adr/0002-replay-validation-and-comparison.md). The calibrated comparison uses private recorded history; its inputs and numbers stay outside this repository. The repository holds the tooling and the synthetic evidence that it behaves.

## Metrics

`python -m trials.compare --window W --compress C --baseline D --run design=collect-dir ...` prints one table row per metric and design; `--json` also writes every per-run value at full precision. Wait and wall values are seconds scaled back by `--compress`. The table prints values below 10 with three decimals and larger ones with one (`trials/compare/cli.py:18`).

| Metric | Meaning | Source |
|---|---|---|
| `<group>/wait/median`, `.../p90` | start minus creation per job, for group `all`, each lane, each gate kind | `trials/compare/stats.py:27-41` |
| `<group>/wall/median`, `.../p90` | completion minus creation per job, same groups | `trials/compare/stats.py:27-41` |
| `gate/<kind or all>/wall/median`, `.../p90` | first job created to last job completed, per gate | `trials/compare/stats.py:60-74` |
| `host/cpu_busy_mean` | mean busy fraction per host over the run, averaged across hosts | `trials/compare/stats.py:112-128` |
| `host/mem_peak_mib` | sum over hosts of each host's peak memory | `trials/compare/stats.py:127` |
| `host/idle_while_waiting` | mean of `1 - busy` per host, sampled only while a job waits | `trials/compare/stats.py:94-109` |
| `host/waiting_s` | seconds with work waiting, merged across jobs | `trials/compare/stats.py:77-95` |

Host rows exist only when a run has sample files next to its collect directory (`trials/compare/cli.py:55-61`).

## Per-design statistics

| Column | Rule | Source |
|---|---|---|
| mean, sd, cv | mean and sample sd over the runs of one design; cv is sd over mean; sd needs two runs | `trials/compare/stats.py:131-146` |
| delta vs baseline | design mean minus baseline mean, with the percentage of the baseline mean | `trials/compare/stats.py:151-153` |
| minimum detectable | `2.8 * sqrt(sdA^2/nA + sdB^2/nB)` | `trials/compare/stats.py:8`, `150` |
| detectable | `yes` when the absolute delta exceeds the minimum, `no` when it does not (the difference is not detectable at this n), `n/a` when sd is undefined | `trials/compare/stats.py:155`, `trials/compare/cli.py:35` |

## Refusals

Every refusal exits 2 with a `compare:` line on stderr (`trials/compare/cli.py:67-69`); nothing is guessed.

| Condition | Source |
|---|---|
| a window gate without a kind | `trials/compare/stats.py:15-16` |
| a replayed job that failed, was cancelled or never started | `trials/validate/stats.py:41-42` |
| a window job with no replay record (an incomplete collect) | `trials/compare/stats.py:28-30` |
| a run with no completed job | `trials/compare/stats.py:55-56` |
| a collected gate whose jobs map to zero or several window gates | `trials/compare/stats.py:64-66` |
| samples that miss part of the run or leave a hole longer than three intervals | `trials/compare/stats.py:121-125` |
| no samples while work waits | `trials/compare/stats.py:106-107` |
| runs of one design with different sample file counts, or some designs without samples | `trials/compare/cli.py:63-65` |
| a baseline with no runs | `trials/compare/stats.py:136-137` |

`trials.compare` does not read the `collected` flag the collect step writes (`trials/dispatch/collect.py:127`); an unfinished collect shows up as the missing or non-succeeded job rows above.

## Reproducibility

The same command on the same inputs gives byte-identical output. Checked on a synthetic window of three jobs in two gates, two designs, two runs each, with sample files (the fixtures of `trials/compare/tests/test_compare.py`), running the command twice and comparing both the table and the `--json` file with `filecmp.cmp(..., shallow=False)`: both identical.

## Changes in this phase

| Change | Red first |
|---|---|
| a replay gate whose kind is neither `pr` nor `schedule` is refused while the plan is built | the test failed with `DID NOT RAISE SpecError` |
| the trial gate matrix job needs input validation | the test found no `needs: validate` on the matrix job |
| the kube namespace quota is 32 CPU, 48 GiB, 24 pods, above a full pool | the quota test failed with `assert 16 >= 26.05` |
| fractional metrics print with three decimals | `0.12345` printed as `0.1` |

## Known limits

- The lab runs few replays per design, so the minimum detectable effect is wide; read `detectable` before any delta.
- The kube design is one node.
- The improved design is measured as one bundle; the effect of each tuning is not separated.
- The penalty of gates running concurrently is not computed.
- No rank test (Mann-Whitney U) is computed; the verdict is the normal-approximation minimum detectable effect.
