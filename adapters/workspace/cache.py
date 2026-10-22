from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Optional, Sequence

from adapters.workspace import affected
from core import store as client

KEY_FORMAT = 1
CACHE_DIR = "_cacache"
OBJECT_PREFIX = "npm/"
STAGING_PREFIX = ".ws-cache-"
MAX_EXTRACTED_BYTES = 4 << 30
MAX_MEMBERS = 500_000
INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]{86}==")
BRANCH = re.compile(r"[A-Za-z0-9._/-]+")
REGISTRY = "https://registry.npmjs.org/"


class CacheError(Exception):
    pass


class LockfileError(CacheError):
    pass


class ArchiveError(Exception):
    pass


def verify_lockfile(data: bytes) -> None:
    try:
        lock = json.loads(data)
        packages = lock["packages"]
    except (ValueError, KeyError, TypeError):
        raise LockfileError("package-lock.json has no packages map (lockfileVersion 2 or later)") from None
    if not isinstance(packages, dict):
        raise LockfileError("package-lock.json packages is not an object")
    for path, entry in packages.items():
        if not (path.startswith("node_modules/") or "/node_modules/" in path):
            continue
        if not isinstance(entry, dict):
            raise LockfileError(f"lockfile entry {path} is not an object")
        if entry.get("link") is True or entry.get("inBundle") is True:
            continue
        if not INTEGRITY.fullmatch(str(entry.get("integrity", ""))):
            raise LockfileError(f"lockfile entry {path} has no sha512 integrity; npm could not verify it")
        if not str(entry.get("resolved", "")).startswith(REGISTRY):
            raise LockfileError(f"lockfile entry {path} does not resolve from {REGISTRY}")


