import base64
import hashlib
import http.server
import io
import json
import os
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from adapters.workspace import cache

ROOT = "sim/workspace"
REPO_ROOT = Path(__file__).resolve().parents[3]
READ_ID, WRITE_ID = "reader-id-0001", "writer-id-0001"
ENV_NAMES = ("SIM_CACHE_ENDPOINT", "SIM_CACHE_REGION", "SIM_CACHE_READ_KEY_ID", "SIM_CACHE_READ_SECRET",
             "SIM_CACHE_WRITE_KEY_ID", "SIM_CACHE_WRITE_SECRET", "RUNNER_ENVIRONMENT", "SIM_DEFAULT_BRANCH")
GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
       "-c", "core.autocrlf=false"]
TOOLS = ("--node", "22", "--npm", "10")


def integrity(text):
    return "sha512-" + base64.b64encode(hashlib.sha512(text.encode()).digest()).decode()


def lockfile(**override):
    entry = {"version": "1.0.0", "resolved": "https://registry.example.test/a/-/a-1.0.0.tgz",
             "integrity": integrity("a")}
    entry.update(override)
    return {"lockfileVersion": 3, "packages": {
        "": {"name": "w", "workspaces": ["libs/*"]},
        "libs/one": {"name": "one"},
        "node_modules/one": {"resolved": "libs/one", "link": True},
        "node_modules/a": entry,
        "node_modules/a/node_modules/b": {"resolved": "https://registry.example.test/b/-/b-1.0.0.tgz",
                                          "integrity": integrity("b")},
    }}


class FakeStore:
    def __init__(self):
        self.objects = {}
        self.requests = []
        store = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                key_id = self.headers.get("Authorization", "").partition("Credential=")[2].partition("/")[0]
                store.requests.append((self.command, self.path, key_id))
                status, payload = 200, b""
                if self.command == "PUT":
                    if key_id == WRITE_ID:
                        store.objects[self.path] = body
                    else:
                        status = 403
                elif self.path in store.objects:
                    payload = store.objects[self.path]
                else:
                    status = 404
                self.close_connection = True
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_PUT = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def store():
    fake = FakeStore()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, store, writer=False, hosted=True):
    monkeypatch.setenv("SIM_CACHE_ENDPOINT", store.endpoint)
    monkeypatch.setenv("SIM_CACHE_READ_KEY_ID", READ_ID)
    monkeypatch.setenv("SIM_CACHE_READ_SECRET", "reader-secret")
    if writer:
        monkeypatch.setenv("SIM_CACHE_WRITE_KEY_ID", WRITE_ID)
        monkeypatch.setenv("SIM_CACHE_WRITE_SECRET", "writer-secret")
    if hosted:
        monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("SIM_DEFAULT_BRANCH", "main")


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    top = tmp_path / "checkout"
    (top / ROOT).mkdir(parents=True)
    (top / ROOT / "package-lock.json").write_text(json.dumps(lockfile()), encoding="utf-8")
    (top / ".gitignore").write_text("npm-cache/\n", encoding="utf-8")
    git(top, "init", "-q")
    git(top, "add", "-A")
    git(top, "commit", "-q", "-m", "base")
    git(top, "update-ref", "refs/remotes/origin/main", "HEAD")
    return top


def fill_cache(top):
    content = top / "npm-cache" / "_cacache" / "content-v2" / "sha512" / "ab"
    content.mkdir(parents=True)
    (content / "cd").write_bytes(b"tarball")
    (top / "npm-cache" / "_cacache" / "index-v5").mkdir()
    (top / "npm-cache" / "_logs").mkdir()
    return top / "npm-cache"


def run(monkeypatch, capsys, top, *argv):
    monkeypatch.chdir(top)
    code = cache.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err


def save(monkeypatch, capsys, top):
    return run(monkeypatch, capsys, top, "save", "--dir", str(top / "npm-cache"), *TOOLS)


def restore(monkeypatch, capsys, top, directory):
    return run(monkeypatch, capsys, top, "restore", "--dir", str(directory), *TOOLS)


def key_of(top):
    return cache.compute_key((top / ROOT / "package-lock.json").read_bytes(), "22", "10", cache_platform())


