from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gh_server import FakeGitHub
from core import route

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "route.json").read_text())
REPO = "owner/repo"
LABEL = "lab-pool"
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
RUNNERS_PATH = f"/repos/{REPO}/actions/runners"


def fixture(name):
    return json.dumps(FIXTURES[name.removesuffix(".json")]).encode()


def make_env(api, **extra):
    env = {
        "SIM_ROUTE_TOKEN": TOKEN,
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_API_URL": api.url,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }
    env.update(extra)
    return env


@pytest.fixture
def api():
    with FakeGitHub() as server:
        yield server


def event_file(tmp_path, payload):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    return str(path)


@pytest.mark.parametrize(
    "body, expected",
    [
        ("idle.json", "onprem"),
        ("busy.json", "cloud"),
        ("offline.json", "cloud"),
        ("other_label.json", "cloud"),
        ("busy_and_idle.json", "onprem"),
        ("idle_other_label_busy_target.json", "cloud"),
        ("no_busy_field.json", "cloud"),
        ("empty.json", "cloud"),
        ("upper_label.json", "onprem"),
    ],
)
def test_decision_from_runner_list(api, body, expected):
    api.serve(RUNNERS_PATH, fixture(body))
    assert route.decide(make_env(api), LABEL) == expected


def test_request_is_one_authenticated_get_of_the_runners_endpoint(api):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    route.decide(make_env(api), LABEL)
    assert api.paths() == [RUNNERS_PATH + "?per_page=100&page=1"]
    assert api.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_second_page_is_read_when_the_first_is_not_complete(api):
    first = json.dumps(
        {"total_count": 2, "runners": [{"status": "online", "busy": True, "labels": [{"name": LABEL}]}]}
    ).encode()
    second = json.dumps(
        {"total_count": 2, "runners": [{"status": "online", "busy": False, "labels": [{"name": LABEL}]}]}
    ).encode()
    api.serve(RUNNERS_PATH + "?per_page=100&page=1", first)
    api.serve(RUNNERS_PATH + "?per_page=100&page=2", second)
    assert route.decide(make_env(api), LABEL) == "onprem"


def test_missing_token_is_cloud_without_a_request(api):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    env = make_env(api)
    del env["SIM_ROUTE_TOKEN"]
    assert route.decide(env, LABEL) == "cloud"
    assert api.requests == []


def test_empty_token_is_cloud_without_a_request(api):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    assert route.decide(make_env(api, SIM_ROUTE_TOKEN=""), LABEL) == "cloud"
    assert api.requests == []


def test_empty_label_is_cloud_without_a_request(api):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    assert route.decide(make_env(api), "") == "cloud"
    assert api.requests == []


@pytest.mark.parametrize("repo", ["", "no-slash", "a/b/c", "owner/re po", "../x"])
def test_malformed_repository_is_cloud_without_a_request(api, repo):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    assert route.decide(make_env(api, GITHUB_REPOSITORY=repo), LABEL) == "cloud"
    assert api.requests == []


def test_fork_pull_request_is_cloud_without_a_request(api, tmp_path):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    payload = {"pull_request": {"head": {"repo": {"full_name": "someone/repo"}}}}
    env = make_env(api, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=event_file(tmp_path, payload))
    assert route.decide(env, LABEL) == "cloud"
    assert api.requests == []


def test_same_repository_pull_request_may_route_onprem(api, tmp_path):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    payload = {"pull_request": {"head": {"repo": {"full_name": REPO}}}}
    env = make_env(api, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=event_file(tmp_path, payload))
    assert route.decide(env, LABEL) == "onprem"


@pytest.mark.parametrize("payload", [{}, {"pull_request": {"head": {"repo": None}}}, {"pull_request": None}])
def test_pull_request_with_unreadable_head_repository_is_cloud(api, tmp_path, payload):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    env = make_env(api, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=event_file(tmp_path, payload))
    assert route.decide(env, LABEL) == "cloud"
    assert api.requests == []


def test_pull_request_without_an_event_file_is_cloud(api, tmp_path):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    env = make_env(api, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(tmp_path / "absent.json"))
    assert route.decide(env, LABEL) == "cloud"
    assert api.requests == []


@pytest.mark.parametrize("event", ["pull_request_target", "pull_request_review"])
def test_every_pull_request_event_family_checks_the_head_repository(api, tmp_path, event):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    payload = {"pull_request": {"head": {"repo": {"full_name": "someone/repo"}}}}
    env = make_env(api, GITHUB_EVENT_NAME=event, GITHUB_EVENT_PATH=event_file(tmp_path, payload))
    assert route.decide(env, LABEL) == "cloud"


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502])
def test_http_error_is_cloud(api, status):
    api.serve(RUNNERS_PATH, fixture("idle.json"), status=status)
    assert route.decide(make_env(api), LABEL) == "cloud"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"runners": "x"}', b'{"runners": [1]}', b""])
