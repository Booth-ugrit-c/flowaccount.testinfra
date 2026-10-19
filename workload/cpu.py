import multiprocessing
import sys
from multiprocessing.connection import wait
import time

CPU_FLOOR_RATIO = 0.98
CROSSCHECK_SLACK_SECONDS = 0.2
CROSSCHECK_SLACK_RATIO = 0.02
_CHUNK = 20000


def split_cpu(total, workers):
    share = total / workers
    shares = [share] * (workers - 1)
    shares.append(total - sum(shares))
    return shares


def burn_worker(share, conn):
    try:
        x = 1
        while time.process_time() < share:
            for _ in range(_CHUNK):
                x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        conn.send(("ok", time.process_time()))
    except BaseException as exc:
        conn.send(("error", f"{type(exc).__name__}: {exc}"[:120]))
        raise
    finally:
        conn.close()


def children_cpu_seconds():
    if not sys.platform.startswith("linux"):
        return None
    import resource

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def cpu_meets_floor(requested, measured):
    return measured >= CPU_FLOOR_RATIO * requested


def crosscheck_ok(reported, children):
    if children is None:
        return True
    return abs(children - reported) <= CROSSCHECK_SLACK_SECONDS + CROSSCHECK_SLACK_RATIO * reported


class BurnFailure(Exception):
    pass


def run_burn(total, workers, target=burn_worker):
    ctx = multiprocessing.get_context("spawn")
    procs = []
    conns = []
    try:
        before = children_cpu_seconds()
        for share in split_cpu(total, workers):
            recv_end, send_end = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=target, args=(share, send_end), daemon=True)
            proc.start()
            send_end.close()
            procs.append(proc)
            conns.append(recv_end)
        pending = list(range(len(procs)))
        while pending:
            wait([procs[i].sentinel for i in pending])
            for i in [i for i in pending if procs[i].exitcode is not None]:
                pending.remove(i)
                if procs[i].exitcode != 0:
                    raise BurnFailure(f"worker {i} exited with code {procs[i].exitcode}")
        reported = []
        for index, conn in enumerate(conns):
            try:
                message = conn.recv() if conn.poll() else None
            except (EOFError, OSError):
                message = None
            if message is None or message[0] != "ok":
                raise BurnFailure(f"worker {index} sent no result")
            reported.append(message[1])
        after = children_cpu_seconds()
        children = None if before is None else after - before
        return sum(reported), children
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
        for proc in procs:
            proc.join()
        for conn in conns:
            conn.close()
