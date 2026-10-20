import json
import math
import re
from dataclasses import dataclass

SERVICE_TYPES = ("sql", "document", "cache", "search", "queue")
REQUIRED_KEYS = ("id", "lane", "cpu_seconds", "threads", "memory_mib")
OPTIONAL_KEYS = ("services", "label")
MAX_CPU_SECONDS = 86400.0
MAX_THREADS = 256
MAX_MEMORY_MIB = 1048576

ID_RE = re.compile(r"^[a-z]+-\d{3,}$")
_LANE_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class JobSpec:
    id: str
    lane: str
    cpu_seconds: float
    threads: int
    memory_mib: int
    services: tuple


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_spec(obj):
    if not isinstance(obj, dict):
        raise SpecError("spec must be a JSON object")
    unknown = sorted(set(obj) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise SpecError("unknown keys: " + ", ".join(str(k) for k in unknown))
    missing = [k for k in REQUIRED_KEYS if k not in obj]
    if missing:
        raise SpecError("missing keys: " + ", ".join(missing))

    job_id = obj["id"]
    if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
        raise SpecError("id must match ^[a-z]+-\\d{3,}$")
    lane = obj["lane"]
    if not isinstance(lane, str) or not _LANE_RE.fullmatch(lane):
        raise SpecError("lane must match ^[a-z][a-z0-9-]*$")

    cpu = obj["cpu_seconds"]
    if not _is_number(cpu) or not math.isfinite(cpu) or not 0 < cpu <= MAX_CPU_SECONDS:
        raise SpecError(f"cpu_seconds must be a number in (0, {MAX_CPU_SECONDS:g}]")
    threads = obj["threads"]
    if not _is_int(threads) or not 1 <= threads <= MAX_THREADS:
        raise SpecError(f"threads must be an integer in 1..{MAX_THREADS}")
    memory = obj["memory_mib"]
    if not _is_int(memory) or not 0 <= memory <= MAX_MEMORY_MIB:
        raise SpecError(f"memory_mib must be an integer in 0..{MAX_MEMORY_MIB}")

    services = obj.get("services", [])
    if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        raise SpecError("services must be a list of strings")
    bad = [s for s in services if s not in SERVICE_TYPES]
    if bad:
        raise SpecError("unknown services: " + ", ".join(bad))
    if len(set(services)) != len(services):
        raise SpecError("services must not repeat")
    label = obj.get("label")
    if label is not None and not isinstance(label, str):
        raise SpecError("label must be a string")

    return JobSpec(job_id, lane, float(cpu), threads, memory, tuple(services))


def parse_spec_json(text):
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise SpecError(f"not valid JSON: {exc}") from None
    return parse_spec(obj)


def _reject_constant(name):
    raise ValueError(f"constant {name} is not allowed")


def check_specs(text):
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        return [f"not valid JSON: {exc}"]
    if not isinstance(data, list):
        return ["file must hold a JSON array of specs"]
    problems = []
    for index, item in enumerate(data):
        try:
            parse_spec(item)
        except SpecError as exc:
            label = item.get("id") if isinstance(item, dict) else None
            problems.append(f"spec {index} ({label}): {exc}")
    return problems
