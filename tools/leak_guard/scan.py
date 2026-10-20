from __future__ import annotations

import os
import re
from pathlib import Path

from . import gitio
from .denylist import ConfigError, DenyList, mask
from .rules import CI_SKIP_MARKER, find_generic

MAX_SHOWN = 200
PATH_REDACTED = "<path hidden: matched a rule>"
HEX_ONLY_HEADER = re.compile(
    r"^(?:mergetag)? ?(?:(?:tree|parent|object) [0-9a-f]{40,64}|type (?:commit|tree|blob|tag))$"
)
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
        if HEX_ONLY_HEADER.match(line):
            continue
        key, _, value = line.partition(" ")
        where, text = (key, value) if key in IDENTITY_HEADERS else (f"header:{number}", line)
        findings += [f"{tag} {where} {label}" for label in check_line(deny, text, show=False)]
    findings += scan_content(deny, f"{tag} message", message)
    if tag.startswith("commit"):
        for number, line in enumerate(gitio.decode(message).split("\n"), 1):
            if CI_SKIP_MARKER.search(line):
                findings.append(f"{tag} message:{number} ci-skip-marker")
    return findings


def scan_tree_names(deny: DenyList, location: str, data: bytes, oid_len: int) -> list[str]:
    findings = []
    for name in gitio.tree_entry_names(data, oid_len):
        text = gitio.decode(name)
        pieces = {text, text.replace("\x00", ""), *text.split("\n")}
        labels = dict.fromkeys(label for piece in pieces for label in check_line(deny, piece, show=False))
        findings += [f"{location} name {label}" for label in labels]
    return findings


def scan_object_list(
    root: Path, deny: DenyList, objects: list[tuple[str, str]], already_scanned: set[str]
) -> tuple[int, int, list[str]]:
    findings, commits, blobs = [], 0, 0
    proc, read = gitio.object_reader(root)
    try:
        for oid, path in objects:
            shown = "(unnamed)"
            if path:
                shown, path_hits = display_path(deny, path, f"{PATH_REDACTED} (object {oid[:12]})")
                findings += [f"{shown}:0 path matched a rule"] * path_hits
            kind, data = read(oid)
            if kind == "blob" and oid not in already_scanned:
                blobs += 1
                findings += scan_content(deny, f"blob {oid[:12]} {shown}", data)
            elif kind == "tree":
                findings += scan_tree_names(deny, f"tree {oid[:12]}", data, len(oid) // 2)
            elif kind in ("commit", "tag"):
                commits += kind == "commit"
                findings += scan_commit_or_tag(deny, f"{kind} {oid[:12]}", data)
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()
    return commits, blobs, findings


def scan_ref_names(deny: DenyList, names: list[str]) -> list[str]:
    findings = []
    for index, name in enumerate(names, 1):
        findings += scan_content(deny, f"ref #{index}", name.encode("utf-8"), show=False)
    return findings


def scan_all(
    root: Path, deny: DenyList, prefixes: list[str] | None = None
) -> tuple[int, int, int, list[str]]:
    if prefixes:
        chosen = [(n, o) for n, o in gitio.refs_with_ids(root) if n.startswith(tuple(prefixes))]
        if not chosen:
            raise ConfigError("--refs matched no refs, nothing would be scanned")
        names = [n for n, _ in chosen]
        objects = gitio.objects_from(root, [o for _, o in chosen])
    else:
        names, objects = gitio.ref_names(root), gitio.objects_all(root)
    commits, blobs, findings = scan_object_list(root, deny, objects, set())
    return commits, len(names), blobs, scan_ref_names(deny, names) + findings


def scan_push_range(
    root: Path, deny: DenyList, base: str, head: str, remote: str | None = None
) -> tuple[int, int, list[str]]:
    if not gitio.object_exists(root, head, "tree"):
        raise ConfigError("head is not a known object")
    files, findings, scanned = scan_tree_at(root, deny, head)
    objects = gitio.objects_in_range(root, base, head, remote)
    commits, _, walk_findings = scan_object_list(root, deny, objects, scanned)
    return files, commits, findings + walk_findings
