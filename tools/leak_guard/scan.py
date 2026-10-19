from __future__ import annotations

import os
from pathlib import Path

from . import gitio
from .denylist import ConfigError, DenyList, mask
from .rules import find_generic

MAX_SHOWN = 200
PATH_REDACTED = "<path hidden: matched a rule>"


def check_line(deny: DenyList, line: str, show: bool = True) -> list[str]:
    deny_hits = deny.find(line)
    generic = find_generic(line)
    if not deny_hits and not generic:
        return []
    labels = [f"deny-list entry #{number}" for number, _ in deny_hits]
    if not generic:
        return labels
    spans = [s for _, found in deny_hits for s in found] + [m.span() for _, m in generic]
    masked = mask(line, spans).strip()
    if len(masked) > MAX_SHOWN:
        masked = masked[:MAX_SHOWN] + "..."
    for name in dict.fromkeys(n for n, _ in generic):
        labels.append(f"{name}: {masked}" if show else name)
    return labels


def scan_content(deny: DenyList, location: str, data: bytes, show: bool = True) -> list[str]:
    views = [data] + ([data.replace(b"\x00", b"")] if b"\x00" in data else [])
    findings = []
    for view in views:
        for number, line in enumerate(gitio.decode(view).split("\n"), 1):
            for label in check_line(deny, line.rstrip("\r"), show):
                findings.append(f"{location}:{number} {label}")
    return list(dict.fromkeys(findings))


def display_path(deny: DenyList, path: str, fallback: str) -> tuple[str, int]:
    hits = check_line(deny, path, show=False)
    return (fallback, len(hits)) if hits else (path, 0)


def scan_working_tree(root: Path, deny: DenyList) -> tuple[int, list[str]]:
    findings, paths = [], gitio.ls_files(root)
    for index, path in enumerate(paths, 1):
        shown, path_hits = display_path(deny, path, f"{PATH_REDACTED} (ls-files entry #{index})")
        findings += [f"{shown}:0 path matched a rule"] * path_hits
        full = root / path
        if os.path.islink(full):
            data = os.readlink(full).encode()
        elif os.path.isdir(full):
            data = b""
        elif os.path.isfile(full):
            data = full.read_bytes()
        else:
            raise ConfigError(f"ls-files entry #{index} is missing on disk")
        findings += scan_content(deny, shown, data)
    return len(paths), findings


def scan_tree_at(root: Path, deny: DenyList, rev: str) -> tuple[int, list[str]]:
    findings, entries = [], gitio.ls_tree(root, rev)
    proc, read = gitio.blob_reader(root)
    try:
        for index, (path, kind, oid) in enumerate(entries, 1):
            shown, path_hits = display_path(deny, path, f"{PATH_REDACTED} (tree entry #{index})")
            findings += [f"{shown}:0 path matched a rule"] * path_hits
            if kind == "blob":
                findings += scan_content(deny, shown, read(oid))
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()
    return len(entries), findings


def scan_commit(root: Path, deny: DenyList, sha: str) -> list[str]:
    tag = f"commit {sha[:12]}"
    identities, message = gitio.commit_header_and_message(root, sha)
    findings = []
    for who, line in zip(("author", "committer"), identities):
        findings += [f"{tag} {who} {label}" for label in check_line(deny, line, show=False)]
    findings += scan_content(deny, f"{tag} message", message.encode())
    for path, number, line in gitio.added_lines(root, sha):
        shown, path_hits = display_path(deny, path, PATH_REDACTED)
        findings += [f"{tag} {shown}:{number} path matched a rule"] * path_hits
        findings += [f"{tag} {shown}:{number} {label}" for label in check_line(deny, line.rstrip("\r"))]
    return findings


def scan_push_range(root: Path, deny: DenyList, base: str, head: str) -> tuple[int, int, list[str]]:
    if not gitio.object_exists(root, head, "tree"):
        raise ConfigError("head is not a known object")
    findings = []
    if gitio.git(root, "cat-file", "-t", head).strip() == b"tag":
        findings += scan_content(deny, "tag object", gitio.git(root, "cat-file", "tag", head))
    commits = gitio.commits_in_range(root, base, head)
    files, tree_findings = scan_tree_at(root, deny, head)
    findings += tree_findings
    for sha in commits:
        findings += scan_commit(root, deny, sha)
    return files, len(commits), findings
