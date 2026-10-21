from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gh_server import FakeGitHub
from tools.sim_gate import classify

CASES = json.loads((Path(__file__).parent / "fixtures" / "classify.json").read_text())
REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "gh" + "s_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"

EXPECTED = {
    "pass_full": "pass",
    "pass_without_markers": "pass",
    "success_without_test_evidence": "test",
    "test_failure": "test",
    "test_end_with_runner_lost_text": "test",
    "runner_lost_log": "infra",
    "lost_communication_log": "infra",
    "test_step_cancelled": "infra",
    "never_started_no_artifact": "infra",
    "never_started_no_steps": "infra",
    "job_cancelled_before_test": "infra",
    "build_failure": "test",
    "health_failure": "infra",
    "setup_job_failure": "infra",
    "test_step_failed_no_end_no_fingerprint": "test",
    "checkout_failure": "test",
    "garbage_job_json": "test",
    "unknown_extra_markers": "test",
    "missing_job_json": "test",
    "job_timeout": "test",
    "hygiene_failure": "infra",
    "setup_dotnet_failure": "infra",
    "cache_tripwire_over_a_test_failure": "infra",
    "cache_tripwire_over_a_success": "infra",
    "cache_tripwire_alone": "infra",
}


def case_dir(root, name):
    case = CASES[name]
    target = Path(root) / name
    target.mkdir(parents=True)
    if "job" in case:
        (target / "job.json").write_text(json.dumps(case["job"]))
    if "raw_job" in case:
        (target / "job.json").write_text(case["raw_job"])
    if case["markers"] is not None:
        (target / "markers").mkdir()
        for marker in case["markers"]:
            (target / "markers" / marker).write_text("")
    if case["log"] is not None:
        (target / "log.txt").write_text(case["log"])
    return target


def test_every_fixture_has_an_expectation():
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_verdict_per_fixture(tmp_path, case):
    assert classify.classify_dir(case_dir(tmp_path, case)) == EXPECTED[case]


def test_markers_directory_may_exist_and_be_empty(tmp_path):
    case_dir(tmp_path, "runner_lost_log").rename(tmp_path / "job")
    shutil.rmtree(tmp_path / "job" / "markers")
    (tmp_path / "job" / "markers").mkdir()
    assert classify.classify_dir(tmp_path / "job") == "infra"


def test_missing_directory_is_test(tmp_path):
    assert classify.classify_dir(tmp_path / "absent") == "test"


def test_fingerprint_match_ignores_case(tmp_path):
    case_dir(tmp_path, "runner_lost_log").rename(tmp_path / "job")
    (tmp_path / "job" / "log.txt").write_text("THE RUNNER HAS RECEIVED A SHUTDOWN SIGNAL\n")
    assert classify.classify_dir(tmp_path / "job") == "infra"


def test_blank_fingerprint_line_never_matches_everything(tmp_path):
    path = tmp_path / "fp.txt"
    path.write_text("\n  \nlost communication\n")
    assert classify.load_fingerprints(path) == ["lost communication"]
    case_dir(tmp_path, "test_step_failed_no_end_no_fingerprint").rename(tmp_path / "job")
    assert classify.classify_dir(tmp_path / "job", fingerprints=classify.load_fingerprints(path)) == "test"


def test_shipped_fingerprints_name_the_documented_runner_lost_lines():
    shipped = classify.load_fingerprints(classify.FINGERPRINTS)
    assert "the runner has received a shutdown signal" in shipped
    assert "lost communication with the server" in shipped


def run_cli(job_dir, env_extra=None, args=()):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "SIM_"))}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "tools.sim_gate.classify", "--job-dir", str(job_dir), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=30,
    )


def test_cli_prints_the_verdict_alone(tmp_path):
    out = run_cli(case_dir(tmp_path, "runner_lost_log"))
    assert (out.stdout, out.returncode) == ("infra\n", 0)
    assert run_cli(case_dir(tmp_path, "test_failure")).stdout == "test\n"
    assert run_cli(case_dir(tmp_path, "pass_full")).stdout == "pass\n"


def test_cli_on_a_missing_directory_is_test_not_a_crash(tmp_path):
    out = run_cli(tmp_path / "absent")
    assert (out.stdout, out.returncode) == ("test\n", 0)


JOBS_PATH = "/repos/owner/repo/actions/runs/42/attempts/1/jobs"
ANNOTATIONS_PATH = "/repos/owner/repo/check-runs/7/annotations?per_page=100"
ARTIFACTS_PATH = "/repos/owner/repo/actions/runs/42/artifacts?per_page=100&name=markers-unit-attempt"
TARGET_JOB = "unit / attempt / run unit-attempt"


def api_env(api):
    return {
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_API_URL": api.url,
    }


def job_entry(job_id, name, conclusion="failure"):
    return {
        "id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "steps": [{"name": "Test", "conclusion": "cancelled", "number": 1, "status": "completed"}],
        "huge_field": "x" * 50,
    }


def make_zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def serve_attempt(api, jobs, annotations=None, artifacts=None, zip_bytes=b""):
    api.serve(JOBS_PATH + "?per_page=100&page=1", json.dumps({"total_count": len(jobs), "jobs": jobs}).encode())
    api.serve(ANNOTATIONS_PATH, json.dumps(annotations or []).encode())
    api.serve(ARTIFACTS_PATH, json.dumps({"artifacts": artifacts or []}).encode())
    api.serve("/repos/owner/repo/actions/artifacts/9/zip", b"", status=302, headers={"Location": api.url + "/blob/9"})
    api.serve("/blob/9", zip_bytes)


