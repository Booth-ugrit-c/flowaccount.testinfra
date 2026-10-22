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
from core import classify

CASES = json.loads((Path(__file__).parent / "fixtures" / "classify.json").read_text())
REPO_ROOT = Path(__file__).resolve().parents[2]
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
    "tailnet_join_failure_then_build_failure": "test",
    "tailnet_join_failure_then_pass": "pass",
    "cache_tripwire_over_a_test_failure": "infra",
    "cache_tripwire_over_a_success": "infra",
    "cache_tripwire_alone": "infra",
    "job_timeout_without_annotations": "test",
    "build_cut_by_timeout_without_annotations": "test",
    "test_step_cancelled_without_annotations": "test",
    "test_step_cancelled_with_empty_annotations": "test",
    "never_started_without_annotations": "test",
    "job_cancelled_before_test_without_annotations": "test",
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
        (target / "annotations.ok").write_text("")
    return target


def test_every_fixture_has_an_expectation():
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_verdict_per_fixture(tmp_path, case):
    assert classify.classify_dir(case_dir(tmp_path, case), extra_infra_steps=DOTNET_SETUP) == EXPECTED[case]


DOTNET_SETUP = ("Set up .NET",)


def test_core_names_no_language_setup_step_as_infra():
    assert classify.INFRA_STEPS == ("Set up job", "Hygiene", "Health", "Services")


def test_an_adapter_setup_failure_is_test_unless_the_adapter_names_it(tmp_path):
    job = case_dir(tmp_path, "setup_dotnet_failure")
    assert classify.classify_dir(job) == "test"
    assert classify.classify_dir(job, extra_infra_steps=DOTNET_SETUP) == "infra"
    assert classify.classify_dir(job, extra_infra_steps=("Set up Node",)) == "test"


def test_cli_takes_the_adapter_setup_step_as_infra(tmp_path):
    job = case_dir(tmp_path, "setup_dotnet_failure")
    assert run_cli(job).stdout == "test\n"
    assert run_cli(job, args=["--infra-step", "Set up .NET"]).stdout == "infra\n"
    assert run_cli(job, args=["--infra-step", "Set up Node", "--infra-step", "Set up .NET"]).stdout == "infra\n"


def test_cli_ignores_an_infra_step_name_outside_the_charset_and_says_so(tmp_path):
    out = run_cli(case_dir(tmp_path, "setup_dotnet_failure"), args=["--infra-step", "Set up .NET\n"])
    assert out.stdout == "test\n"
    assert "an infra step name that is the Test step or outside" in out.stderr


def test_cli_never_takes_the_test_step_as_an_infra_step(tmp_path):
    out = run_cli(case_dir(tmp_path, "test_step_failed_no_end_no_fingerprint"), args=["--infra-step", "Test"])
    assert out.stdout == "test\n"
    assert "an infra step name that is the Test step" in out.stderr


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
        [sys.executable, "-m", "core.classify", "--job-dir", str(job_dir), *args],
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


def test_cli_writes_the_reason_to_the_reason_file(tmp_path):
    reason = tmp_path / "reason.txt"
    out = run_cli(case_dir(tmp_path, "cache_tripwire_over_a_test_failure"), args=["--reason-file", str(reason)])
    assert out.stdout == "infra" + chr(10)
    assert reason.read_text() == "cache tripwire"


def test_cli_writes_an_empty_reason_file_when_there_is_no_reason(tmp_path):
    reason = tmp_path / "reason.txt"
    out = run_cli(case_dir(tmp_path, "runner_lost_log"), args=["--reason-file", str(reason)])
    assert out.stdout == "infra" + chr(10)
    assert reason.read_text() == ""


def test_a_failed_annotation_fetch_leaves_no_ok_marker_so_a_cancelled_test_step_is_test(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)])
    api.serve(ANNOTATIONS_PATH, b"{}", status=500)
    fetch(api, tmp_path)
    job_dir = tmp_path / "job"
    assert (job_dir / "job.json").exists() and not (job_dir / "annotations.ok").exists()
    assert classify.classify_dir(job_dir) == "test"


def test_a_successful_annotation_fetch_with_a_cancel_signal_makes_a_cancelled_test_step_infra(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)], annotations=[{"title": "", "message": "The operation was canceled."}])
    fetch(api, tmp_path)
    assert (tmp_path / "job" / "annotations.ok").exists()
    assert classify.classify_dir(tmp_path / "job") == "infra"


