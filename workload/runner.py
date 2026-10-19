import json
import signal
import sys
import time

from workload import cpu, memory, services

PREFIX = "workload-result "
ERROR_LIMIT = 200


class Terminated(BaseException):
    pass


def _raise_terminated(signum, frame):
    raise Terminated()


def install_signal_handlers():
    signal.signal(signal.SIGTERM, _raise_terminated)


def progress(text):
    print(f"workload: {text}", file=sys.stderr, flush=True)


def make_result(job_id, ok, cpu_seconds, wall_seconds, peak_rss, ready_seconds, error):
    def rounded(value):
        return None if value is None else round(value, 3)

    return {
        "id": job_id,
        "ok": ok,
        "cpu_seconds": rounded(cpu_seconds),
        "wall_seconds": rounded(wall_seconds),
        "peak_rss_mib": rounded(peak_rss),
        "services_ready_seconds": rounded(ready_seconds),
        "error": None if error is None else " ".join(str(error).split())[:ERROR_LIMIT],
    }


def format_line(result):
    return PREFIX + json.dumps(result, separators=(",", ":"))


def run_job(spec, burn=cpu.run_burn, holder_factory=memory.MemoryHolder, service_factory=services.ServiceSet):
    began = time.monotonic()
    measured = None
    peak = memory.peak_rss_mib()
    ready = None
    error = None
    holder = holder_factory(spec.memory_mib)
    service_set = service_factory(spec.id, spec.services)
    try:
        progress(f"{spec.id} start cpu={spec.cpu_seconds:g}s threads={spec.threads} "
                 f"memory={spec.memory_mib}MiB services={','.join(spec.services) or '-'}")
        holder.start()
        if spec.memory_mib:
            progress(f"{spec.id} memory resident")
        service_set.start()
        ready = service_set.ready_seconds
        if spec.services:
            progress(f"{spec.id} services ready in {ready:.1f}s")
        measured, children = burn(spec.cpu_seconds, spec.threads)
        holder.check_alive()
        if not cpu.cpu_meets_floor(spec.cpu_seconds, measured):
            error = f"cpu below target: measured {measured:.3f}s of {spec.cpu_seconds:g}s requested"
        elif not cpu.crosscheck_ok(measured, children):
            error = f"cpu cross-check failed: workers reported {measured:.3f}s, kernel counted {children:.3f}s"
    except Terminated:
        error = "terminated by signal"
    except (cpu.BurnFailure, memory.MemoryFailure, services.ServiceFailure) as exc:
        error = str(exc)
    except Exception as exc:
        error = f"internal error: {type(exc).__name__}: {exc}"
    finally:
        try:
            holder.release()
        finally:
            service_set.stop()
    peak = _max_peak(peak, holder.peak_rss_mib)
    return make_result(spec.id, error is None, measured, time.monotonic() - began, peak, ready, error)


def _max_peak(a, b):
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None
