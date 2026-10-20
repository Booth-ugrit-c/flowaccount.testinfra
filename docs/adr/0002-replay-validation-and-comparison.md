# ADR 0002: validate a compressed replay before comparing designs on it

Status: Accepted, 2026-10-20

## Context

Designs are compared by replaying recorded job history as synthetic work: each job burns CPU-seconds, holds memory and starts service containers, arriving at its recorded offset. The history itself stays private; only its shape reaches the replay, through a calibration file passed by path.

A lab host is smaller than the system the history came from, so a window is compressed by a factor `k`: arrival offsets are divided by `k` and each job's work is scaled by a per-lane factor. A compressed replay is only useful if the queueing it produces resembles the real queueing. A comparison run on an unvalidated replay measures the replay, not the design.

Run-to-run noise on a shared lab host is not zero, and only a few runs fit in a trial session. A difference between two designs smaller than that noise is not a finding.

## Decision

Windows:
- Fixed-length windows that start when no job is in flight, chosen to hold enough jobs per lane in both the fit and the holdout split (`trials/windows`).
- Each gate keeps its kind (`pr` or `schedule`) so a design can route the two differently; a gate is `schedule` when it was triggered by a schedule or ran on the default branch.

Calibration, before any comparison:
- Fit: per lane, `new factor = old factor x real median duration / replay median duration` over fit-split windows (`python -m trials.validate fit`).
- Holdout verdict: on windows not used for the fit, per lane and for both wall time and wait time, the replay median (multiplied back by `k`) must fall inside the real interquartile range (`python -m trials.validate verdict`).
- Red check: the same verdict must fail on a replay deliberately mis-scaled by 2. A verdict that cannot fail is not a verdict.
- The order of fit, holdout, refit and the cut-off for the comparison is written down before the first holdout result is seen. One refit on more windows is allowed; a second holdout failure labels every comparison number "synthetic load, not calibrated".

Comparison (`python -m trials.compare`):
- All designs replay the same window, in interleaved rounds so drift in the lab host is spread across designs rather than landing on one.
- Per run: wait and wall median and 90th percentile by lane, by gate kind and overall; host CPU busy fraction and peak memory from 1-second `/proc` samplers on every runner host, cut to the run's span.
- Per design: mean, standard deviation and coefficient of variation of the per-run values; delta against the baseline design; minimum detectable effect `2.8 x sqrt(sdA^2/nA + sdB^2/nB)` (two-sided, alpha 0.05, power 0.8).
- A delta smaller than the minimum detectable effect is reported as "not detectable", never as "no difference".

## Consequences

- Real job data never enters the repository; the tools take paths and print aggregates.
- Every comparison number carries the holdout verdict it rests on.
- With few runs per design the minimum detectable effect is wide; it is printed next to every delta so a reader can see which differences the trial could and could not resolve.
- Compression keeps the ratio of offered work to capacity only as far as the fitted factors hold; the holdout verdict is the check on that, not an assumption.
