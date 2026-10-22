from __future__ import annotations

import pytest

from core import prior
from test_retest_sim import REPO, SHA_A, SHA_B, TOKEN, FakeGitHub, gate_jobs, lane, make_run, make_state, upload_step, zipped


@pytest.fixture
def github(monkeypatch, tmp_path):
    with FakeGitHub() as fake:
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setenv("GITHUB_API_URL", fake.url)
        monkeypatch.setenv("GITHUB_SHA", SHA_A)
        monkeypatch.setenv("GITHUB_RUN_ID", "200")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
        fake.runs["100"] = [make_run()]
        fake.artifacts["100"] = zipped(make_state())
        fake.jobs["100"] = [gate_jobs()]
        fake.output = tmp_path / "output.txt"
        yield fake


def check(capsys, run_id="100", unit="true", integration="false", wait="0.2"):
    code = prior.main(["check", "--run-id", run_id, "--workflow", "sim-gate.yml", "--stack", "dotnet",
                       "--selected", f"unit={unit}", "--selected", f"integration={integration}", "--wait", wait, "--poll", "0.05"])
    out, err = capsys.readouterr()
    assert TOKEN not in out + err
    return code, out


def refused(github, capsys, reason, **kwargs):
    code, out = check(capsys, **kwargs)
    assert code == 1
    assert out.startswith("prior: refused: ") and reason in out, out
    assert not github.output.exists() or "prior_" not in github.output.read_text()


def test_the_stand_in_shape_is_verified_and_passes_the_lane_not_rerun(github, capsys):
    code, out = check(capsys)
    assert code == 0, out
    assert out.strip() == "prior: verified run 100 attempt 1; not rerun: integration"
    assert github.output.read_text() == "prior_integration=pass\n"
    assert any(p.endswith("/actions/runs/100/attempts/1") for _, p, _, _ in github.requests)


def test_a_retest_of_every_lane_writes_no_prior_lane(github, capsys):
    github.artifacts["100"] = zipped(make_state(retest_lanes=["build", "unit", "integration"], lanes=[lane("build", "infra"), lane("unit", "none"), lane("integration", "none")]))
    code, out = check(capsys, integration="true")
    assert code == 0, out
    assert "not rerun: none" in out


def test_a_lane_not_rerun_that_did_not_pass_is_refused(github, capsys):
    github.artifacts["100"] = zipped(make_state(lanes=[lane("build", "pass"), lane("unit", "infra"), lane("integration", "test")]))
    refused(github, capsys, "lane integration is not rerun and did not pass")


def test_a_previous_run_without_a_verdict_is_refused(github, capsys):
    github.artifacts.pop("100")
    refused(github, capsys, "artifact count")


def test_a_previous_run_on_another_commit_is_refused(github, capsys):
    github.runs["100"] = [make_run(head_sha=SHA_B)]
    refused(github, capsys, "another commit")


def test_a_previous_verdict_for_another_commit_is_refused(github, capsys):
    state = make_state()
    state["inputs"]["pr_sha"] = SHA_B
    github.artifacts["100"] = zipped(state)
    refused(github, capsys, "another stack or commit")


def test_a_previous_run_that_is_itself_a_retest_is_refused(github, capsys):
    github.artifacts["100"] = zipped(make_state(retest=True, retest_of="99", result="red [infra failed 2x]"))
    refused(github, capsys, "itself a retest")


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"path": ".github/workflows/other.yml"}, "not from the workflow"),
        ({"event": "push"}, "not dispatched"),
        ({"conclusion": "success"}, "conclusion is 'success'"),
        ({"id": 101}, "run id"),
    ],
)
def test_an_unexpected_previous_run_is_refused(github, capsys, changes, reason):
    github.runs["100"] = [make_run(**changes)]
    refused(github, capsys, reason)


def test_a_previous_run_that_never_completes_is_refused(github, capsys):
    github.runs["100"] = [make_run(status="in_progress", conclusion=None)]
    refused(github, capsys, "previous run not completed")


def test_a_previous_verdict_that_disagrees_with_its_gate_job_is_refused(github, capsys):
    github.jobs["100"] = [gate_jobs("success")]
    refused(github, capsys, "gate result")


def test_a_previous_verdict_not_uploaded_by_its_gate_step_is_refused(github, capsys):
    github.jobs["100"] = [gate_jobs(steps=[upload_step(started_at="2026-01-02T03:10:06Z", completed_at="2026-01-02T03:10:08Z")])]
    refused(github, capsys, "artifact time")


def test_a_previous_run_that_selected_other_lanes_is_refused(github, capsys):
    github.artifacts["100"] = zipped(make_state(lanes=[lane("build", "pass"), lane("unit", "infra")]))
    refused(github, capsys, "selected other lanes")


def test_a_lane_to_retest_that_is_not_selected_is_refused(github, capsys):
    github.artifacts["100"] = zipped(make_state(retest_lanes=["unit", "integration"]))
    refused(github, capsys, "a lane to retest is not selected")


def test_a_run_cannot_retest_itself(github, capsys):
    refused(github, capsys, "cannot retest itself", run_id="200")
    assert github.requests == []


@pytest.mark.parametrize("run_id", ["0100", "", "1x"])
def test_a_malformed_retest_of_is_refused(github, capsys, run_id):
    refused(github, capsys, "retest_of", run_id=run_id)


def test_an_api_error_is_refused(github, capsys):
    github.fail[("GET", f"/repos/{REPO}/actions/runs/100/attempts/1/jobs")] = 500
    refused(github, capsys, "HTTPError 500")


@pytest.mark.parametrize("argv", [["--selected", "build=true"], ["--selected", "unit=maybe"], ["--selected", "unit=true", "--selected", "unit=false"], []])
def test_bad_lane_arguments_are_refused(github, argv):
    with pytest.raises(SystemExit) as raised:
        prior.main(["check", "--run-id", "100", "--workflow", "sim-gate.yml", "--stack", "dotnet", *argv])
    assert raised.value.code == 2
    assert github.requests == []
