from __future__ import annotations

import argparse
import heapq
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

DEFAULT_ROOT = "sim/workspace"
TEST_TARGET = "test"
EXTERNAL_PREFIX = "npm:"
INERT_SUFFIXES = (".md",)
NX_TIMEOUT = 600
SOURCE_SUFFIXES = (".js", ".cjs", ".mjs", ".jsx", ".ts", ".cts", ".mts", ".tsx")
CALL = re.compile(
    r"\b(?:require(?:\.resolve)?|import"
    r"|jest\.(?:mock|doMock|unmock|setMock|requireActual|requireMock|createMockFromModule))"
    r"""\s*\(\s*(?:(['"])([^'"\n]*)\1|`([^`$\n]*)`)?"""
)
STATIC = re.compile(r"""\b(?:from|import)\s*(['"])([^'"\n]*)\1""")
CREATE_REQUIRE = re.compile(r"\bcreateRequire\b")
ROOT_DIR_TOKEN = "<rootDir>/"
SYMLINK_MODE = "120000"
LOCKFILE = "package-lock.json"


class GraphError(Exception):
    pass


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    name: str
    root: str
    dependencies: tuple
    has_test: bool


def _project_root(root: str, value) -> Optional[str]:
    if not isinstance(value, str):
        raise GraphError(f"project root {value!r} is not a string")
    relative = posixpath.normpath(value.replace("\\", "/")) if value else "."
    if relative.startswith(("/", "../")) or relative == ".." or ":" in relative:
        raise GraphError(f"project root {value!r} is outside the workspace")
    if relative == ".":
        return None
    return posixpath.join(root.strip("/"), relative)


def parse_graph(document, root: str) -> dict:
    try:
        nodes = document["graph"]["nodes"]
        edges = document["graph"]["dependencies"]
    except (KeyError, TypeError):
        raise GraphError("the graph file has no graph.nodes or graph.dependencies") from None
    if not isinstance(nodes, dict) or not isinstance(edges, dict):
        raise GraphError("graph.nodes and graph.dependencies must be objects")
    found = {}
    for name, node in nodes.items():
        try:
            data = node["data"]
            targets = data.get("targets") or {}
        except (KeyError, TypeError, AttributeError):
            raise GraphError(f"node {name!r} has no data") from None
        if not isinstance(targets, dict):
            raise GraphError(f"node {name!r} has targets that are not an object")
        found[name] = (_project_root(root, data.get("root")), TEST_TARGET in targets)
    graph = {}
    for name, (project_root, has_test) in found.items():
        dependencies = set()
        for edge in edges.get(name) or ():
            target = edge.get("target") if isinstance(edge, dict) else None
            if not isinstance(target, str):
                raise GraphError(f"node {name!r} has a malformed dependency {edge!r}")
            if target.startswith(EXTERNAL_PREFIX):
                continue
            if target not in found:
                raise GraphError(f"{name} depends on {target}, which is not a project in the graph")
            if target != name:
                dependencies.add(target)
        graph[name] = Project(name, project_root, tuple(sorted(dependencies)), has_test)
    for name in edges:
        if name not in found:
            raise GraphError(f"dependencies list {name}, which is not a project in the graph")
    roots = {}
    for project in graph.values():
        if project.root is not None and project.root in roots:
            raise GraphError(f"{roots[project.root]} and {project.name} share the root {project.root}")
        roots[project.root] = project.name
    if not any(project.has_test for project in graph.values()):
        raise GraphError(f"no project has a {TEST_TARGET} target")
    return graph


