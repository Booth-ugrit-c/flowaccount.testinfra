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


WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _utf8_text(raw: bytes, source: str) -> str:
    text = raw.decode("utf-8-sig", errors="replace")
    if raw.startswith(WIDE_BOMS) or CONTROL_CHARS.search(text):
        raise ConfigError(f"{source} is not UTF-8 text (UTF-16/32 byte marks or control characters found)")
    return text


def _decode_env(encoded: str) -> str:
    try:
        raw = base64.b64decode(encoded, validate=True)
        raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ConfigError(f"{ENV_CONTENT} is not base64 of UTF-8 text") from None
    return _utf8_text(raw, ENV_CONTENT)


def _read(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read deny-list file ({type(exc).__name__})") from None
    return _utf8_text(raw, "deny-list file")


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
