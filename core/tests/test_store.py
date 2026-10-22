import functools
import hashlib
import hmac
import http.server
import re
import socket
import threading

import pytest

from core import store

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


class FakeS3:
    def __init__(self, region="us-east-1"):
        self.region = region
        self.keys = {READ_ID: READ_SECRET, WRITE_ID: WRITE_SECRET}
        self.objects = {}
        self.requests = []
        self.mode = None
        self.release = threading.Event()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_any(self):
                fake.handle(self)

            do_GET = do_PUT = handle_any

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

    def paths(self):
        return [r["path"] for r in self.requests]

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
            handler.wfile.write(payload)
        except OSError:
            pass


@pytest.fixture
def fake():
    server = FakeS3()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, endpoint, reader=True):
    monkeypatch.setenv("SIM_CACHE_ENDPOINT", endpoint)
    if reader:
        monkeypatch.setenv("SIM_CACHE_READ_KEY_ID", READ_ID)
        monkeypatch.setenv("SIM_CACHE_READ_SECRET", READ_SECRET)


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
    header = store.sign("GET", "/test.txt", headers, hashlib.sha256(b"").hexdigest(), access, secret, "us-east-1")
    assert header == (
        f"AWS4-HMAC-SHA256 Credential={access}/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_tripwire_present_writes_the_marker(monkeypatch, capsys, fake, tmp_path):
    configure(monkeypatch, fake.endpoint)
    fake.objects["/sim-control/tripwire"] = b"tripped"
    markers = tmp_path / "work" / "markers"
    monkeypatch.chdir(tmp_path)
    code = store.main(["tripwire", "--markers", str(markers)])
    assert code == 0
    assert (markers / "cache-tripwire").is_file()
    assert capsys.readouterr().out.strip() == "tripped"
    assert [(r["method"], r["path"], r["key_id"], r["verified"]) for r in fake.requests] == [
        ("GET", "/sim-control/tripwire", READ_ID, True)
    ]


def test_tripwire_absent_writes_nothing(monkeypatch, capsys, fake, tmp_path):
    configure(monkeypatch, fake.endpoint)
    markers = tmp_path / "markers"
    markers.mkdir()
    assert store.main(["tripwire", "--markers", str(markers)]) == 0
    assert capsys.readouterr().out.strip() == "clear"
    assert list(markers.iterdir()) == []
    assert fake.paths() == ["/sim-control/tripwire"]


@pytest.mark.parametrize("mode", ["down", 403, 500, "slow", "no-key"])
def test_tripwire_store_down_or_erroring_is_not_a_trip(monkeypatch, capsys, fake, tmp_path, mode):
    markers = tmp_path / "markers"
    markers.mkdir()
    configure(monkeypatch, closed_endpoint() if mode == "down" else fake.endpoint, reader=mode != "no-key")
    fake.objects["/sim-control/tripwire"] = b"tripped"
    fake.mode = mode if mode in (403, 500, "slow") else None
    assert store.main(["tripwire", "--markers", str(markers), "--timeout", "0.3"]) == 0
    assert capsys.readouterr().out == ""
    assert list(markers.iterdir()) == []
    if mode == "no-key":
        assert fake.requests == []


def test_tripwire_key_id_with_a_newline_is_no_credential_and_never_a_traceback(monkeypatch, capsys, fake, tmp_path):
    configure(monkeypatch, fake.endpoint)
    monkeypatch.setenv("SIM_CACHE_READ_KEY_ID", "KEYID\n")
    monkeypatch.chdir(tmp_path)
    assert store.main(["tripwire", "--markers", str(tmp_path / "m")]) == 0
    err = capsys.readouterr().err
    assert not (tmp_path / "m" / "cache-tripwire").exists()
    assert "no reader key" in err
    assert "KEYID" not in err and "Signature=" not in err and "Traceback" not in err
    assert fake.requests == []


@pytest.mark.parametrize("field", ["region", "secret"])
def test_a_surrogate_in_a_signed_value_is_a_store_error_not_a_crash(fake, field):
    values = {"region": "us-east-1", "secret": READ_SECRET}
    values[field] += chr(0xDCFF)
    client = store.Store(fake.endpoint, READ_ID, values["secret"], values["region"], 2.0)
    with pytest.raises(store.StoreError):
        client.get("sim-cache", "x.tar.gz")
    assert fake.requests == []


@pytest.mark.parametrize("role", ["", "read", "OTHER", "READ_KEY"])
def test_credentials_refuse_a_role_other_than_read_or_write(monkeypatch, role):
    monkeypatch.setenv(f"SIM_CACHE_{role}_KEY_ID", READ_ID)
    monkeypatch.setenv(f"SIM_CACHE_{role}_SECRET", READ_SECRET)
    with pytest.raises(ValueError):
        store.credentials(role)
