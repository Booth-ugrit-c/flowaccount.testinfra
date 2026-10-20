import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trials.dispatch.clock import Clock
from trials.dispatch.gh import GhClient, GhError
from trials.dispatch.specs import MAX_INPUTS_CHARS, SAFE_RE, SpecError, build_specs, select_gates

WORKFLOW_FILE = "trial-gate.yml"
STATE_FILE = "dispatch.json"
LOOKBACK_S = 30.0


@dataclass
class PlannedGate:
    gate_id: str
    split: str
    arrival_offset_s: float
    due_s: float
    specs: list[dict]
    skipped: list[str]


@dataclass
class Plan:
    trial_id: str
    repo: str
    ref: str
    compress: float
    split: str
    items: list[PlannedGate] = field(default_factory=list)

    @property
    def job_count(self) -> int:
        return sum(len(item.specs) for item in self.items)


def run_name(trial_id: str, gate_id: str) -> str:
    return f"trial {trial_id} gate {gate_id}"


def build_plan(calibration, *, split, limit, labels, factors, threads, memory, compress, repo, ref, trial_id) -> Plan:
    if compress <= 0:
        raise SpecError("--compress must be positive")
    if not SAFE_RE.match(trial_id):
        raise SpecError(f"--trial-id must match {SAFE_RE.pattern}")
    plan = Plan(trial_id=trial_id, repo=repo, ref=ref, compress=compress, split=split)
    gates = select_gates(calibration, split, limit)
    first = gates[0]["arrival_offset_s"]
    for gate in gates:
        specs, skipped = build_specs(gate, labels, factors, threads, memory)
        plan.items.append(
            PlannedGate(
                gate_id=gate["gate"],
                split=gate["split"],
                arrival_offset_s=gate["arrival_offset_s"],
                due_s=(gate["arrival_offset_s"] - first) / compress,
                specs=specs,
                skipped=skipped,
            )
        )
    return plan


def format_plan(plan: Plan) -> str:
    skipped = sum(len(item.skipped) for item in plan.items)
    lines = [
        f"plan: {len(plan.items)} gate / {plan.job_count} jobs ({skipped} null-duration skipped)",
        f"trial {plan.trial_id} repo {plan.repo} ref {plan.ref} compress {plan.compress:g} split {plan.split}",
    ]
    for item in plan.items:
        lines.append(
            f"gate {item.gate_id} split={item.split} arrival_offset_s={item.arrival_offset_s:g} "
            f"due_after_start_s={item.due_s:g} jobs={len(item.specs)} skipped_null={len(item.skipped)}"
        )
        lines.extend(f"  {json.dumps(spec, separators=(',', ':'))}" for spec in item.specs)
    return "\n".join(lines)


def write_state(out_dir: Path, state: dict) -> None:
    tmp = out_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out_dir / STATE_FILE)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_run_id(gh: GhClient, repo: str, name: str, since_epoch: float, clock: Clock, timeout_s: float, interval_s: float) -> dict:
    deadline = clock.monotonic() + timeout_s
    while True:
        payload = gh.api(
            f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs",
            params={"event": "workflow_dispatch", "per_page": 100, "created": f">={_iso(since_epoch - LOOKBACK_S)}"},
        )
        runs = [r for r in (payload or {}).get("workflow_runs", []) if name in (r.get("display_title"), r.get("name"))]
        if len(runs) == 1:
            return runs[0]
        if len(runs) > 1:
            raise GhError(f"{len(runs)} runs match run-name {name!r}: ids {[r['id'] for r in runs]}")
        if clock.monotonic() >= deadline:
            raise GhError(f"no run named {name!r} appeared within {timeout_s:g}s")
        clock.sleep(interval_s)


def dispatch_gate(gh: GhClient, plan: Plan, item: PlannedGate, clock: Clock, discovery_timeout_s: float, discovery_interval_s: float) -> dict:
    inputs = {
        "trial_id": plan.trial_id,
        "gate_id": item.gate_id,
        "jobs": json.dumps(item.specs, separators=(",", ":")),
    }
    if sum(len(value) for value in inputs.values()) > MAX_INPUTS_CHARS:
        raise SpecError(f"gate {item.gate_id}: dispatch inputs exceed {MAX_INPUTS_CHARS} characters")
    dispatched_epoch = clock.time()
    response = gh.api(
        f"repos/{plan.repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        method="POST",
        body={"ref": plan.ref, "inputs": inputs, "return_run_details": True},
    )
    if isinstance(response, dict) and response.get("workflow_run_id"):
        return {
            "run_id": response["workflow_run_id"],
            "run_url": response.get("html_url"),
            "run_id_source": "dispatch-response",
            "dispatched_at": _iso(dispatched_epoch),
        }
    run = discover_run_id(gh, plan.repo, run_name(plan.trial_id, item.gate_id), dispatched_epoch, clock, discovery_timeout_s, discovery_interval_s)
    return {
        "run_id": run["id"],
        "run_url": run.get("html_url"),
        "run_id_source": "run-name-poll",
        "dispatched_at": _iso(dispatched_epoch),
    }


def run_replay(plan: Plan, gh: GhClient, clock: Clock, out_dir: Path, discovery_timeout_s: float = 120.0, discovery_interval_s: float = 3.0) -> bool:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / STATE_FILE).exists():
        raise SpecError(f"{out_dir / STATE_FILE} already exists: use a fresh --out per trial")
    state = {
        "trial_id": plan.trial_id,
        "repo": plan.repo,
        "ref": plan.ref,
        "workflow": WORKFLOW_FILE,
        "compress": plan.compress,
        "split": plan.split,
        "started_at": clock.iso(),
        "gates": [],
        "skipped_gates": [],
    }
    write_state(out_dir, state)
    start = clock.monotonic()
    ok = True
    for item in plan.items:
        if not item.specs:
            state["skipped_gates"].append({"gate_id": item.gate_id, "reason": "no dispatchable jobs", "skipped_null_duration": item.skipped})
            write_state(out_dir, state)
            continue
        wait = start + item.due_s - clock.monotonic()
        if wait > 0:
            clock.sleep(wait)
        record = {
            "gate_id": item.gate_id,
            "split": item.split,
            "arrival_offset_s": item.arrival_offset_s,
            "due_after_start_s": item.due_s,
            "lateness_s": round(clock.monotonic() - start - item.due_s, 3),
            "spec_ids": [spec["id"] for spec in item.specs],
            "skipped_null_duration": item.skipped,
            "specs": item.specs,
            "run_id": None,
            "error": None,
        }
        try:
            record.update(dispatch_gate(gh, plan, item, clock, discovery_timeout_s, discovery_interval_s))
        except (GhError, SpecError) as exc:
            record["error"] = str(exc)
            ok = False
        state["gates"].append(record)
        write_state(out_dir, state)
    return ok