def cache_platform():
    import platform
    import sys
    return f"{sys.platform}-{platform.machine().lower()}"


def tar_gz(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, content in members:
            tar.addfile(info, io.BytesIO(content) if content is not None else None)
    return buffer.getvalue()


def member(name, kind=tarfile.REGTYPE, content=b"x", link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    info.size = len(content) if kind == tarfile.REGTYPE else 0
    return info, content if kind == tarfile.REGTYPE else None


def test_the_committed_sim_lockfile_is_fully_verifiable():
    cache.verify_lockfile((REPO_ROOT / ROOT / "package-lock.json").read_bytes())


@pytest.mark.parametrize("override", [
    {"integrity": None},
    {"integrity": "sha1-" + "a" * 27 + "="},
    {"resolved": "http://registry.example.test/a/-/a-1.0.0.tgz"},
    {"resolved": "git+ssh://example.test/a.git"},
])
def test_registry_entry_npm_cannot_verify_is_refused(override):
    with pytest.raises(cache.LockfileError):
        cache.verify_lockfile(json.dumps(lockfile(**override)).encode())


def test_lockfile_without_a_packages_map_is_refused():
    with pytest.raises(cache.LockfileError):
        cache.verify_lockfile(json.dumps({"lockfileVersion": 1, "dependencies": {}}).encode())


def test_key_changes_with_lockfile_toolchain_and_platform_only():
    data = json.dumps(lockfile()).encode()
    base = cache.compute_key(data, "22", "10", "linux-x86_64")
    assert base == cache.compute_key(data, "22", "10", "linux-x86_64")
    assert len({base, cache.compute_key(data + b" ", "22", "10", "linux-x86_64"),
                cache.compute_key(data, "24", "10", "linux-x86_64"), cache.compute_key(data, "22", "11", "linux-x86_64"),
                cache.compute_key(data, "22", "10", "win32-amd64")}) == 5


def test_save_then_restore_round_trips_only_cacache(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store, writer=True)
    fill_cache(repo)
    assert save(monkeypatch, capsys, repo)[1] == "saved"
    assert [r[1] for r in store.requests] == [f"/sim-cache/npm/{key_of(repo)}.tar.gz"]
    target = repo.parent / "fresh"
    code, out, _ = restore(monkeypatch, capsys, repo, target)
    assert (code, out) == (0, "hit")
    assert (target / "_cacache" / "content-v2" / "sha512" / "ab" / "cd").read_bytes() == b"tarball"
    assert not (target / "_logs").exists()
    assert [p.name for p in target.iterdir()] == ["_cacache"]


def test_save_refuses_a_self_hosted_runner(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store, writer=True, hosted=False)
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    fill_cache(repo)
    assert save(monkeypatch, capsys, repo)[1] == "skip: not a GitHub-hosted runner"
    assert store.requests == []


def test_save_refuses_a_head_that_is_not_the_default_branch_tip(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store, writer=True)
    fill_cache(repo)
    (repo / "change.txt").write_text("pr\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")
    assert save(monkeypatch, capsys, repo)[1] == "skip: not the default branch tip"
    assert store.requests == []


@pytest.mark.parametrize("branch", ["", "-main", "ma..in", "main~0", "main@{0}"])
def test_save_refuses_an_unusable_default_branch_name(monkeypatch, capsys, repo, store, branch):
    configure(monkeypatch, store, writer=True)
    monkeypatch.setenv("SIM_DEFAULT_BRANCH", branch)
    fill_cache(repo)
    assert save(monkeypatch, capsys, repo)[1] == "skip: not the default branch tip"


def test_save_refuses_a_dirty_tree(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store, writer=True)
    fill_cache(repo)
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    assert save(monkeypatch, capsys, repo)[1] == "skip: the tree has modified or untracked files"
    assert store.requests == []


def test_save_without_a_writer_key_sends_nothing(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store)
    fill_cache(repo)
    assert save(monkeypatch, capsys, repo)[1] == "skip: no writer key"
    assert store.requests == []


def test_save_refuses_a_lockfile_npm_cannot_verify(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store, writer=True)
    (repo / ROOT / "package-lock.json").write_text(json.dumps(lockfile(integrity=None)), encoding="utf-8")
    git(repo, "commit", "-qam", "weak")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    fill_cache(repo)
    assert "no sha512 integrity" in save(monkeypatch, capsys, repo)[1]
    assert store.requests == []


def test_save_refuses_a_link_inside_cacache(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store, writer=True)
    directory = fill_cache(repo)
    target = directory / "_cacache" / "content-v2" / "sha512" / "ab" / "cd"
    try:
        os.link(target, directory / "_cacache" / "index-v5" / "linked")
    except OSError:
        pytest.skip("hard links unavailable")
    assert save(monkeypatch, capsys, repo)[1] == "skip: hard-linked file in _cacache"
    assert store.requests == []


def test_restore_with_a_lockfile_npm_cannot_verify_is_a_miss_without_a_request(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store)
    (repo / ROOT / "package-lock.json").write_text(json.dumps(lockfile(integrity=None)), encoding="utf-8")
    code, out, err = restore(monkeypatch, capsys, repo, repo / "npm-cache")
    assert (code, out) == (0, "miss") and "integrity" in err
    assert store.requests == []


def test_restore_without_a_reader_key_is_a_miss(monkeypatch, capsys, repo, store):
    code, out, err = restore(monkeypatch, capsys, repo, repo / "npm-cache")
    assert (code, out) == (0, "miss") and "no reader key" in err


def test_restore_of_an_absent_entry_is_a_quiet_miss(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store)
    code, out, err = restore(monkeypatch, capsys, repo, repo / "npm-cache")
    assert (code, out, err) == (0, "miss", "")


@pytest.mark.parametrize("members", [
    [member("_cacache/ok"), member("_cacache/../../../../escape")],
    [member("_cacache/ok"), member("../../../escape")],
    [member("_cacache/ok"), member("node_modules/evil.js")],
    [member("_cacache/ok"), member("/abs")],
    [member("_cacache/link", tarfile.SYMTYPE, link="/etc/passwd")],
    [member("_cacache/a"), member("_cacache/hard", tarfile.LNKTYPE, link="_cacache/a")],
    [member("_cacache/dev", tarfile.CHRTYPE)],
])
def test_restore_refuses_an_archive_that_leaves_cacache_or_is_not_plain_files(monkeypatch, capsys, repo, store, members):
    configure(monkeypatch, store)
    store.objects[f"/sim-cache/npm/{key_of(repo)}.tar.gz"] = tar_gz(members)
    target = repo.parent / "fresh"
    code, out, err = restore(monkeypatch, capsys, repo, target)
    assert (code, out) == (0, "miss") and "unusable archive" in err
    assert not (target / "_cacache").exists() and not (repo.parent / "escape").exists()
    assert list(target.iterdir()) == []


def test_restore_replaces_an_existing_cache_and_leaves_no_staging(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store)
    store.objects[f"/sim-cache/npm/{key_of(repo)}.tar.gz"] = tar_gz([member("_cacache/new", content=b"n")])
    directory = repo.parent / "fresh"
    (directory / "_cacache").mkdir(parents=True)
    (directory / "_cacache" / "old").write_bytes(b"o")
    assert restore(monkeypatch, capsys, repo, directory)[1] == "hit"
    assert sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")) == ["_cacache", "_cacache/new"]


def test_key_without_a_lockfile_is_an_error(monkeypatch, capsys, tmp_path):
    code, _, err = run(monkeypatch, capsys, tmp_path, "key", *TOOLS)
    assert code == 2 and "package-lock.json" in err


@pytest.mark.parametrize("limit, value", [("MAX_EXTRACTED_BYTES", 3), ("MAX_MEMBERS", 1)])
def test_restore_refuses_an_archive_beyond_the_limits(monkeypatch, capsys, repo, store, limit, value):
    configure(monkeypatch, store)
    monkeypatch.setattr(cache, limit, value)
    store.objects[f"/sim-cache/npm/{key_of(repo)}.tar.gz"] = tar_gz(
        [member("_cacache/a", content=b"aa"), member("_cacache/b", content=b"bb")])
    target = repo.parent / "fresh"
    code, out, err = restore(monkeypatch, capsys, repo, target)
    assert (code, out) == (0, "miss") and "unusable archive" in err
    assert list(target.iterdir()) == []
