from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

MASK = "***"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    allowed: Optional[Callable[[re.Match], bool]] = None


def _ipv4_allowed(m: re.Match) -> bool:
    octets = [int(o) for o in m.group(0).split(".")]
    if any(o > 255 for o in octets):
        return True
    a, b, c, d = octets
    if a == 127 or octets == [0, 0, 0, 0]:
        return True
    return (a, b, c) in {(192, 0, 2), (198, 51, 100), (203, 0, 113)}


_ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org")
_ALLOWED_EMAIL_ADDRESSES = {"noreply@anthropic.com", "git@github.com"}


def _email_allowed(m: re.Match) -> bool:
    address = m.group(0).lower()
    if address in _ALLOWED_EMAIL_ADDRESSES:
        return True
    domain = address.rsplit("@", 1)[1]
    if domain == "users.noreply.github.com" or domain.endswith(".test"):
        return True
    return any(domain == d or domain.endswith("." + d) for d in _ALLOWED_EMAIL_DOMAINS)


RULES = [
    Rule("twelve-digit-id", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    Rule(
        "ipv4-address",
        re.compile(r"(?<!\d)(?<!\d\.)\d{1,3}(?:\.\d{1,3}){3}(?!\d|\.\d)"),
        _ipv4_allowed,
    ),
    Rule("private-key-header", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    Rule(
        "github-token",
        re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    Rule("aws-access-key-id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    Rule(
        "instance-size-runner-label",
        re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.]+-\d+vcpu-\d+(?:gb)?ram(?![A-Za-z0-9])", re.I),
    ),
    Rule(
        "email-address",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])"),
        _email_allowed,
    ),
]


CI_SKIP_MARKER = re.compile(
    r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]|^\s*skip-checks:\s*true\s*$", re.I
)


def find_generic(line: str) -> list[tuple[str, re.Match]]:
    hits = []
    for rule in RULES:
        for m in rule.pattern.finditer(line):
            if rule.allowed is None or not rule.allowed(m):
                hits.append((rule.name, m))
    return hits
