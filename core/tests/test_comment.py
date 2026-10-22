from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from core import comment

ROOT = Path(__file__).resolve().parents[2]
REPO = "o/r"
SERVER = "https://github.example"
RUN_URL = f"{SERVER}/{REPO}/actions/runs/200"
MARKER = "sim-gate:dotnet"
HEADER = f"<!-- {MARKER} -->"
TOKEN = "tok-SENTINEL-4f1c"
SHA_A = "a" * 40
SHA_B = "b" * 40
BOT = {"login": "github-actions[bot]", "type": "Bot"}


def zipped(document):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("verdict.json", json.dumps(document))
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self):
        self.comments = []
        self.artifacts = {}
        self.fail = {}
        self.requests = []
        self.next_id = 1000
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else None
                parent.requests.append((method, self.path, payload, dict(self.headers)))
                status, body, headers = parent.route(method, self.path, payload)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.answer("GET")

            def do_POST(self):
                self.answer("POST")

            def do_PATCH(self):
                self.answer("PATCH")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, raw_path, payload):
        parts = urlsplit(raw_path)
        path, query = parts.path, parse_qs(parts.query)
        for (fail_method, prefix), status in self.fail.items():
            if method == fail_method and path.startswith(prefix):
                return status, b"{}", {}
        if match := re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", path):
            if method == "GET":
                page = int(query.get("page", ["1"])[0])
                return 200, json.dumps(self.comments[(page - 1) * 100 : page * 100]).encode(), {}
            if method == "POST":
                self.next_id += 1
                self.comments.append({"id": self.next_id, "body": payload["body"], "user": dict(BOT)})
                return 201, json.dumps(self.comments[-1]).encode(), {}
        if (match := re.fullmatch(rf"/repos/{REPO}/issues/comments/(\d+)", path)) and method == "PATCH":
            for item in self.comments:
                if item["id"] == int(match.group(1)):
                    item["body"] = payload["body"]
                    return 200, json.dumps(item).encode(), {}
            return 404, b"{}", {}
        if match := re.fullmatch(rf"/repos/{REPO}/actions/runs/(\d+)/artifacts", path):
            run_id = match.group(1)
            listed = []
            if run_id in self.artifacts:
                size = len(self.artifacts[run_id])
                listed = [{"id": int(run_id) + 5000, "name": "sim-gate-verdict", "expired": False, "size_in_bytes": size}]
            return 200, json.dumps({"total_count": len(listed), "artifacts": listed}).encode(), {}
        if match := re.fullmatch(rf"/repos/{REPO}/actions/artifacts/(\d+)/zip", path):
            return 302, b"", {"Location": f"{self.url}/blob/{int(match.group(1)) - 5000}"}
        if match := re.fullmatch(r"/blob/(\d+)", path):
            return 200, self.artifacts[match.group(1)], {}
        return 404, b"{}", {}

    def methods(self, prefix):
        return [method for method, path, _, _ in self.requests if path.startswith(prefix) and method != "GET"]


def lane(name, verdict, reason="", executed=None):
    return {"name": name, "verdict": verdict, "reason": reason, "executed": executed}


def make_state(**changes):
    state = {
        "schema": 1,
        "stack": "dotnet",
        "run_id": "200",
        "run_attempt": 1,
        "retest": False,
        "retest_of": "",
        "result": "pass",
        "retest_lanes": [],
        "inputs": {
            "pr_number": "7",
            "pr_branch": "feature-a",
            "pr_sha": SHA_A,
            "pr_author": "someone",
            "base_sha": SHA_B,
            "cache": "use",
            "fault": "",
        },
        "lanes": [lane("build", "pass"), lane("unit", "pass", executed=12)],
    }
    state.update(changes)
    return state


@pytest.fixture
def github(monkeypatch):
    with FakeGitHub() as fake:
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setenv("GITHUB_API_URL", fake.url)
        monkeypatch.setenv("GITHUB_SERVER_URL", SERVER)
        yield fake


def upsert(tmp_path, state, marker=MARKER, run_url=RUN_URL, pr="7"):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return comment.main(["upsert", "--pr", pr, "--marker", marker, "--state", str(path), "--run-url", run_url])


def table_rows(body):
    return [line for line in body.splitlines() if line.startswith("| ") and not line.startswith("| Lane ")]


