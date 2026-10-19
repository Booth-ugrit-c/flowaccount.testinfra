from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterator

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


def blob_reader(root: Path):
    proc = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    def read(oid: str) -> bytes:
        proc.stdin.write(oid.encode() + b"\n")
        proc.stdin.flush()
        header = proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise GitError("cat-file could not read a blob")
        size = int(header[2])
        data = proc.stdout.read(size)
        proc.stdout.read(1)
        return data

    return proc, read


def object_exists(root: Path, rev: str, kind: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{rev}^{{{kind}}}"], capture_output=True
    )
    return proc.returncode == 0


def commits_in_range(root: Path, base: str, head: str) -> list[str]:
    if ZEROS.match(base) or not object_exists(root, base, "commit"):
        raw = git(root, "rev-list", "--reverse", head, "--not", "--remotes")
    else:
        raw = git(root, "rev-list", "--reverse", f"{base}..{head}")
    return decode(raw).split()


def commit_header_and_message(root: Path, sha: str) -> tuple[list[str], str]:
    text = decode(git(root, "cat-file", "commit", sha))
    head, _, message = text.partition("\n\n")
    identities = []
    for line in head.split("\n"):
        for key in ("author ", "committer "):
            if line.startswith(key):
                identities.append(line[len(key):])
    return identities, message


def added_lines(root: Path, sha: str) -> Iterator[tuple[str, int, str]]:
    raw = git(
        root, "-c", "core.quotepath=off", "diff-tree", "--no-commit-id", "-r", "-p", "-m",
        "--root", "-a", "-U0", "--no-renames", "--no-ext-diff", "--no-color", sha,
    )
    lines = raw.split(b"\n")
    path, i = None, 0
    hunk = re.compile(rb"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith(b"+++ "):
            path = _diff_path(line[4:])
        elif line.startswith(b"diff --git "):
            path = None
        elif (m := hunk.match(line)) and path is not None:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            number = int(m.group(2))
            while (old_left > 0 or new_left > 0) and i < len(lines):
                body = lines[i]
                i += 1
                if body.startswith(b"+"):
                    new_left -= 1
                    yield path, number, decode(body[1:])
                    number += 1
                elif body.startswith(b"-"):
                    old_left -= 1


def _diff_path(raw: bytes) -> str | None:
    raw = raw.rstrip(b"\t\r")
    if raw == b"/dev/null":
        return None
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = _unquote(raw[1:-1])
    return decode(raw[2:] if raw[:2] == b"b/" else raw)


def _unquote(raw: bytes) -> bytes:
    simple = {b"t": b"\t", b"n": b"\n", b'"': b'"', b"\\": b"\\"}
    out, i = bytearray(), 0
    while i < len(raw):
        c = raw[i : i + 1]
        if c != b"\\":
            out += c
            i += 1
        elif raw[i + 1 : i + 2] in simple:
            out += simple[raw[i + 1 : i + 2]]
            i += 2
        else:
            out.append(int(raw[i + 1 : i + 4], 8))
            i += 4
    return bytes(out)
