import functools
import gzip
import hashlib
import hmac
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from tools.sim_gate import cache

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cache" / "tree.json").read_text(encoding="utf-8"))
ROOT = "sim/dotnet"
SDK = "10.0.100"
UTILS = "sim/dotnet/src/Utils/Utils.csproj"
CORE = "sim/dotnet/src/Core/Core.csproj"
SERVICE = "sim/dotnet/src/Service/Service.csproj"
SERVICE_TESTS = "sim/dotnet/tests/Service.Tests/Service.Tests.csproj"
OTHER = "sim/dotnet/src/Other/Other.csproj"
OTHER_TESTS = "sim/dotnet/tests/Other.Tests/Other.Tests.csproj"
ALL_PROJECTS = {UTILS, CORE, SERVICE, SERVICE_TESTS, OTHER, OTHER_TESTS}
CORE_DEPENDENTS = {CORE, SERVICE, SERVICE_TESTS}

READ_ID, READ_SECRET = "reader-id-0001", "reader-secret-0001"
WRITE_ID, WRITE_SECRET = "writer-id-0001", "writer-secret-0001"
ENV_NAMES = (
    "SIM_CACHE_ENDPOINT", "SIM_CACHE_REGION", "SIM_CACHE_READ_KEY_ID", "SIM_CACHE_READ_SECRET",
    "SIM_CACHE_WRITE_KEY_ID", "SIM_CACHE_WRITE_SECRET",
)
AUTHORIZATION = re.compile(
    r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, "
    r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})$"
)
GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
       "-c", "core.autocrlf=false"]


class FakeS3:
    def __init__(self, region="us-east-1"):
        self.region = region
        self.keys = {READ_ID: READ_SECRET, WRITE_ID: WRITE_SECRET}
        self.writers = {WRITE_ID}
        self.objects = {}
        self.requests = []
        self.mode = None
        self.release = threading.Event()
        store = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_any(self):
                store.handle(self)

            do_GET = do_PUT = do_HEAD = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def paths(self, method=None):
        return [r["path"] for r in self.requests if method in (None, r["method"])]

    def verify(self, handler, body):
        match = AUTHORIZATION.match(handler.headers.get("Authorization", ""))
        if not match:
            return None, False
        key_id, day, region, signed, signature = match.groups()
        secret = self.keys.get(key_id)
        names = signed.split(";")
        if secret is None or region != self.region or not {"host", "x-amz-date", "x-amz-content-sha256"} <= set(names):
            return key_id, False
        stamp = handler.headers["x-amz-date"]
        payload = handler.headers["x-amz-content-sha256"]
        if stamp[:8] != day or payload != hashlib.sha256(body).hexdigest():
            return key_id, False
        path, _, query = handler.path.partition("?")
        canonical = "\n".join(
            [handler.command, path, query, *[f"{n}:{handler.headers[n].strip()}" for n in names], "", signed, payload]
        )
        scope = f"{day}/{region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        derived = functools.reduce(
            lambda k, part: hmac.new(k, part.encode(), hashlib.sha256).digest(),
            [day, region, "s3", "aws4_request"],
            b"AWS4" + secret.encode(),
        )
        expected = hmac.new(derived, to_sign.encode(), hashlib.sha256).hexdigest()
        return key_id, hmac.compare_digest(expected, signature)

    def handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        key_id, verified = self.verify(handler, body)
        self.requests.append({"method": handler.command, "path": handler.path, "key_id": key_id, "verified": verified})
        if self.mode == "slow":
            self.release.wait(10)
        status, payload = 200, b""
        if isinstance(self.mode, int):
            status = self.mode
        elif not verified:
            status = 403
        elif handler.command == "PUT":
            if key_id in self.writers:
                self.objects[handler.path] = body
            else:
                status = 403
        elif handler.path in self.objects:
            payload = self.objects[handler.path]
        else:
            status = 404
        handler.close_connection = True
        try:
            handler.send_response(status)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(payload)
        except OSError:
            pass