def unescaped_pipes(line):
    return len(re.findall(r"(?<!\\)\|", line))


def test_body_starts_with_the_marker(github, tmp_path):
    assert upsert(tmp_path, make_state()) == 0
    assert github.comments[0]["body"].startswith(HEADER + "\n")


def test_first_upsert_posts_and_second_patches_the_same_comment(github, tmp_path):
    assert upsert(tmp_path, make_state()) == 0
    assert upsert(tmp_path, make_state(result="red [test]", lanes=[lane("build", "pass"), lane("unit", "test", executed=12)])) == 0
    assert github.methods(f"/repos/{REPO}/issues") == ["POST", "PATCH"]
    marked = [c for c in github.comments if c["body"].startswith(HEADER)]
    assert len(marked) == 1
    assert "red [test]" in marked[0]["body"]


def test_a_user_comment_carrying_the_marker_is_never_edited(github, tmp_path):
    github.comments.append({"id": 5, "body": HEADER + "\nmine", "user": {"login": "someone", "type": "User"}})
    assert upsert(tmp_path, make_state()) == 0
    assert github.comments[0]["body"] == HEADER + "\nmine"
    assert github.methods(f"/repos/{REPO}/issues") == ["POST"]


def test_another_bot_with_the_marker_is_never_edited(github, tmp_path):
    github.comments.append({"id": 5, "body": HEADER + "\nother", "user": {"login": "other-app[bot]", "type": "Bot"}})
    assert upsert(tmp_path, make_state()) == 0
    assert github.comments[0]["body"] == HEADER + "\nother"
    assert github.methods(f"/repos/{REPO}/issues") == ["POST"]


def test_the_bot_login_on_a_user_account_is_never_edited(github, tmp_path):
    github.comments.append({"id": 5, "body": HEADER + "\nfake", "user": {"login": "github-actions[bot]", "type": "User"}})
    assert upsert(tmp_path, make_state()) == 0
    assert github.comments[0]["body"] == HEADER + "\nfake"
    assert github.methods(f"/repos/{REPO}/issues") == ["POST"]


def test_the_oldest_own_comment_is_patched_and_found_on_a_later_page(github, tmp_path):
    github.comments += [{"id": n, "body": "chatter", "user": {"login": "someone", "type": "User"}} for n in range(1, 151)]
    github.comments += [{"id": 400, "body": HEADER + "\nold", "user": dict(BOT)}, {"id": 300, "body": HEADER + "\nolder", "user": dict(BOT)}]
    assert upsert(tmp_path, make_state()) == 0
    assert github.methods(f"/repos/{REPO}/issues") == ["PATCH"]
    assert [path for method, path, _, _ in github.requests if method == "PATCH"] == [f"/repos/{REPO}/issues/comments/300"]


def test_cell_escapes_pipe_and_newlines():
    assert comment.cell("a|b\nc\r\nd\re") == "a\\|b c d e"


def test_a_pipe_or_newline_in_a_cell_keeps_five_columns():
    state = make_state(lanes=[lane("unit", "infra", reason="x|y\nz|w", executed=3)])
    body = comment.render(MARKER, state, RUN_URL)
    rows = table_rows(body)
    assert len(rows) == 1
    assert unescaped_pipes(rows[0]) == 6


def test_body_is_capped_below_the_api_limit_and_keeps_the_marker():
    state = make_state(lanes=[lane("unit", "infra", reason="r" * 70000, executed=3)])
    body = comment.render(MARKER, state, RUN_URL)
    assert len(body) < 65536
    assert body.startswith(HEADER + "\n")
    assert body.endswith(comment.TRUNCATED)


def test_a_retest_keeps_rows_of_lanes_it_did_not_rerun(github, tmp_path):
    prior = make_state(
        run_id="100",
        result="red [infra failed 1x]",
        retest_lanes=["unit"],
        lanes=[lane("build", "pass"), lane("unit", "infra"), lane("integration", "pass", executed=4)],
    )
    github.artifacts["100"] = zipped(prior)
    retest = make_state(retest=True, retest_of="100", lanes=[lane("build", "pass"), lane("unit", "pass", executed=12)])
    assert upsert(tmp_path, retest) == 0
    body = github.comments[0]["body"]
    rows = table_rows(body)
    assert [row.split("|")[1].strip() for row in rows] == ["build", "unit", "integration"]
    assert f"[previous run]({SERVER}/{REPO}/actions/runs/100)" in rows[2] and "| 4 |" in rows[2]
    assert f"[this run]({RUN_URL})" in rows[1] and "| pass |" in rows[1]
    assert "(Retest)" in body.splitlines()[1]