def live_artifact(expired=False):
    return [{"id": 9, "name": "markers-unit-attempt", "expired": expired}]


@pytest.fixture
def api():
    with FakeGitHub() as server:
        yield server


def fetch(api, tmp_path):
    classify.fetch_attempt(api_env(api), tmp_path / "job", "run unit-attempt", "markers-unit-attempt")


def test_fetch_writes_the_job_the_annotations_and_the_markers(api, tmp_path):
    serve_attempt(
        api,
        [job_entry(6, "unit / attempt / run integration-attempt"), job_entry(7, TARGET_JOB)],
        annotations=[{"title": "", "message": "The runner has received a shutdown signal. x"}],
        artifacts=live_artifact(),
        zip_bytes=make_zip({"health": "", "services": "", "build": "", "test-start": ""}),
    )
    fetch(api, tmp_path)
    job_dir = tmp_path / "job"
    saved = json.loads((job_dir / "job.json").read_text())
    assert saved["id"] == 7 and "huge_field" not in saved
    assert "shutdown signal" in (job_dir / "log.txt").read_text()
    assert sorted(p.name for p in (job_dir / "markers").iterdir()) == ["build", "health", "services", "test-start"]
    assert classify.classify_dir(job_dir) == "infra"


def test_fetch_does_not_forward_the_token_to_the_artifact_blob_host(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)], artifacts=live_artifact(), zip_bytes=make_zip({"health": ""}))
    fetch(api, tmp_path)
    blob = [r for r in api.requests if r.path == "/blob/9"]
    assert len(blob) == 1 and "authorization" not in blob[0].headers
    assert all(r.headers.get("authorization") == f"Bearer {TOKEN}" for r in api.requests if r.path != "/blob/9")


def test_fetch_ignores_members_that_are_not_phase_markers(api, tmp_path):
    serve_attempt(
        api,
        [job_entry(7, TARGET_JOB)],
        artifacts=live_artifact(),
        zip_bytes=make_zip({"health": "", "../escape": "x", "sub/test-end": "", "other.txt": "x"}),
    )
    fetch(api, tmp_path)
    assert sorted(p.name for p in (tmp_path / "job" / "markers").iterdir()) == ["health"]
    assert not (tmp_path / "escape").exists()


def test_missing_artifact_leaves_no_markers_directory(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)])
    fetch(api, tmp_path)
    assert (tmp_path / "job" / "job.json").exists()
    assert not (tmp_path / "job" / "markers").exists()


def test_expired_artifact_is_treated_as_missing(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)], artifacts=live_artifact(expired=True), zip_bytes=make_zip({"health": ""}))
    fetch(api, tmp_path)
    assert not (tmp_path / "job" / "markers").exists()


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [job_entry(7, TARGET_JOB), job_entry(8, "x / run unit-attempt")],
        [job_entry(7, "unit / attempt / run unit-attempt-extra")],
    ],
)
def test_ambiguous_or_absent_job_writes_no_job_json_so_the_verdict_is_test(api, tmp_path, jobs):
    serve_attempt(api, jobs)
    fetch(api, tmp_path)
    assert not (tmp_path / "job" / "job.json").exists()
    assert classify.classify_dir(tmp_path / "job") == "test"


def test_api_failure_never_raises_and_gives_test(api, tmp_path):
    api.serve(JOBS_PATH + "?per_page=100&page=1", b"{}", status=500)
    fetch(api, tmp_path)
    assert classify.classify_dir(tmp_path / "job") == "test"


def test_missing_token_makes_no_request(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)])
    env = api_env(api)
    del env["GITHUB_TOKEN"]
    classify.fetch_attempt(env, tmp_path / "job", "run unit-attempt", "markers-unit-attempt")
    assert api.requests == []


def test_cli_fetch_prints_only_the_verdict_never_the_token(api, tmp_path):
    serve_attempt(
        api,
        [job_entry(7, TARGET_JOB)],
        annotations=[{"title": TOKEN, "message": "lost communication with the server"}],
    )
    out = run_cli(
        tmp_path / "job",
        api_env(api),
        ["--fetch-job", "run unit-attempt", "--fetch-artifact", "markers-unit-attempt"],
    )
    assert out.stdout == "infra\n"
    assert TOKEN not in out.stdout + out.stderr


def test_cli_names_the_cache_tripwire_as_the_reason_on_stderr(tmp_path):
    out = run_cli(case_dir(tmp_path, "cache_tripwire_over_a_test_failure"))
    assert out.stdout == "infra\n"
    assert "infra: cache tripwire" in out.stderr
    quiet = run_cli(case_dir(tmp_path, "runner_lost_log"))
    assert "cache tripwire" not in quiet.stderr


def test_fetch_extracts_the_cache_tripwire_marker(api, tmp_path):
    serve_attempt(
        api,
        [job_entry(7, TARGET_JOB)],
        artifacts=live_artifact(),
        zip_bytes=make_zip({"health": "", "cache-tripwire": ""}),
    )
    fetch(api, tmp_path)
    assert (tmp_path / "job" / "markers" / "cache-tripwire").exists()
    assert classify.classify_dir(tmp_path / "job") == "infra"
