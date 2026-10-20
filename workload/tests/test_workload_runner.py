import functools

from workload import cpu, memory, runner, services, spec as spec_mod
from workload.tests import crashers
from workload.tests.helpers import spec


def job(**overrides):
    return spec_mod.parse_spec(spec(**overrides))


class FakeHolder:
    def __init__(self, mib, fail=None):
        self.mib = mib
        self.fail = fail
        self.released = False
        self.peak_rss_mib = None

    def start(self):
        if self.fail:
            raise memory.MemoryFailure(self.fail)

    def check_alive(self):
        pass

    def release(self):
        self.released = True


class FakeServices:
    instances = []

    def __init__(self, job_id, kinds, fail=None):
        self.kinds = kinds
        self.fail = fail
        self.stopped = False
        self.ready_seconds = None
        FakeServices.instances.append(self)

    def start(self):
        if self.fail:
            raise services.ServiceFailure(self.fail)
        self.ready_seconds = 0.5 if self.kinds else None

    def stop(self):
        self.stopped = True


def instant_burn(total, workers):
    return total, None


def test_memory_failure_fails_the_job_and_still_cleans_up():
    holders = []

    def factory(mib):
        holders.append(FakeHolder(mib, fail="allocation of 99 MiB failed (MemoryError)"))
        return holders[-1]

    FakeServices.instances.clear()
    result = runner.run_job(job(memory_mib=99, services=["cache"]), burn=instant_burn,
                            holder_factory=factory, service_factory=FakeServices)
    assert result["ok"] is False
    assert "MemoryError" in result["error"]
    assert result["cpu_seconds"] is None
    assert holders[0].released and FakeServices.instances[0].stopped


def test_service_failure_fails_the_job_and_removes_containers():
    FakeServices.instances.clear()
    factory = functools.partial(FakeServices, fail="service sql not healthy within 60s")
    result = runner.run_job(job(services=["sql"]), burn=instant_burn,
                            holder_factory=FakeHolder, service_factory=factory)
    assert result["ok"] is False
    assert result["error"] == "service sql not healthy within 60s"
    assert FakeServices.instances[0].stopped


def test_unexpected_exception_is_reported_not_raised_and_cleans_up():
    def broken(total, workers):
        raise RuntimeError("boom")

    FakeServices.instances.clear()
    result = runner.run_job(job(services=["cache"]), burn=broken,
                            holder_factory=FakeHolder, service_factory=FakeServices)
    assert result["ok"] is False
    assert result["error"] == "internal error: RuntimeError: boom"
    assert FakeServices.instances[0].stopped


def test_termination_signal_is_reported_and_cleans_up():
    def terminated(total, workers):
        raise runner.Terminated()

    FakeServices.instances.clear()
    result = runner.run_job(job(), burn=terminated, holder_factory=FakeHolder, service_factory=FakeServices)
    assert result["ok"] is False
    assert result["error"] == "terminated by signal"
    assert FakeServices.instances[0].stopped


def test_success_reports_service_ready_time():
    FakeServices.instances.clear()
    result = runner.run_job(job(services=["cache"]), burn=instant_burn,
                            holder_factory=FakeHolder, service_factory=FakeServices)
    assert result["ok"] is True
    assert result["services_ready_seconds"] == 0.5


def test_dead_memory_holder_fails_the_job():
    holder = memory.MemoryHolder(8, target=crashers.killed_holder)
    result = runner.run_job(job(memory_mib=8), burn=instant_burn,
                            holder_factory=lambda mib: holder, service_factory=FakeServices)
    assert result["ok"] is False
    assert "memory holder exited with code 9" in result["error"]


def test_kernel_cross_check_mismatch_fails_the_job():
    result = runner.run_job(job(cpu_seconds=10), burn=lambda total, workers: (10.0, 4.0),
                            holder_factory=FakeHolder, service_factory=FakeServices)
    assert result["ok"] is False
    assert result["error"].startswith("cpu cross-check failed")


def test_real_holder_keeps_memory_until_released():
    holder = memory.MemoryHolder(16)
    holder.start()
    try:
        holder.check_alive()
    finally:
        holder.release()
