from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import denylist, gitio, scan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.leak_guard")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan").add_argument("--require-denylist", action="store_true")
    push = sub.add_parser("push-range")
    push.add_argument("base")
    push.add_argument("head")
    push.add_argument("--require-denylist", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> int:
    root = gitio.repo_root(Path.cwd())
    deny = denylist.load(root)
    print(f"leak-guard: deny-list {len(deny.entries)} entries (source: {deny.source})")
    if args.require_denylist and not deny.entries:
        raise denylist.ConfigError("--require-denylist set but the deny-list is missing or empty")
    if args.command == "scan":
        files, findings = scan.scan_working_tree(root, deny)
    else:
        files, commits, findings = scan.scan_push_range(root, deny, args.base, args.head)
        print(f"leak-guard: push-range {commits} commits, {args.base[:12]}..{args.head[:12]}")
    for finding in findings:
        print(finding)
    print(f"leak-guard: scanned {files} files, {len(findings)} findings")
    return 1 if findings else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    try:
        return _run(args)
    except (denylist.ConfigError, gitio.GitError) as exc:
        print(f"leak-guard: error: {exc}", file=sys.stderr)
        return 2