def test_a_successful_annotation_fetch_without_any_signal_leaves_a_cancelled_test_step_test(api, tmp_path):
    serve_attempt(api, [job_entry(7, TARGET_JOB)], annotations=[])
    fetch(api, tmp_path)
    assert (tmp_path / "job" / "annotations.ok").exists()
    assert classify.classify_dir(tmp_path / "job") == "test"


def test_a_cancel_signal_in_a_log_without_the_ok_marker_is_not_trusted(tmp_path):
    job = case_dir(tmp_path, "test_step_cancelled")
    assert classify.classify_dir(job) == "infra"
    (job / "annotations.ok").unlink()
    assert classify.classify_dir(job) == "test"


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(classify, "clock", clock)
    monkeypatch.setattr(classify, "sleep", clock.sleep)
    return clock


def run_job(status, conclusion):
    step = "success" if conclusion == "success" else None
    return job_entry(7, TARGET_JOB, conclusion) | {
        "status": status,
        "steps": [{"name": "Test", "conclusion": step, "number": 1, "status": status}],
    }


def serve_jobs_in_turn(api, *answers):
    reads = []

    def body():
        reads.append(len(reads))
        job = answers[min(len(reads), len(answers)) - 1]
        return b"{}" if job is None else json.dumps({"total_count": 1, "jobs": [job]}).encode()

    serve_attempt(api, [], artifacts=live_artifact(), zip_bytes=make_zip({"build": "", "test-start": "", "test-end": ""}))
    api.serve(JOBS_PATH + "?per_page=100&page=1", body)
    return reads


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
def test_a_job_read_before_its_conclusion_is_published_is_read_again_until_final(api, tmp_path, fake_clock, capsys, status):
    reads = serve_jobs_in_turn(api, run_job(status, None), run_job("completed", "success"))
    fetch(api, tmp_path)
    assert classify.classify_dir(tmp_path / "job") == "pass"
    assert len(reads) == 2 and fake_clock.sleeps == [1]
    err = capsys.readouterr().err
    assert f"classify: job not final (status={status} conclusion=null)" in err
    assert "classify: job final after 1s" in err


def test_a_final_job_on_the_first_read_is_not_read_again(api, tmp_path, fake_clock, capsys):
    reads = serve_jobs_in_turn(api, run_job("completed", "success"))
    fetch(api, tmp_path)
    assert classify.classify_dir(tmp_path / "job") == "pass"
    assert (len(reads), fake_clock.sleeps, capsys.readouterr().err) == (1, [], "")


def test_a_job_never_final_within_the_cap_is_classified_as_read_and_says_so(api, tmp_path, fake_clock, capsys):
    reads = serve_jobs_in_turn(api, run_job("in_progress", None))
    fetch(api, tmp_path)
    assert classify.classify_dir(tmp_path / "job") == "test"
    assert sum(fake_clock.sleeps) == classify.JOB_WAIT_SECONDS == 45
    assert max(fake_clock.sleeps) <= 8 and len(reads) == len(fake_clock.sleeps) + 1
    assert f"classify: job still not final after {classify.JOB_WAIT_SECONDS}s, classifying it as read" in capsys.readouterr().err


def test_an_api_failure_while_waiting_is_a_failed_lookup(api, tmp_path, fake_clock, capsys):
    reads = serve_jobs_in_turn(api, run_job("in_progress", None), None)
    fetch(api, tmp_path)
    assert len(reads) == 2 and not (tmp_path / "job" / "job.json").exists()
    assert classify.classify_dir(tmp_path / "job") == "test"
    assert "classify: job lookup failed: KeyError" in capsys.readouterr().err


LOST = "the runner has received a shutdown signal"
STALE = {"status": "queued", "conclusion": None, "steps": []}


def failed_step(name):
    return {"status": "completed", "conclusion": "failure", "steps": [{"name": name, "conclusion": "failure"}]}


@pytest.mark.parametrize(
    ("run_result", "markers", "job", "log", "expected"),
    [
        ("success", {"test-start", "test-end"}, STALE, "", "pass"),
        ("success", {"test-start", "test-end"}, None, "", "pass"),
        ("success", {"test-start"}, {"status": "completed", "conclusion": "success", "steps": [{"name": "Test", "conclusion": "success"}]}, "", "test"),
        ("success", set(), None, "", "test"),
        ("success", {"test-end", "cache-tripwire"}, None, "", "infra"),
        ("failure", {"test-start", "test-end"}, {"status": "completed", "conclusion": "success", "steps": []}, "", "test"),
        ("failure", {"health"}, failed_step("Health"), "", "infra"),
        ("failure", {"build", "test-start"}, failed_step("Test"), "", "test"),
        ("cancelled", {"build"}, STALE, LOST, "infra"),
        ("cancelled", {"build"}, STALE, "", "test"),
        ("skipped", set(), STALE, "", "test"),
    ],
)
def test_the_run_job_result_from_the_workflow_is_authoritative(run_result, markers, job, log, expected):
    assert classify.classify(markers, job, log, classify.load_fingerprints(), True, run_result) == expected


