from __future__ import annotations

import re
import subprocess
from pathlib import Path

ZEROS = re.compile(r"^0+$")


class GitError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {args[0]} failed (exit {proc.returncode})")
    return proc.stdout if proc.returncode == 0 else b""


def decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def repo_root(cwd: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True
    )
    if proc.returncode != 0:
        raise GitError("not inside a git work tree")
    return Path(decode(proc.stdout).strip())


def ls_files(root: Path) -> list[str]:
    raw = git(root, "ls-files", "-z")
    return [decode(p) for p in raw.split(b"\0") if p]


def ls_tree(root: Path, rev: str) -> list[tuple[str, str, str]]:
    raw = git(root, "ls-tree", "-r", "-z", rev)
    out = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        _mode, kind, oid = decode(meta).split(" ")
        out.append((decode(path), kind, oid))
    return out


def object_reader(root: Path):
    proc = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    def read(oid: str) -> tuple[str, bytes]:
        proc.stdin.write(oid.encode() + b"\n")
        proc.stdin.flush()
        header = proc.stdout.readline().split()
        if len(header) != 3:
            raise GitError("cat-file could not read an object")
        data = proc.stdout.read(int(header[2]))
        proc.stdout.read(1)
        return header[1].decode(), data

    return proc, read


def object_exists(root: Path, rev: str, kind: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{rev}^{{{kind}}}"], capture_output=True
    )
    return proc.returncode == 0


def tree_entry_names(data: bytes, oid_len: int) -> list[bytes]:
    names, pos = [], 0
    while pos < len(data):
        space = data.index(b" ", pos)
        end = data.index(b"\0", space)
        names.append(data[space + 1 : end])
        pos = end + 1 + oid_len
    return names


def objects_in_range(root: Path, base: str, head: str, remote: str | None = None) -> list[tuple[str, str]]:
    if ZEROS.match(base) or not object_exists(root, base, "commit"):
        known = f"--remotes={remote}" if remote else "--remotes"
        raw = git(root, "rev-list", "--objects", head, "--not", known)
    else:
        raw = git(root, "rev-list", "--objects", f"{base}..{head}")
    out = []
    for line in decode(raw).split("\n"):
        if line:
            oid, _, path = line.partition(" ")
            out.append((oid, path))
    return out
