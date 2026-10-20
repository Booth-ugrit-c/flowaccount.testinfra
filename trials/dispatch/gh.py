import json
import os
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlencode

from trials.dispatch.clock import Clock

RATE_LIMIT_MARKERS = ("rate limit", "http 429", "abuse detection")
TRANSIENT_MARKERS = ("http 500", "http 502", "http 503", "http 504", "timeout", "connection reset")
FALLBACK_RATE_WAIT_S = 60.0


class GhError(RuntimeError):
    pass


@dataclass
class GhResult:
    rc: int
    out: str
    err: str


def subprocess_runner(args: list[str], stdin: str | None = None) -> GhResult:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    proc = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    return GhResult(proc.returncode, proc.stdout, proc.stderr)


def curl_log_fetcher(repo: str, job_id: int) -> str:
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False).stdout.strip()
    if not token:
        raise GhError("gh auth token returned nothing")
    proc = subprocess.run(
        ["curl", "-fsSL", "--config", "-", f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs"],
        input=f'header = "Authorization: Bearer {token}"\n',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise GhError(f"log fetch for job {job_id} failed rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


class GhClient:
    def __init__(self, runner=subprocess_runner, clock: Clock | None = None, log_fetcher=curl_log_fetcher, max_attempts: int = 8):
        self.runner = runner
        self.clock = clock or Clock()
        self.log_fetcher = log_fetcher
        self.max_attempts = max_attempts

    def api(self, path: str, *, method: str = "GET", body=None, params: dict | None = None):
        url = path + ("?" + urlencode(params) if params else "")
        args = ["api", "-X", method, url]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        transient_tries = 0
        for _ in range(self.max_attempts):
            result = self.runner(args, stdin)
            if result.rc == 0:
                return json.loads(result.out) if result.out.strip() else None
            text = (result.err + result.out).lower()
            if any(marker in text for marker in RATE_LIMIT_MARKERS):
                self._wait_for_rate_limit()
                continue
            if method == "GET" and transient_tries < 3 and any(marker in text for marker in TRANSIENT_MARKERS):
                transient_tries += 1
                self.clock.sleep(2.0**transient_tries)
                continue
            raise GhError(f"gh {method} {path} failed rc={result.rc}: {(result.err or result.out).strip()}")
        raise GhError(f"gh {method} {path} still rate limited after {self.max_attempts} attempts")

    def job_log(self, repo: str, job_id: int) -> str:
        return self.log_fetcher(repo, job_id)

    def _wait_for_rate_limit(self) -> None:
        wait = FALLBACK_RATE_WAIT_S
        probe = self.runner(["api", "rate_limit"], None)
        if probe.rc == 0:
            try:
                core = json.loads(probe.out)["resources"]["core"]
                if core["remaining"] == 0:
                    wait = max(core["reset"] - self.clock.time(), 0.0) + 1.0
            except (ValueError, KeyError, TypeError):
                pass
        print(f"rate limited: sleeping {wait:.0f}s", file=sys.stderr)
        self.clock.sleep(wait)