def test_unreadable_body_is_cloud(api, body):
    api.serve(RUNNERS_PATH, body)
    assert route.decide(make_env(api), LABEL) == "cloud"


def test_timeout_is_cloud(api, monkeypatch):
    monkeypatch.setattr(route, "TIMEOUT_SECONDS", 0.3)
    api.serve(RUNNERS_PATH, lambda: (time.sleep(1.5), fixture("idle.json"))[1])
    started = time.monotonic()
    assert route.decide(make_env(api), LABEL) == "cloud"
    assert time.monotonic() - started < 1.4


def test_unreachable_api_is_cloud():
    env = {
        "SIM_ROUTE_TOKEN": TOKEN,
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_API_URL": "http://127.0.0.1:9",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }
    assert route.decide(env, LABEL) == "cloud"


def test_redirect_is_not_followed_so_the_token_stays_on_the_api_host(api):
    api.serve(RUNNERS_PATH, b"", status=302, headers={"Location": api.url + "/elsewhere"})
    api.serve("/elsewhere", fixture("idle.json"))
    assert route.decide(make_env(api), LABEL) == "cloud"
    assert "/elsewhere" not in api.paths()


@pytest.mark.parametrize("url", ["http://api.example.test", "ftp://api.example.test", "api.example.test", ""])
def test_token_is_never_sent_over_a_non_https_remote_api(url, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "fetch_page", lambda *args: calls.append(args) or ([], 0))
    env = {"SIM_ROUTE_TOKEN": TOKEN, "GITHUB_REPOSITORY": REPO, "GITHUB_API_URL": url, "GITHUB_EVENT_NAME": "x"}
    assert route.decide(env, LABEL) == "cloud"
    assert calls == []


def run_cli(api, tmp_path, token=TOKEN, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "SIM_"))}
    env.update(make_env(api, SIM_ROUTE_TOKEN=token))
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, "-m", "core.route", LABEL],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        timeout=30,
    )


def test_cli_prints_exactly_one_word_and_never_the_token_or_the_body(api, tmp_path):
    api.serve(RUNNERS_PATH, fixture("idle.json"))
    ok = run_cli(api, tmp_path)
    assert ok.stdout == "onprem\n"
    api.serve(RUNNERS_PATH, b'{"detail":"BODY-MARKER ' + TOKEN.encode() + b'"}', status=500)
    bad = run_cli(api, tmp_path)
    assert bad.stdout == "cloud\n"
    for result in (ok, bad):
        assert result.returncode == 0
        assert TOKEN not in result.stdout + result.stderr
        assert "BODY-MARKER" not in result.stdout + result.stderr
        assert "runners" not in result.stdout + result.stderr


def test_cli_without_a_label_argument_is_cloud(api):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "SIM_"))}
    env.update(make_env(api))
    out = subprocess.run(
        [sys.executable, "-m", "core.route"],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        timeout=30,
    )
    assert out.stdout == "cloud\n"
    assert out.returncode == 0