@pytest.fixture
def store():
    fake = FakeS3()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, endpoint, reader=True, writer=False):
    monkeypatch.setenv("SIM_CACHE_ENDPOINT", endpoint)
    if reader:
        monkeypatch.setenv("SIM_CACHE_READ_KEY_ID", READ_ID)
        monkeypatch.setenv("SIM_CACHE_READ_SECRET", READ_SECRET)
    if writer:
        monkeypatch.setenv("SIM_CACHE_WRITE_KEY_ID", WRITE_ID)
        monkeypatch.setenv("SIM_CACHE_WRITE_SECRET", WRITE_SECRET)


def git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path):
    for relative, text in FIXTURE["files"].items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "fixture")
    return path


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "checkout")


def keys_of(path, sdk=SDK, config="Release"):
    return cache.compute_keys(path, ROOT, config, sdk)


def edit(path, relative, text):
    (path / relative).write_text(text, encoding="utf-8", newline="")


def tar_gz(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def cache_path(key):
    return f"/sim-cache/{key}.tar.gz"


def run(monkeypatch, capsys, repo, *argv):
    monkeypatch.chdir(repo)
    code = cache.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def restore(monkeypatch, capsys, repo, project=CORE, *extra):
    return run(monkeypatch, capsys, repo, "restore", "--project", project, "--root", ROOT, "--sdk", SDK, *extra)


def save(monkeypatch, capsys, repo, project=CORE):
    return run(monkeypatch, capsys, repo, "save", "--project", project, "--root", ROOT, "--sdk", SDK)


def project_dir(repo, project=CORE):
    return repo / Path(project).parent


def snapshot(directory):
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))


def closed_endpoint():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def test_signature_matches_the_published_aws_example():
    access = "AKIA" + "IOSFODNN7EXAMPLE"
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256": hashlib.sha256(b"").hexdigest(),
        "x-amz-date": "20130524T000000Z",
    }
    header = cache.sign("GET", "/test.txt", headers, hashlib.sha256(b"").hexdigest(), access, secret, "us-east-1")
    assert header == (
        f"AWS4-HMAC-SHA256 Credential={access}/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_stale_entry_under_the_name_or_an_old_key_is_not_restored(monkeypatch, capsys, repo, store, tmp_path):
    configure(monkeypatch, store.endpoint)
    old_key = keys_of(repo)[CORE]
    stale = tar_gz({"bin/Release/Core.dll": b"core-v1", "obj/project.assets.json": b"{}"})
    store.objects[cache_path(old_key)] = stale
    store.objects["/sim-cache/Core.tar.gz"] = stale
    store.objects["/sim-cache/Core.Core.tar.gz"] = stale

    code, out, _ = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "hit")
    assert (project_dir(repo) / "bin/Release/Core.dll").read_bytes() == b"core-v1"
    shutil.rmtree(project_dir(repo) / "bin")
    shutil.rmtree(project_dir(repo) / "obj")

    edit(repo, "sim/dotnet/src/Core/Stamp.cs", FIXTURE["files"]["sim/dotnet/src/Core/Stamp.cs"].replace("core-v1", "core-v2"))
    new_key = keys_of(repo)[CORE]
    assert new_key != old_key
    store.requests.clear()
    code, out, _ = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert not (project_dir(repo) / "bin").exists()
    assert store.paths() == [cache_path(new_key)]


def test_dependency_source_change_changes_every_dependent_and_nothing_else(repo):
    before = keys_of(repo)
    assert len(set(before.values())) == len(ALL_PROJECTS)
    edit(repo, "sim/dotnet/src/Core/Stamp.cs", "public static class CoreStamp { }")
    after = keys_of(repo)
    changed = {p for p in ALL_PROJECTS if before[p] != after[p]}
    assert changed == CORE_DEPENDENTS


def test_leaf_change_changes_only_the_leaf(repo):
    before = keys_of(repo)
    edit(repo, "sim/dotnet/tests/Service.Tests/FacadeTests.cs", "public class FacadeTests { int x; }")
    after = keys_of(repo)
    assert {p for p in ALL_PROJECTS if before[p] != after[p]} == {SERVICE_TESTS}


