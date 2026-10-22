from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Optional, Sequence

from adapters.dotnet import affected
from core import store as client

OUTPUT_DIRS = ("bin", "obj")
MAX_EXTRACTED_BYTES = 4 << 30
MAX_MEMBERS = 200_000
CHUNK = 1 << 16
KEY_FORMAT = 2
STAGING_PREFIX = ".sim-cache-"


class CacheError(Exception):
    pass


class ArchiveError(Exception):
    pass


def _git(args: Sequence[str], cwd: Path) -> bytes:
    try:
        return affected._git(args, cwd)
    except affected.GitError as exc:
        raise CacheError(str(exc)) from None


def _relative(top: Path, target: str) -> str:
    try:
        relative = os.path.relpath(os.path.realpath(target), os.path.realpath(top)).replace(os.sep, "/")
    except ValueError:
        relative = ".."
    if relative == ".." or relative.startswith("../") or os.path.isabs(relative):
        raise CacheError(f"{target} is outside the repository")
    return "" if relative == "." else relative


def _locate(cwd: Path, root: str) -> tuple:
    top = Path(_git(["rev-parse", "--show-toplevel"], cwd).decode("utf-8").strip())
    return top, _relative(top, os.path.join(cwd, root))


def _under(path: str, directory: str) -> bool:
    return directory == "" or path.startswith(directory + "/")


def _file_digest(path: Path) -> Optional[str]:
    if path.is_symlink():
        raise CacheError(f"{path.name} is a symlink; a link into another project would not be tracked")
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_keys(cwd: Path, root: str, config: str, sdk: str) -> dict:
    top, root_rel = _locate(Path(cwd), root)
    segments = root_rel.split("/") if root_rel else []
    ancestors = {"/".join(segments[:n]) for n in range(len(segments))}
    specs = [f":(glob){a}/*" if a else ":(glob)*" for a in sorted(ancestors)]
    listing = sorted(set(
        name for name in _git(["ls-files", "-z", "--cached", "--", root_rel or ".", *specs], top)
        .decode("utf-8").split("\0") if name
    ))
    under_root = [name for name in listing if _under(name, root_rel)]
    for name in under_root:
        if any(part in OUTPUT_DIRS for part in (name[len(root_rel) + 1:] if root_rel else name).split("/")[:-1]):
            raise CacheError(f"{name} is tracked inside a bin or obj directory")
    sources = {}
    for name in under_root:
        if name.endswith(".csproj"):
            sources[name] = (top / name).read_bytes()
    if not sources:
        raise CacheError(f"no .csproj found under {root}")
    try:
        graph = affected.build_graph(sources)
        order = affected.topological_order(graph)
    except affected.GraphError as exc:
        raise CacheError(str(exc)) from None

    digests = {}

    def digest(name: str) -> Optional[str]:
        if name not in digests:
            digests[name] = _file_digest(top / name)
        return digests[name]

    shared = {
        name: digest(name) for name in listing
        if affected._is_build_wide(posixpath.basename(name))
        and (name in under_root or posixpath.dirname(name) in ancestors)
    }
    by_directory = {}
    for path, project in graph.items():
        by_directory.setdefault(project.directory, []).append(path)
    owned = {path: [] for path in graph}
    for name in under_root:
        directory = posixpath.dirname(name)
        claimed = False
        while True:
            for path in by_directory.get(directory, ()):
                claimed = True
                if digest(name) is not None:
                    owned[path].append([name, digest(name)])
            if not directory:
                break
            directory = posixpath.dirname(directory)
        if not claimed and not affected._is_inert(posixpath.basename(name)):
            shared[name] = digest(name)
    wide = sorted([name, value] for name, value in shared.items() if value is not None)

    keys = {}
    for path in order:
        record = {
            "format": KEY_FORMAT,
            "sdk": sdk,
            "config": config,
            "wide": wide,
            "files": sorted(owned[path]),
            "references": sorted([ref, keys[ref]] for ref in graph[path].references),
        }
        keys[path] = hashlib.sha256(json.dumps(record, separators=(",", ":")).encode("utf-8")).hexdigest()
    return keys


