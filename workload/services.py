import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

NAME_PREFIX = "wl-trial-"
START_TIMEOUT_SECONDS = 900
PROBE_ATTEMPT_SECONDS = 20


@dataclass(frozen=True)
class ServiceDef:
    image: str
    env: tuple
    probe: tuple
    ready_timeout: float
    user: str = ""


SERVICES = {
    "sql": ServiceDef(
        "postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24",
        ("POSTGRES_PASSWORD=workload",),
        ("pg_isready", "-h", "127.0.0.1", "-U", "postgres"),
        60,
    ),
    "document": ServiceDef(
        "mongo:8.0@sha256:4a4ab5d4834207389e0d7270fac988e21ffedccecba77991a552f755df229907",
        (),
        ("mongosh", "--quiet", "--host", "127.0.0.1", "--eval", "quit(db.adminCommand('ping').ok === 1 ? 0 : 1)"),
        90,
    ),
    "cache": ServiceDef(
        "redis:8-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0",
        (),
        ("sh", "-c", "redis-cli ping | grep -q PONG"),
        30,
    ),
    "search": ServiceDef(
        "docker.elastic.co/elasticsearch/elasticsearch:8.19.5@sha256:86d0a30f420470d9cd4102cdf380c5a458bd6ccbbd3f54f7332d3b0b562431b8",
        ("discovery.type=single-node", "xpack.security.enabled=false", "ES_JAVA_OPTS=-Xms256m -Xmx256m"),
        ("curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=5s"),
        240,
    ),
    "queue": ServiceDef(
        "rabbitmq:4.2-alpine@sha256:2ec385e7399a5e0d6fc267304fb711ac78ec0b05dc0a64c4d7501f70b15d66a2",
        (),
        ("rabbitmq-diagnostics", "-q", "check_running"),
        120,
        "rabbitmq",
    ),
}


class ServiceFailure(Exception):
    pass


_live = set()
_live_lock = threading.Lock()


def docker(args, timeout):
    proc = subprocess.Popen(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    with _live_lock:
        _live.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        with _live_lock:
            _live.discard(proc)
    return proc.returncode, out, err


def _kill_live():
    with _live_lock:
        procs = list(_live)
    for proc in procs:
        proc.kill()


def _first_line(text):
    lines = text.strip().splitlines()
    return lines[0][:120] if lines else ""


def _start_one(kind, name):
    spec = SERVICES[kind]
    args = ["run", "-d", "--name", name]
    for item in spec.env:
        args += ["-e", item]
    args.append(spec.image)
    try:
        code, _, err = docker(args, START_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise ServiceFailure("docker is not installed") from None
    except subprocess.TimeoutExpired:
        raise ServiceFailure(f"service {kind} did not start within {START_TIMEOUT_SECONDS}s") from None
    if code != 0:
        raise ServiceFailure(f"service {kind} failed to start: {_first_line(err)}")
    limit = time.monotonic() + spec.ready_timeout
    while True:
        remaining = limit - time.monotonic()
        if remaining <= 0:
            raise ServiceFailure(f"service {kind} not healthy within {spec.ready_timeout:g}s")
        try:
            exec_args = ["exec", *(["-u", spec.user] if spec.user else []), name, *spec.probe]
            code, _, _ = docker(exec_args, min(PROBE_ATTEMPT_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            code = 1
        if code == 0:
            return
        time.sleep(min(1.0, max(0.0, limit - time.monotonic())))


class ServiceSet:
    def __init__(self, job_id, kinds):
        token = uuid.uuid4().hex[:8]
        self.names = {kind: f"{NAME_PREFIX}{job_id}-{kind}-{token}" for kind in kinds}
        self.ready_seconds = None

    def start(self):
        if not self.names:
            return
        began = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=len(self.names))
        try:
            futures = [pool.submit(_start_one, kind, name) for kind, name in self.names.items()]
            for future in futures:
                future.result()
        finally:
            _kill_live()
            pool.shutdown(wait=True)
        self.ready_seconds = time.monotonic() - began

    def stop(self):
        if not self.names:
            return
        try:
            docker(["rm", "-f", "-v", *self.names.values()], 120)
        except (OSError, subprocess.TimeoutExpired):
            pass


def leftovers():
    code, out, err = docker(["ps", "-a", "--filter", f"name=^{NAME_PREFIX}", "--format", "{{.Names}}"], 60)
    if code != 0:
        raise ServiceFailure(f"docker ps failed: {_first_line(err)}")
    return [line for line in out.split() if line.startswith(NAME_PREFIX)]


def remove(names):
    if not names:
        return
    code, _, err = docker(["rm", "-f", "-v", *names], 120)
    if code != 0:
        raise ServiceFailure(f"docker rm failed: {_first_line(err)}")