@pytest.mark.parametrize("relative", [
    "sim/dotnet/Directory.Build.props",
    "sim/dotnet/global.json",
    "sim/dotnet/nuget.config",
    "sim/dotnet/Sim.sln",
    "sim/Directory.Build.targets",
    "Directory.Packages.props",
    ".editorconfig",
    "sim/.globalconfig",
])
def test_build_wide_file_change_changes_every_key(repo, relative):
    before = keys_of(repo)
    edit(repo, relative, FIXTURE["files"][relative] + "\n ")
    after = keys_of(repo)
    assert {p for p in ALL_PROJECTS if before[p] != after[p]} == ALL_PROJECTS


@pytest.mark.parametrize("relative", [
    "sim/README.md",
    "docs/notes.txt",
    "sim/other/Directory.Build.props",
])
def test_unrelated_file_outside_the_build_leaves_every_key(repo, relative):
    before = keys_of(repo)
    edit(repo, relative, FIXTURE["files"][relative] + "\n changed")
    assert keys_of(repo) == before


def test_sdk_or_configuration_change_changes_every_key(repo):
    base = keys_of(repo)
    for other in (keys_of(repo, sdk="10.0.101"), keys_of(repo, config="Debug")):
        assert {p for p in ALL_PROJECTS if base[p] != other[p]} == ALL_PROJECTS


def test_key_depends_on_content_only_not_on_location_or_mtime(tmp_path, repo):
    original = keys_of(repo)
    for path in repo.rglob("*.cs"):
        os.utime(path, (1_000_000, 1_000_000))
    elsewhere = make_repo(tmp_path / "another" / "place" / "clone")
    assert keys_of(repo) == original
    assert keys_of(elsewhere) == original


def test_key_reads_the_working_tree_and_ignores_untracked_and_build_output(repo):
    before = keys_of(repo)
    (project_dir(repo) / "bin").mkdir()
    (project_dir(repo) / "bin" / "Core.dll").write_bytes(b"binary")
    (project_dir(repo) / "obj").mkdir()
    (project_dir(repo) / "obj" / "Core.cache").write_bytes(b"cache")
    (project_dir(repo) / "Generated.cs").write_text("// untracked", encoding="utf-8")
    assert keys_of(repo) == before
    edit(repo, "sim/dotnet/src/Core/Stamp.cs", "uncommitted edit")
    assert keys_of(repo)[CORE] != before[CORE]


def test_key_command_prints_the_computed_key(monkeypatch, capsys, repo):
    expected = keys_of(repo)[SERVICE]
    code, out, err = run(monkeypatch, capsys, repo, "key", "--project", SERVICE, "--root", ROOT, "--sdk", SDK)
    assert (code, out.strip(), err) == (0, expected, "")


def test_key_command_refuses_a_project_outside_the_root(monkeypatch, capsys, repo):
    code, out, err = run(monkeypatch, capsys, repo, "key", "--project", "nowhere/X.csproj", "--root", ROOT, "--sdk", SDK)
    assert code == 2 and out == "" and err.startswith("sim_gate.cache:")


def test_unreachable_endpoint_is_a_miss_not_a_failure(monkeypatch, capsys, repo):
    configure(monkeypatch, closed_endpoint())
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert err.startswith("miss: ")
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


@pytest.mark.parametrize("status", [403, 500, 503, 302])
def test_error_status_is_a_miss(monkeypatch, capsys, repo, store, status):
    configure(monkeypatch, store.endpoint)
    store.mode = status
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert err.startswith("miss: ") and str(status) in err
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


