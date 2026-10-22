from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence
from urllib.parse import quote, urlsplit

CACHE_BUCKET = "sim-cache"
CONTROL_BUCKET = "sim-control"
TRIPWIRE_OBJECT = "tripwire"
TRIPWIRE_MARKER = "cache-tripwire"
DEFAULT_REGION = "us-east-1"
DEFAULT_TIMEOUT = 60.0
MAX_ARCHIVE_BYTES = 1 << 30
CHUNK = 1 << 16
KEY_ID = re.compile(r"[A-Za-z0-9._~+/=-]{1,128}")
ROLES = ("READ", "WRITE")


class StoreError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, path: str, headers: Mapping[str, str], payload_hash: str, key_id: str, secret: str,
         region: str, query: str = "") -> str:
    normalized = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    names = sorted(normalized)
    amz_date = normalized["x-amz-date"]
    day = amz_date[:8]
    canonical = "\n".join([
        method, path, query, *(f"{name}:{normalized[name]}" for name in names), "", ";".join(names), payload_hash,
    ])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    derived = _hmac(("AWS4" + secret).encode("utf-8"), day)
    for part in (region, "s3", "aws4_request"):
        derived = _hmac(derived, part)
    signature = hmac.new(derived, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, "
            f"Signature={signature}")


class Store:
    def __init__(self, endpoint: str, key_id: str, secret: str, region: str, timeout: float):
        try:
            parts = urlsplit(endpoint)
            port = parts.port
        except ValueError:
            raise StoreError("bad endpoint") from None
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path.strip("/") or parts.query or parts.fragment):
            raise StoreError("bad endpoint")
        self.secure = parts.scheme == "https"
        self.hostname = parts.hostname
        self.port = port
        bracketed = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        self.host = f"{bracketed}:{port}" if port else bracketed
        self.key_id, self.secret, self.region, self.timeout = key_id, secret, region, timeout

    def get(self, bucket: str, name: str, sink: Optional[Path] = None) -> int:
        return self._exchange("GET", bucket, name, b"", sink)

    def put(self, bucket: str, name: str, body: bytes) -> int:
        return self._exchange("PUT", bucket, name, body, None)

    def _exchange(self, method: str, bucket: str, name: str, body: bytes, sink: Optional[Path]) -> int:
        path = quote(f"/{bucket}/{name}", safe="/~")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        }
        deadline = time.monotonic() + self.timeout
        factory = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        connection = factory(self.hostname, self.port, timeout=self.timeout)
        try:
            headers["authorization"] = sign(method, path, headers, payload_hash, self.key_id, self.secret, self.region)
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            if sink is not None and response.status == 200:
                self._stream(response, connection, sink, deadline)
            return response.status
        except StoreError:
            raise
        except TimeoutError:
            raise StoreError("timeout") from None
        except (http.client.HTTPException, ValueError):
            raise StoreError("protocol error") from None
        except OSError:
            raise StoreError("unreachable") from None
        finally:
            connection.close()

    @staticmethod
    def _stream(response, connection, sink: Path, deadline: float) -> None:
        total = 0
        with open(sink, "wb") as out:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read(CHUNK)
                if not chunk:
                    return
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise StoreError("archive too large")
                out.write(chunk)


def credentials(role: str) -> Optional[tuple]:
    if role not in ROLES:
        raise ValueError(f"unknown store role {role!r}")
    key_id = os.environ.get(f"SIM_CACHE_{role}_KEY_ID", "")
    secret = os.environ.get(f"SIM_CACHE_{role}_SECRET", "")
    return (key_id, secret) if KEY_ID.fullmatch(key_id) and secret else None


def open_store(role: str, timeout: float) -> Store:
    found = credentials(role)
    if found is None:
        raise StoreError("no reader key" if role == "READ" else "no writer key")
    endpoint = os.environ.get("SIM_CACHE_ENDPOINT", "")
    if not endpoint:
        raise StoreError("no endpoint")
    region = os.environ.get("SIM_CACHE_REGION") or DEFAULT_REGION
    return Store(endpoint, found[0], found[1], region, timeout)


def command_tripwire(args) -> int:
    try:
        status = open_store("READ", args.timeout).get(CONTROL_BUCKET, TRIPWIRE_OBJECT)
    except StoreError as exc:
        print(f"tripwire: {exc.reason}", file=sys.stderr)
        return 0
    if status == 200:
        markers = Path(args.markers)
        markers.mkdir(parents=True, exist_ok=True)
        (markers / TRIPWIRE_MARKER).write_text("cache tripwire\n", encoding="utf-8")
        print("tripped")
    elif status == 404:
        print("clear")
    else:
        print(f"tripwire: http {status}", file=sys.stderr)
    return 0


def add_timeout(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds one store request may take (default: %(default)s)")


def add_tripwire(sub) -> None:
    trip = sub.add_parser(
        "tripwire", help="write the cache-tripwire marker when the store reports a trip",
        description=("GET sim-control/tripwire with the reader key. Present: write MARKERS/cache-tripwire. "
                     "Absent, or the store unreachable: write nothing (a store that is down is a miss, "
                     "not a trip)."),
    )
    trip.add_argument("--markers", required=True, help="directory that receives the marker file")
    add_timeout(trip)
    trip.set_defaults(run=command_tripwire)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.store",
        description=(
            "S3 (SigV4) object store client for every stack cache. Credentials come from the environment "
            "only: SIM_CACHE_ENDPOINT, SIM_CACHE_READ_KEY_ID, SIM_CACHE_READ_SECRET, SIM_CACHE_WRITE_KEY_ID, "
            "SIM_CACHE_WRITE_SECRET, SIM_CACHE_REGION (default us-east-1). A plain http:// endpoint is "
            "acceptable only over an encrypted overlay (tailnet) or an isolated lab segment."
        ),
    )
    add_tripwire(parser.add_subparsers(dest="command", required=True))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
