from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from tools.sim_gate.route import REPOSITORY, TIMEOUT_SECONDS, MAX_BODY_BYTES, NoRedirect, allowed_url, api_base

FINGERPRINTS = Path(__file__).with_name("fingerprints.txt")
PHASE_MARKERS = ("health", "services", "build", "test-start", "test-end")
TRIPWIRE_MARKER = "cache-tripwire"
KNOWN_MARKERS = PHASE_MARKERS + (TRIPWIRE_MARKER,)
TRIPWIRE_REASON = "cache tripwire"
STEP_TEST = "Test"
INFRA_STEPS = ("Set up job", "Hygiene", "Set up .NET", "Health", "Services")
TIMEOUT_TEXT = "exceeded the maximum execution time"
MAX_PAGES = 5


def load_fingerprints(path=FINGERPRINTS):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip().casefold() for line in lines if line.strip()]


def read_markers(markers_dir):
    try:
        return {name for name in KNOWN_MARKERS if (Path(markers_dir) / name).is_file()}
    except OSError:
        return set()


def read_job(path):
    try:
        job = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return job if isinstance(job, dict) else None


def read_log(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").casefold()
    except OSError:
        return ""


def step_conclusions(job):
    steps = job.get("steps")
    if not isinstance(steps, list):
        return {}
    return {s["name"]: s.get("conclusion") for s in steps if isinstance(s, dict) and "name" in s}


def classify(markers, job, log, fingerprints):
    if TRIPWIRE_MARKER in markers:
        return "infra"
    if job is None:
        return "test"
    conclusion = job.get("conclusion")
    steps = step_conclusions(job)
    if conclusion == "success":
        return "pass" if "test-end" in markers or steps.get(STEP_TEST) == "success" else "test"
    if "test-end" in markers:
        return "test"
    if TIMEOUT_TEXT in log:
        return "test"
    if any(fingerprint in log for fingerprint in fingerprints):
        return "infra"
    if steps.get(STEP_TEST) == "cancelled":
        return "infra"
    if any(steps.get(name) == "failure" for name in INFRA_STEPS):
        return "infra"
    never_started = steps.get(STEP_TEST) in (None, "skipped")
    nothing_failed = not any(c == "failure" for c in steps.values())
    if never_started and nothing_failed and conclusion in ("cancelled", "failure"):
        return "infra"
    return "test"


def classify_dir(job_dir, fingerprints=None):
    job_dir = Path(job_dir)
    return classify(
        read_markers(job_dir / "markers"),
        read_job(job_dir / "job.json"),
        read_log(job_dir / "log.txt"),
        load_fingerprints() if fingerprints is None else fingerprints,
    )


def request(url, token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sim-gate-classify"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError("status")
        return response.read(MAX_BODY_BYTES)


def request_json(url, token):
    return json.loads(request(url, token))


def find_job(base, repository, env, token, suffix):
    run_id, attempt = env["GITHUB_RUN_ID"], env["GITHUB_RUN_ATTEMPT"]
    if not (run_id.isdigit() and attempt.isdigit()):
        raise ValueError("run")
    found, seen = [], 0
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}"
        document = request_json(url, token)
        jobs = document["jobs"]
        found += [j for j in jobs if isinstance(j, dict) and str(j.get("name", "")).endswith(suffix)]
        seen += len(jobs)
        if not jobs or seen >= document.get("total_count", seen):
            break
    if len(found) != 1:
        raise ValueError("job")
    return found[0]


def slim(job):
    steps = [
        {key: s.get(key) for key in ("name", "status", "conclusion", "number")}
        for s in job.get("steps", [])
        if isinstance(s, dict)
    ]
    return {key: job.get(key) for key in ("id", "name", "status", "conclusion")} | {"steps": steps}


def fetch_annotations(base, repository, token, job_id):
    notes = request_json(f"{base}/repos/{repository}/check-runs/{job_id}/annotations?per_page=100", token)
    return "\n".join(f"{n.get('title') or ''} {n.get('message') or ''}" for n in notes if isinstance(n, dict))


def fetch_markers(base, repository, token, run_id, artifact_name, markers_dir):
    listing = request_json(
        f"{base}/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&name={artifact_name}", token
    )
    live = [a for a in listing["artifacts"] if a.get("name") == artifact_name and a.get("expired") is False]
    if len(live) != 1:
        return
    artifact_id = int(live[0]["id"])
    try:
        request(f"{base}/repos/{repository}/actions/artifacts/{artifact_id}/zip", token)
        return
    except urllib.error.HTTPError as redirect:
        location = redirect.headers.get("Location", "") if redirect.code in (301, 302, 307) else ""
    if not allowed_url(location):
        return
    archive = zipfile.ZipFile(io.BytesIO(request(location)))
    markers_dir.mkdir(parents=True, exist_ok=True)
    for name in archive.namelist():
        if name in KNOWN_MARKERS:
            (markers_dir / name).write_bytes(b"")


def fetch_attempt(env, job_dir, job_suffix, artifact_name):
    token, repository, base = env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), api_base(env)
    if not token or base is None or not REPOSITORY.match(repository):
        return
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", artifact_name):
        return
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        job = find_job(base, repository, env, token, job_suffix)
    except Exception as exc:
        print(f"classify: job lookup failed: {type(exc).__name__}", file=sys.stderr)
        return
    (job_dir / "job.json").write_text(json.dumps(slim(job)), encoding="utf-8")
    for label, step in (
        ("annotations", lambda: (job_dir / "log.txt").write_text(fetch_annotations(base, repository, token, job["id"]), encoding="utf-8")),
        ("markers", lambda: fetch_markers(base, repository, token, env["GITHUB_RUN_ID"], artifact_name, job_dir / "markers")),
    ):
        try:
            step()
        except Exception as exc:
            print(f"classify: {label} fetch failed: {type(exc).__name__}", file=sys.stderr)


def main(argv):
    parser = argparse.ArgumentParser(prog="classify")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--fetch-job")
    parser.add_argument("--fetch-artifact")
    parser.add_argument("--reason-file")
    args = parser.parse_args(argv)
    try:
        if args.fetch_job and args.fetch_artifact:
            fetch_attempt(os.environ, args.job_dir, args.fetch_job, args.fetch_artifact)
        verdict = classify_dir(args.job_dir)
        tripped = TRIPWIRE_MARKER in read_markers(Path(args.job_dir) / "markers")
        if tripped:
            print("classify: infra: cache tripwire", file=sys.stderr)
        if args.reason_file:
            Path(args.reason_file).write_text(TRIPWIRE_REASON if tripped else "", encoding="utf-8")
    except Exception as exc:
        print(f"classify: {type(exc).__name__}", file=sys.stderr)
        verdict = "test"
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
