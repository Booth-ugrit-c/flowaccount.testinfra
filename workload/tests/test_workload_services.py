import dataclasses
import json
import os
import re

import pytest

from workload import runner, services, spec as spec_mod
from workload.tests.helpers import docker_available, result_of, run_cli, spec

needs_docker = pytest.mark.skipif(not docker_available(), reason="docker daemon not available")
needs_full_docker = pytest.mark.skipif(
    os.environ.get("WORKLOAD_DOCKER_FULL") != "1" or not docker_available(),
    reason="set WORKLOAD_DOCKER_FULL=1 (and have docker) to pull the large service images",
)
PINNED = re.compile(r"^[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def test_every_service_type_has_a_digest_pinned_image():
    assert set(services.SERVICES) == set(spec_mod.SERVICE_TYPES)
    for kind, definition in services.SERVICES.items():
        assert PINNED.match(definition.image), kind


def test_unpinned_image_would_be_caught():
    assert not PINNED.match("redis:8-alpine")
    assert not PINNED.match("redis@sha256:" + "0" * 63)


class FakeDocker:
    def __init__(self, fail_run_for=None, unhealthy_for=None):
        self.calls = []
        self.fail_run_for = fail_run_for
        self.unhealthy_for = unhealthy_for

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        image = args[-1] if args[0] == "run" else None
        if args[0] == "run" and self.fail_run_for and self.fail_run_for in image:
            return 125, "", "Error response from daemon: pull access denied\nsecond line"
        if args[0] == "exec" and self.unhealthy_for and self.unhealthy_for in " ".join(args):
            return 1, "", ""
        return 0, "", ""


def test_container_names_carry_prefix_job_id_type_and_are_unique():
    first = services.ServiceSet("integration-004", ["sql", "cache"])
    second = services.ServiceSet("integration-004", ["sql", "cache"])
    for name in first.names.values():
        assert name.startswith("wl-trial-integration-004-")
    assert set(first.names.values()).isdisjoint(second.names.values())


def test_all_containers_are_removed_when_one_service_fails_to_start(monkeypatch):
    fake = FakeDocker(fail_run_for="postgres")
    monkeypatch.setattr(services, "docker", fake)
    result = runner.run_job(spec_mod.parse_spec(spec(services=["sql", "cache"])))
    assert result["ok"] is False
    assert result["error"] == "service sql failed to start: Error response from daemon: pull access denied"
    removals = [c for c in fake.calls if c[:3] == ["rm", "-f", "-v"]]
    assert len(removals) == 1
    assert len(removals[0]) == 5 and all(n.startswith("wl-trial-") for n in removals[0][3:])


def test_unhealthy_service_times_out_and_is_removed(monkeypatch):
    fake = FakeDocker(unhealthy_for="-cache-")
    quick = dataclasses.replace(services.SERVICES["cache"], ready_timeout=0.3)
    monkeypatch.setitem(services.SERVICES, "cache", quick)
    monkeypatch.setattr(services, "docker", fake)
    result = runner.run_job(spec_mod.parse_spec(spec(services=["cache"])))
    assert result["ok"] is False
    assert result["error"] == "service cache not healthy within 0.3s"
    assert any(c[:3] == ["rm", "-f", "-v"] for c in fake.calls)


def test_missing_docker_binary_is_a_failure_not_a_crash(monkeypatch):
    def absent(args, timeout):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(services, "docker", absent)
    result = runner.run_job(spec_mod.parse_spec(spec(services=["cache"])))
    assert result["ok"] is False
    assert result["error"] == "docker is not installed"


def test_leftovers_lists_only_prefixed_names(monkeypatch):
    out = "wl-trial-unit-001-sql-ab12cd34\nsomething-else\nwl-trial-unit-002-cache-ff00ff00\n"
    monkeypatch.setattr(services, "docker", lambda args, timeout: (0, out, ""))
    assert services.leftovers() == ["wl-trial-unit-001-sql-ab12cd34", "wl-trial-unit-002-cache-ff00ff00"]


def test_leftovers_command_exit_codes(monkeypatch, capsys):
    from workload import cli

    monkeypatch.setattr(services, "docker", lambda args, timeout: (0, "wl-trial-unit-001-sql-ab12cd34\n", ""))
    assert cli.main(["leftovers"]) == 1
    assert "wl-trial-unit-001-sql-ab12cd34" in capsys.readouterr().out
    monkeypatch.setattr(services, "docker", lambda args, timeout: (0, "", ""))
    assert cli.main(["leftovers"]) == 0


@needs_docker
def test_real_cache_service_becomes_healthy_and_is_removed():
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=0.5, services=["cache"])))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = result_of(proc)
    assert result["ok"] is True and result["services_ready_seconds"] > 0
    assert run_cli("leftovers").returncode == 0


@needs_docker
def test_real_unhealthy_service_fails_visibly_and_leaves_nothing(monkeypatch):
    quick = dataclasses.replace(services.SERVICES["cache"], ready_timeout=0.001)
    monkeypatch.setitem(services.SERVICES, "cache", quick)
    result = runner.run_job(spec_mod.parse_spec(spec(services=["cache"])))
    assert result["ok"] is False
    assert "not healthy within" in result["error"]
    assert services.leftovers() == []


@needs_docker
def test_leftovers_command_sees_a_stray_container():
    name = "wl-trial-unit-999-cache-deadbeef"
    image = services.SERVICES["cache"].image
    services.docker(["create", "--name", name, image], 600)
    try:
        proc = run_cli("leftovers")
        assert proc.returncode == 1
        assert name in proc.stdout
    finally:
        services.docker(["rm", "-f", name], 60)
    assert run_cli("leftovers").returncode == 0


@needs_full_docker
@pytest.mark.parametrize("kind", ["sql", "document", "search", "queue"])
def test_each_large_service_type_starts_healthy(kind):
    proc = run_cli("run", "--job-json", json.dumps(spec(id="integration-002", lane="integration",
                                                         cpu_seconds=0.5, services=[kind])), timeout=1800)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result_of(proc)["ok"] is True
    assert run_cli("leftovers").returncode == 0


@needs_full_docker
def test_every_service_type_starts_healthy_together():
    every = list(spec_mod.SERVICE_TYPES)
    proc = run_cli("run", "--job-json", json.dumps(spec(id="integration-001", lane="integration",
                                                         cpu_seconds=0.5, services=every)), timeout=1800)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result_of(proc)["ok"] is True
    assert run_cli("leftovers").returncode == 0