def test_a_retest_drops_prior_rows_from_another_commit(github, tmp_path):
    prior = make_state(run_id="100", lanes=[lane("build", "pass"), lane("integration", "pass", executed=4)])
    prior["inputs"]["pr_sha"] = SHA_B
    github.artifacts["100"] = zipped(prior)
    assert upsert(tmp_path, make_state(retest=True, retest_of="100")) == 0
    body = github.comments[0]["body"]
    assert "integration" not in body
    assert "different commit" in body


def test_an_unreadable_prior_run_still_writes_the_comment(github, tmp_path):
    assert upsert(tmp_path, make_state(retest=True, retest_of="100")) == 0
    assert "could not be read" in github.comments[0]["body"]


def test_a_first_run_never_reads_a_prior_run(github, tmp_path):
    assert upsert(tmp_path, make_state()) == 0
    assert not [path for _, path, _, _ in github.requests if "/actions/" in path]


def test_an_api_error_exits_one_without_printing_the_token(github, tmp_path, capsys):
    github.fail[("GET", f"/repos/{REPO}/issues/7/comments")] = 500
    assert upsert(tmp_path, make_state()) == 1
    out, err = capsys.readouterr()
    assert "comment: failed: HTTPError 500" in err
    assert TOKEN not in out + err


def test_the_token_is_sent_only_as_a_bearer_header(github, tmp_path, capsys):
    assert upsert(tmp_path, make_state()) == 0
    out, err = capsys.readouterr()
    assert TOKEN not in out + err
    assert all(TOKEN not in path and TOKEN not in json.dumps(payload) for _, path, payload, _ in github.requests)
    assert {headers.get("Authorization") for _, _, _, headers in github.requests} == {f"Bearer {TOKEN}"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"marker": "sim-gate:workspace"},
        {"marker": "sim-gate:Dot net"},
        {"run_url": "https://elsewhere.example/o/r/actions/runs/200"},
        {"run_url": f"{SERVER}/{REPO}/actions/runs/200\nx"},
        {"pr": "7 "},
        {"pr": "0"},
    ],
)
def test_bad_arguments_are_refused_before_any_write(github, tmp_path, kwargs):
    assert upsert(tmp_path, make_state(), **kwargs) == 1
    assert github.requests == []


def test_a_bad_state_is_refused_before_any_write(github, tmp_path):
    assert upsert(tmp_path, make_state(lanes=[lane("a|b", "pass")])) == 1
    assert github.requests == []


def test_a_missing_token_is_refused(github, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    assert upsert(tmp_path, make_state()) == 1
    assert github.requests == []


def test_parse_state_rejects_unknown_keys_and_inconsistent_retest():
    with pytest.raises(comment.Refused):
        comment.parse_state(json.dumps(make_state(extra=1)))
    with pytest.raises(comment.Refused):
        comment.parse_state(json.dumps(make_state(retest_of="100")))
    with pytest.raises(comment.Refused):
        comment.parse_state(json.dumps(make_state(retest_lanes=["missing"])))
    assert comment.parse_state(json.dumps(make_state(retest=True, run_attempt=2)))["retest"] is True


@pytest.mark.parametrize("name", ["core/comment.py", "core/retest_sim.py"])
def test_tools_import_only_the_standard_library_and_core(name):
    tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
    modules = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    modules += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
    outside = [m for m in modules if m.split(".")[0] not in sys.stdlib_module_names and m.split(".")[0] not in ("core", "__future__")]
    assert modules and outside == []


def test_cli_entry_point_creates_the_comment(github, tmp_path):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(make_state()), encoding="utf-8")
    env = {**os.environ, "GITHUB_TOKEN": TOKEN, "GITHUB_REPOSITORY": REPO, "GITHUB_API_URL": github.url, "GITHUB_SERVER_URL": SERVER}
    result = subprocess.run(
        [sys.executable, "-m", "core.comment", "upsert", "--pr", "7", "--marker", MARKER, "--state", str(path), "--run-url", RUN_URL],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "comment: created comment on #7"
    assert TOKEN not in result.stdout + result.stderr
