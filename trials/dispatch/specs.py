import json
import re
from pathlib import Path

JOB_ID_RE = re.compile(r"^[a-z]+-\d{3,}$")
SAFE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_SPECS = 256
MAX_INPUTS_CHARS = 60000
SPLITS = ("fit", "holdout", "all")


class SpecError(ValueError):
    pass


def parse_lane_map(text: str, cast, flag: str) -> dict:
    result = {}
    for part in text.split(","):
        lane, sep, value = part.partition("=")
        lane, value = lane.strip(), value.strip()
        if not sep or not lane or not value:
            raise SpecError(f"{flag}: expected lane=value pairs, got {part!r}")
        try:
            result[lane] = cast(value)
        except ValueError as exc:
            raise SpecError(f"{flag}: bad value for {lane}: {value!r}") from exc
    return result


def load_calibration(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError(f"calibration {path}: {exc}") from exc
    gates = data.get("gates") if isinstance(data, dict) else None
    if not isinstance(gates, list) or not gates:
        raise SpecError("calibration holds no gates")
    for gate in gates:
        for key in ("gate", "arrival_offset_s", "split", "jobs"):
            if key not in gate:
                raise SpecError(f"calibration gate missing {key!r}: {gate!r:.80}")
        if gate["split"] not in ("fit", "holdout"):
            raise SpecError(f"gate {gate['gate']}: split must be fit or holdout")
        if not SAFE_RE.match(str(gate["gate"])):
            raise SpecError(f"gate id {gate['gate']!r} must match {SAFE_RE.pattern}")
    return data


def select_gates(calibration: dict, split: str, limit: int | None) -> list[dict]:
    if split not in SPLITS:
        raise SpecError(f"split must be one of {SPLITS}")
    gates = [g for g in calibration["gates"] if split == "all" or g["split"] == split]
    gates.sort(key=lambda g: g["arrival_offset_s"])
    if limit is not None:
        gates = gates[:limit]
    if not gates:
        raise SpecError(f"no gates selected for split={split} limit={limit}")
    return gates


def label_for(labels: dict, lane: str, kind: str | None) -> str | None:
    if kind is None and any(key.startswith(f"{lane}:") for key in labels):
        raise SpecError(f"--labels routes lane {lane!r} by kind but a gate has no kind; run annotate-events first")
    return labels.get(f"{lane}:{kind}", labels.get(lane))


def build_specs(gate: dict, labels: dict, factors: dict, threads: dict, memory: dict) -> tuple[list[dict], list[str]]:
    specs, skipped, seen = [], [], set()
    for job in gate["jobs"]:
        job_id, lane = job["id"], job["lane"]
        if not JOB_ID_RE.match(job_id):
            raise SpecError(f"gate {gate['gate']}: job id {job_id!r} must match {JOB_ID_RE.pattern}")
        if job_id in seen:
            raise SpecError(f"gate {gate['gate']}: duplicate job id {job_id}")
        seen.add(job_id)
        if job.get("duration_s") is None:
            skipped.append(job_id)
            continue
        for flag, table in (("--factor", factors), ("--threads", threads), ("--memory", memory)):
            if lane not in table:
                raise SpecError(f"gate {gate['gate']} job {job_id}: lane {lane!r} missing from {flag}")
        label = label_for(labels, lane, gate.get("kind"))
        if label is None:
            raise SpecError(f"gate {gate['gate']} job {job_id}: lane {lane!r} kind {gate.get('kind')!r} missing from --labels")
        if not SAFE_RE.match(label):
            raise SpecError(f"--labels: {label!r} must match {SAFE_RE.pattern}")
        specs.append(
            {
                "id": job_id,
                "lane": lane,
                "cpu_seconds": round(factors[lane] * job["duration_s"], 3),
                "threads": threads[lane],
                "memory_mib": memory[lane],
                "services": list(job.get("services") or []),
                "label": label,
            }
        )
    if len(specs) > MAX_SPECS:
        raise SpecError(f"gate {gate['gate']}: {len(specs)} specs exceed the {MAX_SPECS} matrix limit")
    return specs, skipped
