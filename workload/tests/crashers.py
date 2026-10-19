import os
import time


def killed_worker(share, conn):
    os._exit(9)


def early_stop_worker(share, conn):
    target = share * 0.5
    while time.process_time() < target:
        pass
    conn.send(("ok", time.process_time()))
    conn.close()


def silent_worker(share, conn):
    conn.close()


def killed_holder(mib, conn):
    os._exit(9)


def swallowing_holder(mib, conn):
    conn.send(("ready", 1.0))
    conn.recv()