def test_timeout_is_a_miss(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    store.mode = "slow"
    code, out, err = restore(monkeypatch, capsys, repo, CORE, "--timeout", "0.3")
    assert (code, out.strip()) == (0, "miss")
    assert err.startswith("miss: ") and "timeout" in err


def test_plain_not_found_is_a_quiet_miss(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip(), err) == (0, "miss", "")


def test_missing_reader_key_or_endpoint_makes_no_request(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, reader=False)
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss") and err.startswith("miss: ")
    monkeypatch.delenv("SIM_CACHE_ENDPOINT")
    monkeypatch.setenv("SIM_CACHE_READ_KEY_ID", READ_ID)
    monkeypatch.setenv("SIM_CACHE_READ_SECRET", READ_SECRET)
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss") and err.startswith("miss: ")
    assert store.requests == []


def test_truncated_archive_leaves_nothing_behind(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    whole = tar_gz({"bin/a.dll": os.urandom(40000), "obj/b.cache": os.urandom(40000)})
    store.objects[cache_path(keys_of(repo)[CORE])] = whole[: len(whole) // 2]
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss") and err.startswith("miss: ")
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


def test_garbage_body_is_a_miss(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    store.objects[cache_path(keys_of(repo)[CORE])] = b"not an archive"
    code, out, _ = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


def test_hit_replaces_existing_output_and_leaves_no_temp_files(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    (project_dir(repo) / "bin").mkdir()
    (project_dir(repo) / "bin" / "old.dll").write_bytes(b"old")
    store.objects[cache_path(keys_of(repo)[CORE])] = tar_gz({"bin/new.dll": b"new", "obj/x.cache": b"x"})
    code, out, _ = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "hit")
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs", "bin", "bin/new.dll", "obj", "obj/x.cache"]


def escape_archives(tmp_path):
    def archive(*entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for info, content in entries:
                tar.addfile(info, io.BytesIO(content) if content is not None else None)
        return buffer.getvalue()

    def regular(name, content=b"x"):
        info = tarfile.TarInfo(name)
        info.size = len(content)
        return info, content

    def link(name, target, kind):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = target
        return info, None

    return {
        "parent-traversal": archive(regular("bin/ok.dll"), regular("bin/../../escaped.txt")),
        "absolute": archive(regular("bin/ok.dll"), regular(str(tmp_path / "absolute-escape.txt"))),
        "symlink-then-write-through": archive(
            regular("bin/ok.dll"), link("bin/link", "../..", tarfile.SYMTYPE), regular("bin/link/escaped.txt")
        ),
        "hardlink": archive(link("bin/hard", "../../Stamp.cs", tarfile.LNKTYPE)),
        "outside-bin-and-obj": archive(regular("Stamp.cs", b"overwritten")),
        "backslash": archive(regular("bin\\..\\..\\escaped.txt")),
        "device": archive(link("bin/dev", "", tarfile.CHRTYPE)),
        "empty": archive(),
    }


@pytest.mark.parametrize("case", [
    "parent-traversal", "absolute", "symlink-then-write-through", "hardlink", "outside-bin-and-obj",
    "backslash", "device", "empty",
])
def test_archive_that_escapes_the_project_directory_is_refused(monkeypatch, capsys, repo, store, tmp_path, case):
    configure(monkeypatch, store.endpoint)
    store.objects[cache_path(keys_of(repo)[CORE])] = escape_archives(tmp_path)[case]
    before = snapshot(repo.parent)
    stamp = (project_dir(repo) / "Stamp.cs").read_bytes()
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss") and err.startswith("miss: ")
    assert snapshot(repo.parent) == before
    assert (project_dir(repo) / "Stamp.cs").read_bytes() == stamp
    assert not (repo / "sim/dotnet/src/escaped.txt").exists()


def test_archive_larger_than_the_limit_is_a_miss(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    store.objects[cache_path(keys_of(repo)[CORE])] = tar_gz({"bin/a.dll": os.urandom(5000)})
    monkeypatch.setattr(cache, "MAX_ARCHIVE_BYTES", 1000)
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss") and "too large" in err
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


def test_archive_that_expands_beyond_the_limit_is_a_miss(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    store.objects[cache_path(keys_of(repo)[CORE])] = tar_gz({"bin/a.dll": b"\0" * 100000})
    monkeypatch.setattr(cache, "MAX_EXTRACTED_BYTES", 1000)
    code, out, _ = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


def build_output(repo, project=CORE):
    base = project_dir(repo, project)
    (base / "bin" / "Release").mkdir(parents=True)
    (base / "obj").mkdir()
    (base / "bin" / "Release" / "Core.dll").write_bytes(b"compiled")
    (base / "obj" / "Core.cache").write_bytes(b"cache")


@pytest.mark.parametrize("present", [(), ("id",), ("secret",)])
def test_save_without_a_complete_writer_key_is_skipped_and_sends_nothing(monkeypatch, capsys, repo, store, present):
    configure(monkeypatch, store.endpoint)
    if "id" in present:
        monkeypatch.setenv("SIM_CACHE_WRITE_KEY_ID", WRITE_ID)
    if "secret" in present:
        monkeypatch.setenv("SIM_CACHE_WRITE_SECRET", WRITE_SECRET)
    build_output(repo)
    code, out, _ = save(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "skip: no writer key")
    assert store.requests == []


def test_save_then_restore_round_trips_with_the_right_keys(monkeypatch, capsys, repo, store, tmp_path):
    configure(monkeypatch, store.endpoint, writer=True)
    build_output(repo)
    key = keys_of(repo)[CORE]
    code, out, _ = save(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "saved")
    assert [(r["method"], r["path"], r["key_id"], r["verified"]) for r in store.requests] == [
        ("PUT", cache_path(key), WRITE_ID, True)
    ]
    with tarfile.open(fileobj=io.BytesIO(store.objects[cache_path(key)]), mode="r:gz") as tar:
        assert sorted(m.name for m in tar.getmembers() if m.isfile()) == ["bin/Release/Core.dll", "obj/Core.cache"]
        assert {m.uid for m in tar} == {0}

    fresh = make_repo(tmp_path / "fresh")
    store.requests.clear()
    code, out, _ = restore(monkeypatch, capsys, fresh)
    assert (code, out.strip()) == (0, "hit")
    assert (project_dir(fresh) / "bin/Release/Core.dll").read_bytes() == b"compiled"
    assert (project_dir(fresh) / "obj/Core.cache").read_bytes() == b"cache"
    assert [(r["method"], r["key_id"], r["verified"]) for r in store.requests] == [("GET", READ_ID, True)]


def test_reader_key_cannot_write(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, writer=True)
    store.writers.clear()
    build_output(repo)
    code, out, err = save(monkeypatch, capsys, repo)
    assert code == 0 and out.strip() == "" and err.startswith("skip: ") and "403" in err
    assert store.objects == {}


def test_save_with_nothing_built_or_an_unreachable_store_is_a_skip(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, writer=True)
    code, out, err = save(monkeypatch, capsys, repo)
    assert code == 0 and out.strip() == "" and err.startswith("skip: ")
    assert store.requests == []
    build_output(repo)
    configure(monkeypatch, closed_endpoint(), writer=True)
    code, out, err = save(monkeypatch, capsys, repo)
    assert code == 0 and out.strip() == "" and err.startswith("skip: ")


def test_save_refuses_a_symlink_in_the_output(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, writer=True)
    build_output(repo)
    try:
        os.symlink("Core.dll", project_dir(repo) / "bin" / "Release" / "link")
    except OSError:
        pytest.skip("symlinks unavailable")
    code, out, err = save(monkeypatch, capsys, repo)
    assert code == 0 and err.startswith("skip: ")
    assert store.requests == []


def test_tripwire_present_writes_the_marker(monkeypatch, capsys, store, tmp_path):
    configure(monkeypatch, store.endpoint)
    store.objects["/sim-control/tripwire"] = b"tripped"
    markers = tmp_path / "work" / "markers"
    monkeypatch.chdir(tmp_path)
    code = cache.main(["tripwire", "--markers", str(markers)])
    assert code == 0
    assert (markers / "cache-tripwire").is_file()
    assert [(r["method"], r["path"], r["key_id"], r["verified"]) for r in store.requests] == [
        ("GET", "/sim-control/tripwire", READ_ID, True)
    ]


def test_tripwire_absent_writes_nothing(monkeypatch, store, tmp_path):
    configure(monkeypatch, store.endpoint)
    markers = tmp_path / "markers"
    markers.mkdir()
    assert cache.main(["tripwire", "--markers", str(markers)]) == 0
    assert list(markers.iterdir()) == []
    assert store.paths() == ["/sim-control/tripwire"]


@pytest.mark.parametrize("mode", ["down", 403, 500, "slow", "no-key"])
def test_tripwire_store_down_or_erroring_is_not_a_trip(monkeypatch, store, tmp_path, mode):
    markers = tmp_path / "markers"
    markers.mkdir()
    configure(monkeypatch, closed_endpoint() if mode == "down" else store.endpoint, reader=mode != "no-key")
    store.objects["/sim-control/tripwire"] = b"tripped"
    store.mode = mode if mode in (403, 500, "slow") else None
    assert cache.main(["tripwire", "--markers", str(markers), "--timeout", "0.3"]) == 0
    assert list(markers.iterdir()) == []
    if mode == "no-key":
        assert store.requests == []


def test_region_can_be_overridden(monkeypatch, capsys, repo):
    other = FakeS3(region="eu-west-1")
    try:
        configure(monkeypatch, other.endpoint)
        monkeypatch.setenv("SIM_CACHE_REGION", "eu-west-1")
        other.objects[cache_path(keys_of(repo)[CORE])] = tar_gz({"bin/a.dll": b"a"})
        code, out, _ = restore(monkeypatch, capsys, repo)
        assert (code, out.strip()) == (0, "hit")
    finally:
        other.close()


def test_secrets_and_endpoint_never_reach_output(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, writer=True)
    build_output(repo)
    key = keys_of(repo)[CORE]
    seen = []
    for mode, setup in (
        (None, lambda: store.objects.pop(cache_path(key), None)),
        (None, lambda: None),
        (403, lambda: None),
        (500, lambda: None),
    ):
        store.mode = mode
        setup()
        seen.append(restore(monkeypatch, capsys, repo)[1:])
        seen.append(save(monkeypatch, capsys, repo)[1:])
    store.mode = None
    monkeypatch.chdir(repo)
    cache.main(["tripwire", "--markers", str(repo / "markers")])
    seen.append(capsys.readouterr()[0:2])
    monkeypatch.setenv("SIM_CACHE_ENDPOINT", closed_endpoint())
    seen.append(restore(monkeypatch, capsys, repo)[1:])
    text = "\n".join(a + b for a, b in seen)
    host = store.endpoint.split("//")[1]
    for secret in (READ_ID, READ_SECRET, WRITE_ID, WRITE_SECRET, host, "127.0.0.1"):
        assert secret not in text
    assert re.search(r"[0-9a-f]{64}", text) is None


def test_secrets_never_reach_a_real_process_and_are_not_accepted_on_argv(tmp_path, repo, store):
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "SIM_CACHE_ENDPOINT": store.endpoint,
        "SIM_CACHE_READ_KEY_ID": READ_ID,
        "SIM_CACHE_READ_SECRET": READ_SECRET,
    }
    command = [sys.executable, "-m", "tools.sim_gate.cache", "restore", "--project", CORE, "--root", ROOT, "--sdk", SDK]
    proc = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "miss"
    assert READ_ID not in proc.stdout + proc.stderr and READ_SECRET not in proc.stdout + proc.stderr
    assert [r["verified"] for r in store.requests] == [True]
    for flag in ("--secret", "--key-id", "--read-secret", "--write-secret"):
        refused = subprocess.run([*command, flag, "x"], cwd=repo, env=env, capture_output=True, text=True)
        assert refused.returncode == 2
    assert len(store.requests) == 1


def test_help_documents_the_save_precondition():
    proc = subprocess.run(
        [sys.executable, "-m", "tools.sim_gate.cache", "save", "--help"],
        cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, capture_output=True, text=True,
    )
    flat = " ".join(proc.stdout.split())
    assert proc.returncode == 0
    assert "successful build" in flat and "SIM_CACHE_WRITE_KEY_ID" in flat


@pytest.mark.parametrize("relative", ["sim/dotnet/.editorconfig", "sim/dotnet/src/Shared.cs"])
def test_unowned_tracked_file_under_the_root_changes_every_key(repo, relative):
    before = keys_of(repo)
    edit(repo, relative, FIXTURE["files"][relative] + "\ndotnet_diagnostic.CS0168.severity = error")
    after = keys_of(repo)
    assert {p for p in ALL_PROJECTS if before[p] != after[p]} == ALL_PROJECTS


def test_inert_unowned_file_leaves_every_key(repo):
    before = keys_of(repo)
    edit(repo, "sim/dotnet/NOTES.md", "changed notes")
    assert keys_of(repo) == before


def test_tracked_symlink_under_the_root_is_refused(repo):
    try:
        os.symlink("../Utils/Stamp.cs", repo / "sim/dotnet/src/Core/Alias.cs")
    except OSError:
        pytest.skip("symlinks unavailable")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    with pytest.raises(cache.CacheError):
        keys_of(repo)


def test_save_refuses_a_hard_link_out_of_the_project(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, writer=True)
    build_output(repo)
    try:
        os.link(repo / "sim/dotnet/src/Utils/Stamp.cs", project_dir(repo) / "bin" / "Release" / "foreign.cs")
    except OSError:
        pytest.skip("hard links unavailable")
    code, out, err = save(monkeypatch, capsys, repo)
    assert code == 0 and out.strip() == "" and err.startswith("skip: ")
    assert store.requests == []


def test_restore_removes_stale_staging_directories(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    stale = project_dir(repo) / ".sim-cache-leftover"
    (stale / "previous-obj").mkdir(parents=True)
    (stale / "previous-obj" / "x.cache").write_bytes(b"x")
    code, out, _ = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert snapshot(project_dir(repo)) == ["Core.csproj", "Stamp.cs"]


@pytest.mark.parametrize("command", ["restore", "tripwire"])
def test_key_id_with_a_newline_is_no_credential_and_never_a_traceback(monkeypatch, capsys, repo, store, tmp_path, command):
    configure(monkeypatch, store.endpoint)
    monkeypatch.setenv("SIM_CACHE_READ_KEY_ID", "KEYID\n")
    if command == "restore":
        code, out, err = restore(monkeypatch, capsys, repo)
        assert (code, out.strip()) == (0, "miss")
    else:
        monkeypatch.chdir(tmp_path)
        assert cache.main(["tripwire", "--markers", str(tmp_path / "m")]) == 0
        captured = capsys.readouterr()
        err = captured.err
        assert not (tmp_path / "m" / "cache-tripwire").exists()
    assert "no reader key" in err
    assert "KEYID" not in err and "Signature=" not in err and "Traceback" not in err
    assert store.requests == []


def test_write_key_id_with_a_newline_is_no_writer_key(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint, writer=True)
    monkeypatch.setenv("SIM_CACHE_WRITE_KEY_ID", "KEYID\n")
    build_output(repo)
    code, out, err = save(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "skip: no writer key")
    assert "KEYID" not in err and store.requests == []


def test_a_header_the_http_layer_rejects_is_a_miss_without_the_signature(monkeypatch, capsys, repo, store):
    configure(monkeypatch, store.endpoint)
    monkeypatch.setenv("SIM_CACHE_REGION", "us-east-1\n")
    code, out, err = restore(monkeypatch, capsys, repo)
    assert (code, out.strip()) == (0, "miss")
    assert err.startswith("miss: ") and "Signature=" not in err and "Traceback" not in err
    assert store.requests == []


def test_tracked_file_inside_a_bin_or_obj_directory_is_refused(monkeypatch, capsys, repo):
    hidden = project_dir(repo) / "Logic" / "bin" / "Hidden.cs"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("class Hidden { }", encoding="utf-8")
    git(repo, "add", "-f", "-A")
    git(repo, "commit", "-q", "-m", "hidden")
    with pytest.raises(cache.CacheError, match="bin or obj"):
        keys_of(repo)
    for command in ("key", "restore"):
        code, out, err = run(monkeypatch, capsys, repo, command, "--project", CORE, "--root", ROOT, "--sdk", SDK)
        assert code == 2 and out == "" and "bin or obj" in err


@pytest.mark.parametrize("field", ["region", "secret"])
def test_a_surrogate_in_a_signed_value_is_a_store_error_not_a_crash(store, field):
    values = {"region": "us-east-1", "secret": READ_SECRET}
    values[field] += chr(0xDCFF)
    client = cache.Store(store.endpoint, READ_ID, values["secret"], values["region"], 2.0)
    with pytest.raises(cache.StoreError):
        client.get("sim-cache", "x.tar.gz")
    assert store.requests == []