def _workspace_patterns(manifest) -> list:
    patterns = manifest.get("workspaces", []) if isinstance(manifest, dict) else None
    if isinstance(patterns, dict):
        patterns = patterns.get("packages", [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise GraphError("package.json workspaces is not a list of patterns")
    return patterns


def check_workspaces(graph: Mapping[str, Project], workspace: Path, root: str) -> None:
    try:
        manifest = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphError(f"cannot read {root}/package.json: {exc}") from None
    tested = {project.root for project in graph.values() if project.has_test}
    for pattern in _workspace_patterns(manifest):
        if pattern.startswith("!") or posixpath.isabs(pattern) or ".." in pattern.split("/"):
            raise GraphError(f"workspace pattern {pattern!r} is not supported")
        for directory in sorted(d for d in workspace.glob(pattern) if d.is_dir()):
            try:
                scripts = json.loads((directory / "package.json").read_text(encoding="utf-8")).get("scripts")
            except FileNotFoundError:
                continue
            except (OSError, ValueError, AttributeError) as exc:
                raise GraphError(f"cannot read {directory / 'package.json'}: {exc}") from None
            if isinstance(scripts, dict) and TEST_TARGET in scripts:
                path = posixpath.join(root.strip("/"), directory.relative_to(workspace).as_posix())
                if path not in tested:
                    raise GraphError(f"{path} has a {TEST_TARGET} script but no {TEST_TARGET} target in the Nx graph")


def _inert(file: str) -> bool:
    return file.lower().endswith(INERT_SUFFIXES)


def _inside(file: str, directory: str) -> bool:
    return file.startswith(directory + "/")


def _in_ancestor(file: str, root: str) -> bool:
    parent = posixpath.dirname(file)
    return parent == "" or root.startswith(parent + "/")


def changed_projects(graph: Mapping[str, Project], changed: Iterable[str], root: str) -> tuple:
    root = root.strip("/")
    seeds = set()
    for file in changed:
        if not _inside(file, root):
            if _in_ancestor(file, root) and not _inert(file):
                return set(), f"{file} is workspace-wide configuration"
            continue
        owners = [p.name for p in graph.values() if p.root is not None and _inside(file, p.root)]
        if owners:
            seeds.update(owners)
        elif not _inert(file):
            return set(), f"{file} belongs to no project"
    return seeds, None


def with_dependents(graph: Mapping[str, Project], seeds: Iterable[str]) -> set:
    dependents = {name: [] for name in graph}
    for project in graph.values():
        for dependency in project.dependencies:
            dependents[dependency].append(project.name)
    found = set(seeds)
    pending = list(found)
    while pending:
        for dependent in dependents[pending.pop()]:
            if dependent not in found:
                found.add(dependent)
                pending.append(dependent)
    return found


def order(graph: Mapping[str, Project], names: Iterable[str]) -> list:
    wanted = set(names)
    waiting = {name: len([d for d in graph[name].dependencies if d in wanted]) for name in wanted}
    dependents = {name: [] for name in wanted}
    for name in wanted:
        for dependency in graph[name].dependencies:
            if dependency in wanted:
                dependents[dependency].append(name)
    ready = [name for name, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    result = []
    while ready:
        name = heapq.heappop(ready)
        result.append(name)
        for dependent in dependents[name]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)
    return result + sorted(wanted - set(result))


def _closure(graph: Mapping[str, Project], name: str) -> set:
    found, pending = set(), [name]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(graph[current].dependencies)
    return found


def _specifiers(text: str) -> tuple:
    found, dynamic = [], False
    for match in CALL.finditer(text):
        literal = match.group(2) if match.group(1) else match.group(3)
        if literal:
            found.append(literal)
        else:
            dynamic = True
    found += [match.group(2) for match in STATIC.finditer(text) if match.group(2)]
    return found, dynamic or bool(CREATE_REQUIRE.search(text))


def _package(specifier: str) -> str:
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") else parts[0]


def _tracked(cwd: Path, root: str) -> list:
    out = _git(["ls-files", "-s", "-z", "--", root], cwd)
    entries = []
    for record in out.decode("utf-8").split("\0"):
        if record:
            meta, _, name = record.partition("\t")
            if "node_modules" not in name.split("/"):
                entries.append((meta.split(" ")[0], name))
    return entries


def _json_paths(node) -> list:
    if isinstance(node, dict):
        return [path for value in node.values() for path in _json_paths(value)]
    if isinstance(node, list):
        return [path for value in node for path in _json_paths(value)]
    if isinstance(node, str):
        text = node[len(ROOT_DIR_TOKEN):] if node.startswith(ROOT_DIR_TOKEN) else node
        return [text] if text.startswith(("./", "../")) or text in (".", "..") else []
    return []


def _scan(cwd: Path, root: str):
    for mode, file in _tracked(cwd, root.strip("/")):
        if mode == SYMLINK_MODE:
            yield file, None, "is a symlink the scan cannot follow"
            continue
        is_json = file.endswith(".json") and posixpath.basename(file) != LOCKFILE
        if not (file.endswith(SOURCE_SUFFIXES) or is_json):
            continue
        try:
            text = (cwd / file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise GraphError(f"cannot read {file}: {exc}") from None
        if is_json:
            try:
                paths = _json_paths(json.loads(text))
            except ValueError:
                yield file, None, "is not plain JSON the scan can read"
                continue
            yield from ((file, path, None) for path in paths)
            continue
        specifiers, dynamic = _specifiers(text)
        if dynamic:
            yield file, None, "has a require or import that is not a string literal"
        yield from ((file, specifier, None) for specifier in specifiers)


def _package_names(graph: Mapping[str, Project], cwd: Path) -> dict:
    names = {name: name for name in graph}
    for project in graph.values():
        if project.root is None:
            continue
        try:
            manifest = json.loads((cwd / project.root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            names.setdefault(manifest["name"], project.name)
    return names


def references(cwd: Path, root: str) -> list:
    return [(file, specifier) for file, specifier, problem in _scan(cwd, root) if problem is None]


def _owners(graph: Mapping[str, Project], path: str) -> set:
    return {p.name for p in graph.values() if p.root is not None and (path == p.root or _inside(path, p.root))}


def undeclared_reference(graph: Mapping[str, Project], cwd: Path, root: str) -> Optional[str]:
    root = root.strip("/")
    packages = _package_names(graph, cwd)
    closures = {name: _closure(graph, name) for name in graph}
    for file, specifier, problem in _scan(cwd, root):
        if problem is not None:
            return f"{file} {problem}"
        if specifier.startswith("/"):
            return f"{file} references the absolute path {specifier}"
        if specifier.startswith(("./", "../")) or specifier in (".", ".."):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(file), specifier))
            if target != root and not _inside(target, root):
                return f"{file} references {specifier} outside the workspace"
            targets = _owners(graph, target)
        elif _package(specifier) in packages:
            targets = {packages[_package(specifier)]}
        else:
            continue
        if not targets:
            continue
        owners = _owners(graph, file)
        if not owners:
            return f"{file} belongs to no project and references {specifier}"
        for owner in owners:
            if not targets & closures[owner]:
                return f"{file} references {specifier} with no graph edge from {owner}"
    return None


def _label(project: Project) -> str:
    return project.root if project.root is not None else project.name


def select(graph: Mapping[str, Project], changed: Iterable[str], root: str, hidden: Optional[str] = None) -> dict:
    seeds, reason = changed_projects(graph, list(changed), root)
    reason = reason or hidden
    chosen = with_dependents(graph, seeds) if reason is None else set()
    if reason is None and not any(graph[name].has_test for name in chosen):
        reason = f"no changed file maps to a project with a {TEST_TARGET} target"
    if reason is not None:
        chosen = set(graph)
    build = order(graph, chosen)
    projects = {
        _label(graph[name]): {
            "name": name,
            "dependencies": [_label(graph[d]) for d in graph[name].dependencies],
            "test": graph[name].has_test,
        }
        for name in order(graph, graph)
    }
    return {
        "build": [_label(graph[name]) for name in build],
        "test": [_label(graph[name]) for name in build if graph[name].has_test],
        "all": reason is not None,
        "reason": reason or "affected",
        "projects": projects,
    }


def _git(args: Sequence[str], cwd: Path) -> bytes:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise GitError("git is not installed") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git {args[0]} failed: {detail[0] if detail else proc.returncode}")
    return proc.stdout


def changed_files(base: str, head: str, cwd: Path) -> list:
    out = _git(["diff", "--name-only", "--no-renames", "-z", f"{base}..{head}", "--"], cwd)
    return [name for name in out.decode("utf-8").split("\0") if name]


def check_checkout(head: str, root: str, cwd: Path) -> None:
    wanted = _git(["rev-parse", "--verify", f"{head}^{{commit}}"], cwd).strip()
    current = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd).strip()
    if wanted != current:
        raise GitError(f"the checkout is not {head}; the Nx graph reads the working tree")
    dirty = _git(["status", "--porcelain", "--untracked-files=all", "--", root.strip("/")], cwd)
    if dirty.strip():
        raise GitError(f"{root} has modified or untracked files; the Nx graph would not match {head}")


def nx_entry(workspace: Path) -> Path:
    package = workspace / "node_modules" / "nx"
    try:
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GraphError(f"nx is not installed under {workspace}; run npm ci first") from None
    entry = manifest.get("bin") if isinstance(manifest, dict) else None
    entry = entry.get("nx") if isinstance(entry, dict) else entry
    if not isinstance(entry, str) or not entry:
        raise GraphError("the nx package declares no nx executable")
    resolved = (package / entry).resolve()
    if not resolved.is_relative_to(package.resolve()) or not resolved.is_file():
        raise GraphError(f"the nx executable {entry!r} is not a file inside the nx package")
    return resolved


def run_nx_graph(workspace: Path) -> dict:
    override = os.environ.get("SIM_NX")
    if override:
        command = [override]
    else:
        node = shutil.which("node")
        if node is None:
            raise GraphError("node is not installed")
        command = [node, str(nx_entry(workspace))]
    env = {**os.environ, "NX_DAEMON": "false", "NX_NO_CLOUD": "true", "NX_TUI": "false"}
    with tempfile.TemporaryDirectory(prefix="ws-graph-") as work:
        target = Path(work) / "graph.json"
        try:
            proc = subprocess.run([*command, "graph", f"--file={target}"], cwd=workspace, env=env,
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=NX_TIMEOUT)
        except (FileNotFoundError, PermissionError):
            raise GraphError(f"cannot run {command[0]}") from None
        except subprocess.TimeoutExpired:
            raise GraphError("nx graph timed out") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise GraphError(f"nx graph failed: {detail[-1] if detail else proc.returncode}")
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GraphError(f"nx graph wrote no readable file: {exc}") from None


def read_graph(root: str, cwd: Path, graph_file: Optional[str]) -> dict:
    workspace = cwd / root
    if graph_file:
        try:
            document = json.loads(Path(graph_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GraphError(f"cannot read {graph_file}: {exc}") from None
    else:
        document = run_nx_graph(workspace)
    graph = parse_graph(document, root)
    check_workspaces(graph, workspace, root)
    return graph


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m adapters.workspace.affected")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--graph", help="an existing `nx graph --file` output for the head checkout")
    parser.add_argument("--names", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    for flag, ref in (("--base", args.base), ("--head", args.head)):
        if ref.startswith("-"):
            print(f"workspace.affected: {flag} {ref!r} must not start with '-'", file=sys.stderr)
            return 2
    cwd = Path.cwd()
    try:
        check_checkout(args.head, args.root, cwd)
        graph = read_graph(args.root, cwd, args.graph)
        hidden = undeclared_reference(graph, cwd, args.root)
        result = select(graph, changed_files(args.base, args.head, cwd), args.root, hidden)
    except (GraphError, GitError) as exc:
        print(f"workspace.affected: {exc}", file=sys.stderr)
        return 2
    if args.names:
        by_label = {label: entry["name"] for label, entry in result["projects"].items()}
        result = {**result, "build": [by_label[p] for p in result["build"]],
                  "test": [by_label[p] for p in result["test"]]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
