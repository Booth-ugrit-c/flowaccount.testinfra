from __future__ import annotations

import json

import pytest

from trials.calibration import cli
from trials.calibration.config import REQUIRED_KEYS
from trials.calibration.ghapi import GhError
from trials.calibration.tests.helpers import FakeGh, client_for, config_dict, make_job, make_run, standard_world


def invoke(tmp_path, monkeypatch, cfg, fake, retries=2):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    client, slept = client_for(fake, retries=retries)
    monkeypatch.setattr(cli, "GhClient", lambda: client)
    out = tmp_path / "out"
    return cli.main(["extract", "--config", str(cfg_path), "--out", str(out)]), out, slept, client


def test_zero_match_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch, capsys):
    runs, jobs = standard_world(3)
    cfg = config_dict(runners=["no-such-runner"], manual_runners=[])
    code, out, _, _ = invoke(tmp_path, monkeypatch, cfg, FakeGh(runs, jobs))
    assert code != 0
    assert not out.exists()
    assert "matched zero" in capsys.readouterr().err


def test_zero_runs_in_window_also_fails(tmp_path, monkeypatch):
    code, out, _, _ = invoke(tmp_path, monkeypatch, config_dict(), FakeGh([], {}))
    assert code != 0 and not out.exists()


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_missing_config_key_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch, capsys, key):
    runs, jobs = standard_world(1)
    cfg = config_dict()
    del cfg[key]
    code, out, _, client = invoke(tmp_path, monkeypatch, cfg, FakeGh(runs, jobs))
    assert code == 2 and not out.exists()
    assert key in capsys.readouterr().err
    assert client.calls == 0


def test_api_failure_after_retries_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch, capsys):
    runs, jobs = standard_world(1)
    fake = FakeGh(runs, jobs)
    fake.always_fail = GhError("HTTP 502: bad gateway")
    code, out, slept, client = invoke(tmp_path, monkeypatch, config_dict(), fake, retries=2)
    assert code == 3 and not out.exists()
    assert client.calls == 3
    assert slept == [1.0, 2.0]
    assert "after 2 retries" in capsys.readouterr().err


def test_transient_failure_is_retried_and_the_run_completes(tmp_path, monkeypatch):
    runs, jobs = standard_world(1)
    fake = FakeGh(runs, jobs)
    fake.failures = [GhError("HTTP 500: boom")]
    code, out, slept, _ = invoke(tmp_path, monkeypatch, config_dict(), fake)
    assert code == 0 and (out / "today.json").exists()
    assert slept == [1.0]


def test_rate_limit_sleeps_until_reset_then_continues(tmp_path, monkeypatch, capsys):
    runs, jobs = standard_world(2)
    fake = FakeGh(runs, jobs)
    fake.reset_epoch = 30
    fake.failures = [GhError("HTTP 403: API rate limit exceeded for user")]
    code, out, slept, client = invoke(tmp_path, monkeypatch, config_dict(), fake)
    today = json.loads((out / "today.json").read_text("utf-8"))
    assert code == 0
    assert slept == [31.0]
    assert (client.rate_probes, client.rate_waits) == (1, 1)
    assert len(today["gates"]) == 2


def test_persistent_rate_limit_fails_instead_of_skipping(tmp_path, monkeypatch, capsys):
    runs, jobs = standard_world(1)
    fake = FakeGh(runs, jobs)
    fake.always_fail = GhError("HTTP 403: API rate limit exceeded")
    code, out, _, _ = invoke(tmp_path, monkeypatch, config_dict(), fake)
    assert code == 3 and not out.exists()


def test_not_found_is_not_retried(tmp_path, monkeypatch):
    runs, jobs = standard_world(1)
    fake = FakeGh(runs, jobs)
    fake.always_fail = GhError("gh: Not Found (HTTP 404)")
    code, out, slept, client = invoke(tmp_path, monkeypatch, config_dict(), fake)
    assert code == 3 and client.calls == 1 and slept == []


def test_invalid_window_and_service_type_rejected(tmp_path, monkeypatch, capsys):
    runs, jobs = standard_world(1)
    bad_window = config_dict(window={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"})
    assert invoke(tmp_path, monkeypatch, bad_window, FakeGh(runs, jobs))[0] == 2
    bad_service = config_dict(service_patterns=[{"pattern": "x", "services": ["blob"]}])
    assert invoke(tmp_path, monkeypatch, bad_service, FakeGh(runs, jobs))[0] == 2


def test_truncated_run_listing_fails_instead_of_dropping_runs(tmp_path, monkeypatch, capsys):
    runs = [make_run(100 + n, n * 60) for n in range(5)]
    jobs = {r["id"]: [make_job("unit / tests", "lab-runner-a", 0, 5, 65)] for r in runs}
    code, out, _, _ = invoke(tmp_path, monkeypatch, config_dict(workflows=["pipeline.yml"]), FakeGh(runs, jobs, cap=3))
    assert code == 3 and not out.exists()
    assert "run listing incomplete: 3 of 5" in capsys.readouterr().err


def test_truncated_job_listing_fails(tmp_path, monkeypatch, capsys):
    runs, jobs = standard_world(1)
    fake = FakeGh(runs, jobs)
    real = fake.respond

    def short(path):
        pages = real(path)
        if path.split("?")[0].endswith("/jobs"):
            pages[0]["total_count"] += 1
        return pages

    fake.respond = short
    code, out, _, _ = invoke(tmp_path, monkeypatch, config_dict(), fake)
    assert code == 3 and not out.exists()


def test_skipped_job_has_null_timings_even_with_a_runner_name(tmp_path, monkeypatch):
    runs = [make_run(1, 0)]
    jobs = {1: [make_job("unit / tests", "lab-runner-a", 0, 5, 65, conclusion="skipped")]}
    code, out, _, _ = invoke(tmp_path, monkeypatch, config_dict(), FakeGh(runs, jobs))
    job = json.loads((out / "today.json").read_text("utf-8"))["gates"][0]["jobs"][0]
    assert code == 0 and (job["duration_s"], job["queue_wait_s"]) == (None, None)