def _sdk_version(args, cwd: Path) -> str:
    if args.sdk:
        return args.sdk
    top, root_rel = _locate(cwd, args.root)
    try:
        proc = subprocess.run(["dotnet", "--version"], cwd=top / root_rel, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise CacheError("cannot determine the SDK version; install dotnet or pass --sdk") from None
    version = proc.stdout.strip()
    if proc.returncode != 0 or not version:
        raise CacheError("cannot determine the SDK version; pass --sdk")
    return version


def _project_key(args) -> tuple:
    cwd = Path.cwd()
    keys = compute_keys(cwd, args.root, args.config, _sdk_version(args, cwd))
    top, _ = _locate(cwd, args.root)
    project = _relative(top, os.path.join(cwd, args.project))
    if project not in keys:
        raise CacheError(f"{args.project} is not a project under {args.root}")
    return top / project, keys[project]


def _safe_parts(name: str) -> list:
    if not name or "\\" in name or "\0" in name or ":" in name or name.startswith("/"):
        raise ArchiveError("unsafe member name")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[0] not in OUTPUT_DIRS:
        raise ArchiveError("member outside bin and obj")
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
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                os.utime(target, (member.mtime, member.mtime))
            else:
                raise ArchiveError("unsupported member type")


def build_archive(project_dir: Path, destination: Path) -> None:
    def scrub(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    found = False
    with tarfile.open(destination, "w:gz") as tar:
        for name in OUTPUT_DIRS:
            base = project_dir / name
            if not base.is_dir() or base.is_symlink():
                continue
            found = True
            for current, directories, files in os.walk(base):
                for entry in directories + files:
                    info = os.lstat(os.path.join(current, entry))
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        raise ArchiveError("non-regular file in bin or obj")
                    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        raise ArchiveError("hard-linked file in bin or obj")
            tar.add(base, arcname=name, filter=scrub)
    if not found:
        raise ArchiveError("no bin or obj to save")


def _miss(reason: Optional[str] = None) -> int:
    print("miss")
    if reason:
        print(f"miss: {reason}", file=sys.stderr)
    return 0


def command_restore(args) -> int:
    project, key = _project_key(args)
    project_dir = project.parent
    for leftover in project_dir.glob(STAGING_PREFIX + "*"):
        shutil.rmtree(leftover, ignore_errors=True)
    work = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=project_dir))
    try:
        archive = work / "archive.tar.gz"
        staged = work / "staged"
        staged.mkdir()
        try:
            status = client.open_store("READ", args.timeout).get(client.CACHE_BUCKET, f"{key}.tar.gz", archive)
            if status == 404:
                return _miss()
            if status != 200:
                return _miss(f"http {status}")
            extract(archive, staged)
            present = [name for name in OUTPUT_DIRS if (staged / name).is_dir()]
            if not present:
                raise ArchiveError("empty archive")
        except client.StoreError as exc:
            return _miss(exc.reason)
        except (ArchiveError, tarfile.TarError, EOFError, zlib.error, OSError):
            return _miss("unusable archive")
        for name in present:
            target = project_dir / name
            if target.exists() or target.is_symlink():
                os.replace(target, work / f"previous-{name}")
            os.replace(staged / name, target)
        print("hit")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def command_save(args) -> int:
    project, key = _project_key(args)
    if client.credentials("WRITE") is None:
        print("skip: no writer key")
        return 0
    work = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=project.parent))
    try:
        archive = work / "archive.tar.gz"
        try:
            store = client.open_store("WRITE", args.timeout)
            build_archive(project.parent, archive)
            if archive.stat().st_size > client.MAX_ARCHIVE_BYTES:
                raise ArchiveError("archive too large")
            status = store.put(client.CACHE_BUCKET, f"{key}.tar.gz", archive.read_bytes())
        except client.StoreError as exc:
            print(f"skip: {exc.reason}", file=sys.stderr)
            return 0
        except (ArchiveError, OSError) as exc:
            print(f"skip: {exc if isinstance(exc, ArchiveError) else 'cannot read the build output'}",
                  file=sys.stderr)
            return 0
        if status != 200:
            print(f"skip: http {status}", file=sys.stderr)
            return 0
        print("saved")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def command_key(args) -> int:
    print(_project_key(args)[1])
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adapters.dotnet.cache",
        description=(
            "Per-project build cache. The key is a sha256 over the project's tracked files as they are in the "
            "working tree (what the build compiles, so an uncommitted edit changes the key), the keys of its "
            "ProjectReferences, every build-wide file under the root and in its ancestors, every other tracked "
            "non-inert file under the root that no project owns, the SDK version and "
            "the configuration. Untracked files and bin/obj never enter the key. Credentials come from the "
            "environment only: SIM_CACHE_ENDPOINT, SIM_CACHE_READ_KEY_ID, SIM_CACHE_READ_SECRET, "
            "SIM_CACHE_WRITE_KEY_ID, SIM_CACHE_WRITE_SECRET, SIM_CACHE_REGION (default us-east-1). A plain http:// "
            "endpoint is acceptable only over an encrypted overlay (tailnet) or an isolated lab segment. "
            "Restore removes leftover .sim-cache-* staging directories in the project, so run one restore or "
            "save per project at a time."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", required=True, help="path to the .csproj")
    common.add_argument("--root", default=affected.DEFAULT_ROOT, help="solution root (default: %(default)s)")
    common.add_argument("--config", default="Release", help="build configuration (default: %(default)s)")
    common.add_argument("--sdk", help="SDK version for the key (default: dotnet --version in the root)")
    timed = argparse.ArgumentParser(add_help=False)
    client.add_timeout(timed)
    sub.add_parser("key", parents=[common], help="print the content key of a project").set_defaults(run=command_key)
    sub.add_parser(
        "restore", parents=[common, timed],
        help="restore bin/obj for the current key on a hit",
        description=("Fetch the entry for the CURRENT key only and extract bin/obj into the project directory. "
                     "Prints hit or miss. An unreachable store, a timeout, an HTTP error or an unusable archive "
                     "is a miss (exit 0, reason on stderr), never a failure."),
    ).set_defaults(run=command_restore)
    sub.add_parser(
        "save", parents=[common, timed],
        help="upload bin/obj under the current key",
        description=("Upload bin/obj under the current key. Run it only after a successful build of this "
                     "project: the client cannot tell a failed or partial build from a good one. Needs "
                     "SIM_CACHE_WRITE_KEY_ID and SIM_CACHE_WRITE_SECRET; without both it prints "
                     "'skip: no writer key' and sends nothing. Store problems are skips (exit 0)."),
    ).set_defaults(run=command_save)
    client.add_tripwire(sub)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except CacheError as exc:
        print(f"dotnet.cache: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
