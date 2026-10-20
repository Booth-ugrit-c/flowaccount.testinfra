import multiprocessing
import sys
from multiprocessing.connection import wait

PAGE = 4096
MIB = 1024 * 1024


def peak_rss_mib():
    if not sys.platform.startswith("linux"):
        return None
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def touch(mib):
    buf = bytearray(mib * MIB)
    buf[::PAGE] = b"\x01" * len(range(0, len(buf), PAGE))
    return buf


def hold_main(mib, conn):
    try:
        buf = touch(mib)
    except MemoryError:
        conn.send(("error", f"allocation of {mib} MiB failed (MemoryError)"))
        return
    conn.send(("ready", peak_rss_mib()))
    conn.recv()
    del buf


class MemoryFailure(Exception):
    pass


class MemoryHolder:
    def __init__(self, mib, target=hold_main):
        self.mib = mib
        self._target = target
        self._proc = None
        self._conn = None
        self.peak_rss_mib = None

    def start(self):
        if self.mib == 0:
            return
        ctx = multiprocessing.get_context("spawn")
        self._conn, child_end = ctx.Pipe()
        self._proc = ctx.Process(target=self._target, args=(self.mib, child_end), daemon=True)
        self._proc.start()
        child_end.close()
        wait([self._conn, self._proc.sentinel])
        try:
            message = self._conn.recv() if self._conn.poll() else None
        except (EOFError, OSError):
            message = None
        if message is None:
            self._proc.join()
            raise MemoryFailure(f"memory holder exited with code {self._proc.exitcode}")
        if message[0] != "ready":
            raise MemoryFailure(message[1])
        self.peak_rss_mib = message[1]

    def check_alive(self):
        if self._proc is not None and not self._proc.is_alive():
            raise MemoryFailure(f"memory holder exited with code {self._proc.exitcode}")

    def release(self):
        if self._proc is None:
            return
        try:
            if self._proc.is_alive():
                try:
                    self._conn.send("release")
                except OSError:
                    pass
                self._proc.join(5)
            if self._proc.is_alive():
                self._proc.terminate()
            self._proc.join()
        finally:
            self._conn.close()
            self._proc = None
