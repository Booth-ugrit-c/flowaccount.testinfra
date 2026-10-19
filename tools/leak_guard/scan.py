from __future__ import annotations

import os
from pathlib import Path

from . import gitio
from .denylist import ConfigError, DenyList, mask
from .rules import find_generic

MAX_SHOWN = 200
PATH_REDACTED = "<path hidden: matched a rule>"
HEX_ONLY_HEADERS = ("tree ", "parent ", "object ", "type ")
IDENTITY_HEADERS = ("author", "committer", "tagger")


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


def scan_tree_at(root: Path, deny: DenyList, rev: str) -> tuple[int, list[str], set[str]]:
    findings, scanned, entries = [], set(), gitio.ls_tree(root, rev)
    proc, read = gitio.object_reader(root)
    try:
        for index, (path, kind, oid) in enumerate(entries, 1):
            shown, path_hits = display_path(deny, path, f"{PATH_REDACTED} (tree entry #{index})")
            findings += [f"{shown}:0 path matched a rule"] * path_hits
            if kind == "blob":
                findings += scan_content(deny, shown, read(oid)[1])
                scanned.add(oid)
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()
    return len(entries), findings, scanned


def scan_commit_or_tag(deny: DenyList, tag: str, raw: bytes) -> list[str]:
    header, _, message = raw.partition(b"\n\n")
    findings = []
    for number, line in enumerate(gitio.decode(header).split("\n"), 1):
        if line.lstrip(" ").startswith(HEX_ONLY_HEADERS):
            continue
        key, _, value = line.partition(" ")
        where, text = (key, value) if key in IDENTITY_HEADERS else (f"header:{number}", line)
        findings += [f"{tag} {where} {label}" for label in check_line(deny, text, show=False)]
    return findings + scan_content(deny, f"{tag} message", message)


def scan_new_objects(
    root: Path, deny: DenyList, base: str, head: str, already_scanned: set[str]
) -> tuple[int, list[str]]:
    findings, commits = [], 0
    proc, read = gitio.object_reader(root)
    try:
        for oid, path in gitio.objects_in_range(root, base, head):
            shown = "(unnamed)"
            if path:
                shown, path_hits = display_path(deny, path, f"{PATH_REDACTED} (object {oid[:12]})")
                findings += [f"{shown}:0 path matched a rule"] * path_hits
            kind, data = read(oid)
            if kind == "blob" and oid not in already_scanned:
                findings += scan_content(deny, f"blob {oid[:12]} {shown}", data)
            elif kind in ("commit", "tag"):
                commits += kind == "commit"
                findings += scan_commit_or_tag(deny, f"{kind} {oid[:12]}", data)
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()
    return commits, findings


def scan_push_range(root: Path, deny: DenyList, base: str, head: str) -> tuple[int, int, list[str]]:
    if not gitio.object_exists(root, head, "tree"):
        raise ConfigError("head is not a known object")
    files, findings, scanned = scan_tree_at(root, deny, head)
    commits, walk_findings = scan_new_objects(root, deny, base, head, scanned)
    return files, commits, findings + walk_findings
