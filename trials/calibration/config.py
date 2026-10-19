from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConfigError

SERVICE_TYPES = ("sql", "document", "cache", "search", "queue")
MATCH_FIELDS = ("labels", "runner_name")
REQUIRED_KEYS = (
    "repo", "workflows", "window", "flip", "match_field", "runners",
    "manual_runners", "lanes", "service_patterns",
)


@dataclass(frozen=True)
class Lane:
    name: str
    pattern: re.Pattern
    default_services: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    repo: str
    workflows: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    flip: datetime
    match_field: str
    runners: frozenset[str]
    manual_runners: frozenset[str]
    lanes: tuple[Lane, ...]
    service_patterns: tuple[tuple[re.Pattern, tuple[str, ...]], ...]


def parse_time(value: object, key: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigError(f"config key {key}: expected an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ConfigError(f"config key {key}: not an ISO-8601 time") from None
    if parsed.tzinfo is None:
        raise ConfigError(f"config key {key}: time needs a UTC offset or Z")
    return parsed.astimezone(timezone.utc)


def _string_list(raw: dict, key: str, allow_empty: bool = False) -> tuple[str, ...]:
    value = raw[key]
    ok = isinstance(value, list) and all(isinstance(item, str) and item for item in value)
    if not ok or (not value and not allow_empty):
        raise ConfigError(f"config key {key}: expected a non-empty list of strings")
    return tuple(value)


def _services(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(item not in SERVICE_TYPES for item in value):
        raise ConfigError(f"config key {key}: services must be a list drawn from {list(SERVICE_TYPES)}")
    return tuple(value)


def _pattern(value: object, key: str) -> re.Pattern:
    try:
        return re.compile(value)
    except (re.error, TypeError):
        raise ConfigError(f"config key {key}: invalid regular expression") from None


def parse_config(raw: object) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    for key in REQUIRED_KEYS:
        if key not in raw:
            raise ConfigError(f"config is missing key: {key}")
    repo = raw["repo"]
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ConfigError("config key repo: expected owner/name")
    window = raw["window"]
    if not isinstance(window, dict) or "start" not in window or "end" not in window:
        raise ConfigError("config key window: expected start and end")
    start = parse_time(window["start"], "window.start")
    end = parse_time(window["end"], "window.end")
    if end <= start:
        raise ConfigError("config key window: end must be after start")
    if raw["match_field"] not in MATCH_FIELDS:
        raise ConfigError(f"config key match_field: expected one of {list(MATCH_FIELDS)}")
    runners = frozenset(_string_list(raw, "runners"))
    manual = frozenset(_string_list(raw, "manual_runners", allow_empty=True))
    lanes = []
    if not isinstance(raw["lanes"], list) or not raw["lanes"]:
        raise ConfigError("config key lanes: expected a non-empty list")
    for entry in raw["lanes"]:
        if not isinstance(entry, dict) or not {"name", "job_pattern", "default_services"} <= entry.keys():
            raise ConfigError("config key lanes: each lane needs name, job_pattern, default_services")
        if not isinstance(entry["name"], str) or not re.fullmatch(r"[a-z]+", entry["name"]):
            raise ConfigError("config key lanes: lane name must match [a-z]+")
        lanes.append(Lane(entry["name"], _pattern(entry["job_pattern"], "lanes.job_pattern"),
                          _services(entry["default_services"], "lanes.default_services")))
    if len({lane.name for lane in lanes}) != len(lanes):
        raise ConfigError("config key lanes: lane names must be unique")
    if not isinstance(raw["service_patterns"], list):
        raise ConfigError("config key service_patterns: expected a list")
    service_patterns = []
    for entry in raw["service_patterns"]:
        if not isinstance(entry, dict) or not {"pattern", "services"} <= entry.keys():
            raise ConfigError("config key service_patterns: each entry needs pattern and services")
        service_patterns.append((_pattern(entry["pattern"], "service_patterns.pattern"),
                                 _services(entry["services"], "service_patterns.services")))
    return Config(
        repo=repo, workflows=_string_list(raw, "workflows"), window_start=start, window_end=end,
        flip=parse_time(raw["flip"], "flip"), match_field=raw["match_field"], runners=runners,
        manual_runners=manual, lanes=tuple(lanes), service_patterns=tuple(service_patterns),
    )


def load_config(path: Path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc.strerror}") from None
    try:
        return parse_config(json.loads(text))
    except json.JSONDecodeError:
        raise ConfigError("config is not valid JSON") from None
