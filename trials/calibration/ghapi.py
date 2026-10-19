from __future__ import annotations

import json
import subprocess
import time
from typing import Callable

from .errors import ApiError

MAX_RATE_WAITS = 5
SECONDARY_LIMIT_PAUSE_S = 60.0
PERMANENT_MARKERS = ("HTTP 404", "HTTP 401", "Not Found", "Bad credentials")


class GhError(Exception):
    def __init__(self, stderr: str, returncode: int = 1):
        super().__init__(stderr)
        self.stderr = stderr
        self.returncode = returncode


def subprocess_gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise GhError(proc.stderr or proc.stdout, proc.returncode)
    return proc.stdout


def is_rate_limited(stderr: str) -> bool:
    return "rate limit" in stderr.lower()


class GhClient:
    def __init__(
        self,
        runner: Callable[[list[str]], str] = subprocess_gh,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        retries: int = 4,
        backoff_s: float = 2.0,
    ):
        self.runner = runner
        self.sleep = sleep
        self.now = now
        self.retries = retries
        self.backoff_s = backoff_s
        self.calls = 0
        self.rate_probes = 0
        self.rate_waits = 0

    def get(self, path: str) -> dict:
        return json.loads(self._call(["api", path]))

    def pages(self, path: str) -> list[dict]:
        data = json.loads(self._call(["api", "--paginate", "--slurp", path]))
        if not isinstance(data, list):
            raise ApiError("paginated response was not a list of pages")
        return data

    def _call(self, args: list[str]) -> str:
        failures = 0
        rate_waits = 0
        while True:
            self.calls += 1
            try:
                out = self.runner(args)
                json.loads(out)
                return out
            except GhError as exc:
                if is_rate_limited(exc.stderr):
                    rate_waits += 1
                    if rate_waits > MAX_RATE_WAITS:
                        raise ApiError("rate limit persisted after repeated waits") from None
                    self._wait_for_reset()
                    continue
                if any(marker in exc.stderr for marker in PERMANENT_MARKERS):
                    raise ApiError(f"gh api failed: {exc.stderr.strip()[:200]}") from None
                failure = exc.stderr.strip()[:200]
            except ValueError:
                failure = "response was not valid JSON"
            failures += 1
            if failures > self.retries:
                raise ApiError(f"gh api failed after {self.retries} retries: {failure}")
            self.sleep(self.backoff_s * 2 ** (failures - 1))

    def _wait_for_reset(self) -> None:
        self.rate_probes += 1
        try:
            core = json.loads(self.runner(["api", "rate_limit"]))["resources"]["core"]
            remaining, reset = core["remaining"], core["reset"]
        except (GhError, ValueError, KeyError, TypeError):
            remaining, reset = 0, self.now() + SECONDARY_LIMIT_PAUSE_S
        self.rate_waits += 1
        pause = max(reset - self.now(), 0.0) + 1.0 if remaining == 0 else SECONDARY_LIMIT_PAUSE_S
        self.sleep(pause)
