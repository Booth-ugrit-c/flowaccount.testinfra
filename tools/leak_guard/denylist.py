from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .rules import MASK

ENV_CONTENT = "LEAK_GUARD_DENYLIST"
ENV_FILE = "LEAK_GUARD_DENYLIST_FILE"
SIBLING_RELATIVE = Path("..") / "_testinfra-private" / "leak-guard-denylist.txt"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    number: int
    regex: re.Pattern

    def matches(self, text: str) -> list[tuple[int, int]]:
        return [m.span() for m in self.regex.finditer(text) if m.end() > m.start()]


@dataclass(frozen=True)
class DenyList:
    entries: tuple[Entry, ...]
    source: str

    def find(self, text: str) -> list[tuple[int, list[tuple[int, int]]]]:
        hits = []
        for entry in self.entries:
            spans = entry.matches(text)
            if spans:
                hits.append((entry.number, spans))
        return hits


def parse(content: str, source: str) -> DenyList:
    entries = []
    for raw in content.split("\n"):
        line = raw.rstrip("\r").strip()
        if not line or line.startswith("#"):
            continue
        number = len(entries) + 1
        if line.startswith("re:"):
            body = line[3:]
            if not body.strip():
                raise ConfigError(f"deny-list entry #{number}: empty regex")
            try:
                regex = re.compile(body, re.IGNORECASE)
            except re.error:
                raise ConfigError(f"deny-list entry #{number}: invalid regex") from None
            entries.append(Entry(number, regex))
        else:
            entries.append(Entry(number, re.compile(re.escape(line), re.IGNORECASE)))
    return DenyList(tuple(entries), source)


def _decode_env(encoded: str) -> str:
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8-sig")
    except (binascii.Error, UnicodeDecodeError):
        raise ConfigError(f"{ENV_CONTENT} is not base64 of UTF-8 text") from None


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError as exc:
        raise ConfigError(f"cannot read deny-list file ({type(exc).__name__})") from None


def load(repo_root: Path, environ=os.environ) -> DenyList:
    encoded = "".join(environ.get(ENV_CONTENT, "").split())
    if encoded:
        return parse(_decode_env(encoded), "env")
    file_path = environ.get(ENV_FILE, "").strip()
    if file_path:
        return parse(_read(Path(file_path)), "file")
    sibling = repo_root / SIBLING_RELATIVE
    if sibling.is_file():
        return parse(_read(sibling), "file")
    return DenyList((), "none")


def mask(text: str, spans: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for start, end in sorted(spans):
        if start < pos:
            end = max(end, pos)
            start = pos
        out.append(text[pos:start])
        if end > start:
            out.append(MASK)
        pos = max(pos, end)
    out.append(text[pos:])
    return "".join(out)