def _major(tool: str, override: Optional[str]) -> str:
    if override:
        return override
    found = shutil.which(tool)
    if found is None:
        raise CacheError(f"{tool} is not installed; pass --{tool}")
    try:
        proc = subprocess.run([found, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise CacheError(f"cannot run {tool} --version; pass --{tool}") from None
    match = re.match(r"v?(\d+)\.", proc.stdout.strip())
    if proc.returncode != 0 or not match:
        raise CacheError(f"cannot read the {tool} version; pass --{tool}")
    return match.group(1)


def compute_key(lockfile: bytes, node: str, npm: str, system: str) -> str:
    verify_lockfile(lockfile)
    record = {
        "format": KEY_FORMAT,
        "lockfile": hashlib.sha256(lockfile).hexdigest(),
        "node": node,
        "npm": npm,
        "platform": system,
    }
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _key(args) -> str:
    try:
        lockfile = (Path.cwd() / args.root / "package-lock.json").read_bytes()
    except OSError:
        raise CacheError(f"cannot read {args.root}/package-lock.json") from None
    system = f"{sys.platform}-{platform.machine().lower()}"
    return compute_key(lockfile, _major("node", args.node), _major("npm", args.npm), system)


def _safe_parts(name: str) -> list:
    if not name or "\\" in name or "\0" in name or ":" in name or name.startswith("/"):
        raise ArchiveError("unsafe member name")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[0] != CACHE_DIR:
        raise ArchiveError(f"member outside {CACHE_DIR}")
    return parts


def extract(archive: Path, destination: Path) -> None:
    total = count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            count += 1
            if count > MAX_MEMBERS:
                raise ArchiveError("too many members")
            target = destination.joinpath(*_safe_parts(member.name))
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    raise ArchiveError("archive expands too large")
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as out:
                    shutil.copyfileobj(tar.extractfile(member), out)
                os.chmod(target, 0o644)
            else:
                raise ArchiveError("unsupported member type")


def build_archive(cache_dir: Path, destination: Path) -> None:
    base = cache_dir / CACHE_DIR
    if not base.is_dir() or base.is_symlink():
        raise ArchiveError(f"no {CACHE_DIR} to save")
    for current, directories, files in os.walk(base):
        for entry in directories + files:
            info = os.lstat(os.path.join(current, entry))
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ArchiveError(f"non-regular file in {CACHE_DIR}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ArchiveError(f"hard-linked file in {CACHE_DIR}")

    def scrub(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(destination, "w:gz") as tar:
        tar.add(base, arcname=CACHE_DIR, filter=scrub)


def _object(key: str) -> str:
    return f"{OBJECT_PREFIX}{key}.tar.gz"


def command_key(args) -> int:
    print(_key(args))
    return 0


def command_restore(args) -> int:
    try:
        key = _key(args)
    except LockfileError as exc:
        print("miss")
        print(f"miss: {exc}", file=sys.stderr)
        return 0
    cache_dir = Path(args.dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for leftover in cache_dir.glob(STAGING_PREFIX + "*"):
        shutil.rmtree(leftover, ignore_errors=True)
    work = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=cache_dir))
    try:
        archive = work / "archive.tar.gz"
        staged = work / "staged"
        staged.mkdir()
        try:
            status = client.open_store("READ", args.timeout).get(client.CACHE_BUCKET, _object(key), archive)
            if status != 200:
                print("miss")
                if status != 404:
                    print(f"miss: http {status}", file=sys.stderr)
                return 0
            extract(archive, staged)
            if not (staged / CACHE_DIR).is_dir():
                raise ArchiveError("empty archive")
        except client.StoreError as exc:
            print("miss")
            print(f"miss: {exc.reason}", file=sys.stderr)
            return 0
        except (ArchiveError, tarfile.TarError, EOFError, zlib.error, OSError):
            print("miss")
            print("miss: unusable archive", file=sys.stderr)
            return 0
        target = cache_dir / CACHE_DIR
        if target.exists() or target.is_symlink():
            os.replace(target, work / "previous")
        os.replace(staged / CACHE_DIR, target)
        print("hit")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _skip(reason: str) -> int:
    print(f"skip: {reason}")
    return 0


def _default_branch_tip(cwd: Path) -> bool:
    branch = os.environ.get("SIM_DEFAULT_BRANCH", "")
    if not BRANCH.fullmatch(branch) or branch.startswith("-") or ".." in branch:
        return False
    try:
        tip = affected._git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"], cwd).strip()
        head = affected._git(["rev-parse", "--verify", "HEAD^{commit}"], cwd).strip()
    except affected.GitError:
        return False
    return tip == head


def _clean_tree(cwd: Path) -> bool:
    try:
        return not affected._git(["status", "--porcelain", "--untracked-files=all"], cwd).strip()
    except affected.GitError:
        return False


def command_save(args) -> int:
    cwd = Path.cwd()
    if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        return _skip("not a GitHub-hosted runner")
    if not _default_branch_tip(cwd):
        return _skip("not the default branch tip")
    if not _clean_tree(cwd):
        return _skip("the tree has modified or untracked files")
    try:
        key = _key(args)
    except LockfileError as exc:
        return _skip(str(exc))
    work = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX))
    try:
        archive = work / "archive.tar.gz"
        try:
            store = client.open_store("WRITE", args.timeout)
            build_archive(Path(args.dir), archive)
            if archive.stat().st_size > client.MAX_ARCHIVE_BYTES:
                raise ArchiveError("archive too large")
            status = store.put(client.CACHE_BUCKET, _object(key), archive.read_bytes())
        except client.StoreError as exc:
            return _skip(exc.reason)
        except ArchiveError as exc:
            return _skip(str(exc))
        except OSError:
            return _skip(f"cannot read {CACHE_DIR}")
        if status != 200:
            return _skip(f"http {status}")
        print("saved")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adapters.workspace.cache",
        description=(
            "npm download cache (the _cacache directory of `npm ci --cache DIR`). The key is a sha256 over "
            "package-lock.json, the node and npm major versions and the platform. Every registry entry in the "
            "lockfile must carry a sha512 integrity, so npm verifies each restored tarball. Store settings come "
            "from the environment as for python -m core.store."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=affected.DEFAULT_ROOT, help="workspace root (default: %(default)s)")
    common.add_argument("--node", help="node major version for the key (default: node --version)")
    common.add_argument("--npm", help="npm major version for the key (default: npm --version)")
    stored = argparse.ArgumentParser(add_help=False)
    stored.add_argument("--dir", required=True, help="the npm cache directory passed to npm ci --cache")
    stored.add_argument("--timeout", type=float, default=client.DEFAULT_TIMEOUT,
                        help="seconds one store request may take (default: %(default)s)")
    sub.add_parser("key", parents=[common], help="print the cache key").set_defaults(run=command_key)
    sub.add_parser(
        "restore", parents=[common, stored], help="restore _cacache for the current key",
        description="Prints hit or miss. Any store, archive or lockfile problem is a miss (exit 0).",
    ).set_defaults(run=command_restore)
    sub.add_parser(
        "save", parents=[common, stored], help="upload _cacache under the current key",
        description=("Saves only on a GitHub-hosted runner, at the tip of origin/$SIM_DEFAULT_BRANCH, with a clean "
                     "tree and a writer key; otherwise prints a skip reason (exit 0)."),
    ).set_defaults(run=command_save)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except CacheError as exc:
        print(f"workspace.cache: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