def test_a_successful_run_job_passes_on_its_markers_without_asking_the_jobs_api(api, tmp_path, fake_clock, capsys):
    reads = serve_jobs_in_turn(api, run_job("queued", None))
    classify.fetch_attempt(api_env(api), tmp_path / "job", "run unit-attempt", "markers-unit-attempt", "success")
    assert classify.classify_dir(tmp_path / "job", run_result="success") == "pass"
    assert (reads, fake_clock.sleeps, capsys.readouterr().err) == ([], [], "")
    assert not (tmp_path / "job" / "job.json").exists()


def test_cli_passes_a_successful_run_job_while_the_jobs_api_still_says_queued(api, tmp_path):
    serve_jobs_in_turn(api, run_job("queued", None))
    args = ["--fetch-job", "run unit-attempt", "--fetch-artifact", "markers-unit-attempt", "--run-result", "success"]
    out = run_cli(tmp_path / "job", api_env(api), args)
    assert (out.stdout, out.stderr) == ("pass\n", "")
    assert not any(path.startswith(JOBS_PATH) for path in api.paths())


def test_cli_without_the_test_end_marker_is_test_even_when_the_run_job_succeeded(api, tmp_path):
    serve_attempt(api, [], artifacts=live_artifact(), zip_bytes=make_zip({"build": "", "test-start": ""}))
    args = ["--fetch-job", "run unit-attempt", "--fetch-artifact", "markers-unit-attempt", "--run-result", "success"]
    assert run_cli(tmp_path / "job", api_env(api), args).stdout == "test\n"


def test_a_failed_run_job_with_a_stale_jobs_api_waits_then_stays_fail_closed(api, tmp_path, fake_clock, capsys):
    reads = serve_jobs_in_turn(api, run_job("in_progress", None))
    classify.fetch_attempt(api_env(api), tmp_path / "job", "run unit-attempt", "markers-unit-attempt", "failure")
    assert classify.classify_dir(tmp_path / "job", run_result="failure") == "test"
    assert len(reads) > 1 and sum(fake_clock.sleeps) == classify.JOB_WAIT_SECONDS
    assert "classify: job still not final" in capsys.readouterr().err


def test_cli_ignores_an_unknown_run_result_and_says_so(tmp_path):
    out = run_cli(case_dir(tmp_path, "pass_full"), args=["--run-result", "neutral"])
    assert out.stdout == "pass\n"
    assert "classify: unknown run result" in out.stderr


@pytest.mark.parametrize(
    ("run_result", "markers", "job", "infra", "expected"),
    [
        ("success", {"health", "build"}, None, (), "pass"),
        ("success", {"health", "test-end"}, None, (), "test"),
        ("success", {"health"}, None, (), "test"),
        ("failure", {"health", "build"}, failed_step("Save cache"), (), "test"),
        ("failure", {"health"}, failed_step("Upload build"), ("Upload build",), "infra"),
        ("failure", {"health"}, failed_step("Build"), ("Upload build",), "test"),
    ],
)
def test_a_build_job_is_done_at_its_build_marker(run_result, markers, job, infra, expected):
    infra_steps = classify.INFRA_STEPS + infra
    assert classify.classify(markers, job, "", [], True, run_result, infra_steps, "build") == expected


def build_markers(tmp_path, *names):
    markers = tmp_path / "job" / "markers"
    markers.mkdir(parents=True)
    for name in names:
        (markers / name).write_text("")
    return tmp_path / "job"


def test_cli_takes_the_done_marker_from_the_adapter(tmp_path):
    job = build_markers(tmp_path, "health", "build")
    assert run_cli(job, args=["--run-result", "success"]).stdout == "test\n"
    assert run_cli(job, args=["--run-result", "success", "--done-marker", "build"]).stdout == "pass\n"


@pytest.mark.parametrize("marker", ["health", "test-start", "cache-tripwire", ""])
def test_cli_refuses_a_done_marker_that_is_not_a_phase_end(tmp_path, marker):
    out = run_cli(build_markers(tmp_path, "health", marker or "x"), args=["--run-result", "success", "--done-marker", marker])
    assert out.returncode != 0
    assert "pass" not in out.stdout
