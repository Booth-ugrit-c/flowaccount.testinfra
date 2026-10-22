from __future__ import annotations

import argparse
import heapq
import json
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

DEFAULT_ROOT = "sim/dotnet"
BUILD_WIDE_NAMES = frozenset({"global.json", "nuget.config"})
BUILD_WIDE_PREFIXES = ("directory.build.", "directory.packages.")
BUILD_WIDE_SUFFIXES = (".sln", ".slnx")
INERT_NAMES = frozenset({".gitignore", ".gitattributes"})
INERT_SUFFIXES = (".md", ".txt")
TEST_SDK_PACKAGE = "microsoft.net.test.sdk"
PATH_ATTRIBUTES = ("Include", "Update", "Remove")
NAME_ITEMS = frozenset({
    "ProjectReference", "PackageReference", "PackageVersion", "GlobalPackageReference",
    "FrameworkReference", "Using", "InternalsVisibleTo",
})


class GraphError(Exception):
    pass


class CycleError(GraphError):
    pass


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    path: str
    name: str
    directory: str
    references: tuple
    is_test: bool


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _may_leave_directory(value: str) -> bool:
    if "$(" in value or "@(" in value or "%(" in value:
        return True
    relative = posixpath.normpath(value.strip().replace("\\", "/"))
    return relative == ".." or relative.startswith(("../", "/")) or re.match(r"[A-Za-z]:", relative) is not None


def _untracked_paths(path: str, tag: str, element) -> None:
    attributes = (*PATH_ATTRIBUTES, "Project") if tag == "Import" else PATH_ATTRIBUTES
    for attribute in attributes:
        for value in (element.get(attribute) or "").split(";"):
            if value.strip() and _may_leave_directory(value):
                raise GraphError(
                    f"{path}: <{tag} {attribute}='{value.strip()}'> may reach outside the project directory; "
                    "that dependency would not be tracked"
                )


def parse_project(path: str, content: Union[str, bytes]) -> Project:
    data = content.encode("utf-8") if isinstance(content, str) else content
    try:
        tree = ET.fromstring(data)
    except ET.ParseError as exc:
        raise GraphError(f"{path}: not well-formed XML: {exc}") from None
    directory = posixpath.dirname(path)
    references = set()
    is_test = False
    for element in tree.iter():
        tag = _local(element.tag)
        if tag not in NAME_ITEMS:
            _untracked_paths(path, tag, element)
        if tag == "HintPath" and (element.text or "").strip() and _may_leave_directory(element.text):
            raise GraphError(
                f"{path}: <HintPath>{element.text.strip()}</HintPath> may reach outside the project directory; "
                "that dependency would not be tracked"
            )
        if tag == "ProjectReference":
            include = element.get("Include")
            if include is None:
                continue
            if "$(" in include or "@(" in include or "%(" in include:
                raise GraphError(f"{path}: ProjectReference {include!r} holds an MSBuild expression that cannot be resolved")
            references.add(posixpath.normpath(posixpath.join(directory, include.strip().replace("\\", "/"))))
        elif tag == "IsTestProject" and (element.text or "").strip().lower() == "true":
            is_test = True
        elif tag == "PackageReference" and (element.get("Include") or "").strip().lower() == TEST_SDK_PACKAGE:
            is_test = True
    name = posixpath.basename(path).rpartition(".")[0]
    return Project(path, name, directory, tuple(sorted(references)), is_test)


def build_graph(files: Mapping[str, Union[str, bytes]]) -> dict:
    graph = {}
    by_name = {}
    for path in sorted(files):
        project = parse_project(path, files[path])
        if project.name in by_name:
            raise GraphError(f"two projects are named {project.name}: {by_name[project.name]} and {path}")
        by_name[project.name] = path
        graph[path] = project
    for project in graph.values():
        for reference in project.references:
            if reference not in graph:
                raise GraphError(f"{project.path} references {reference}, which is not a project under the root")
    return graph


def _cycle_through(graph: Mapping[str, Project], remaining: set) -> list:
    node = min(remaining)
    walk = []
    seen = {}
    while node not in seen:
        seen[node] = len(walk)
        walk.append(node)
        node = min(ref for ref in graph[node].references if ref in remaining)
    return walk[seen[node]:] + [node]


def topological_order(graph: Mapping[str, Project]) -> list:
    waiting = {path: len(project.references) for path, project in graph.items()}
    dependents = {path: [] for path in graph}
    for project in graph.values():
        for reference in project.references:
            dependents[reference].append(project.path)
    ready = [path for path, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        path = heapq.heappop(ready)
        order.append(path)
        for dependent in dependents[path]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) != len(graph):
        cycle = _cycle_through(graph, set(graph) - set(order))
        raise CycleError("project reference cycle: " + " -> ".join(graph[path].name for path in cycle))
    return order


def _is_build_wide(name: str) -> bool:
    lower = name.lower()
    return lower in BUILD_WIDE_NAMES or lower.startswith(BUILD_WIDE_PREFIXES) or lower.endswith(BUILD_WIDE_SUFFIXES)


def _is_inert(name: str) -> bool:
    lower = name.lower()
    return lower in INERT_NAMES or lower.endswith(INERT_SUFFIXES)


def _in_ancestor(file: str, root: str) -> bool:
    parent = posixpath.dirname(file)
    return parent == "" or root.startswith(parent + "/")


def _inside(file: str, directory: str) -> bool:
    return directory == "" or file.startswith(directory + "/")


def changed_projects(graph: Mapping[str, Project], changed: Iterable[str], root: str) -> set:
    prefix = root.strip("/") + "/"
    seeds = set()
    for file in changed:
        name = posixpath.basename(file)
        if not file.startswith(prefix):
            if _is_build_wide(name) and _in_ancestor(file, prefix.rstrip("/")):
                return set(graph)
            continue
        if _is_build_wide(name):
            return set(graph)
        owners = [project.path for project in graph.values() if _inside(file, project.directory)]
        if owners:
            seeds.update(owners)
        elif not _is_inert(name):
            return set(graph)
    return seeds


def with_dependents(graph: Mapping[str, Project], seeds: Iterable[str]) -> set:
    dependents = {path: [] for path in graph}
    for project in graph.values():
        for reference in project.references:
            dependents[reference].append(project.path)
    found = set(seeds)
    pending = list(found)
    while pending:
        for dependent in dependents[pending.pop()]:
            if dependent not in found:
                found.add(dependent)
                pending.append(dependent)
    return found


def select(graph: Mapping[str, Project], changed: Iterable[str], root: str) -> dict:
    order = topological_order(graph)
    affected = with_dependents(graph, changed_projects(graph, changed, root))
    build = [path for path in order if path in affected]
    return {"build": build, "test": [path for path in build if graph[path].is_test]}


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


def read_projects(head: str, root: str, cwd: Path) -> dict:
    out = _git(["ls-tree", "-r", "-z", "--name-only", "--full-tree", head, "--", root.strip("/")], cwd)
    paths = [name for name in out.decode("utf-8").split("\0") if name.endswith(".csproj")]
    return {path: _git(["show", f"{head}:{path}"], cwd) for path in paths}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.sim_gate.affected")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--names", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    for flag, ref in (("--base", args.base), ("--head", args.head)):
        if ref.startswith("-"):
            print(f"sim_gate.affected: {flag} {ref!r} must not start with '-'", file=sys.stderr)
            return 2
    cwd = Path.cwd()
    try:
        files = read_projects(args.head, args.root, cwd)
        if not files:
            raise GraphError(f"no .csproj found under {args.root} at {args.head}")
        graph = build_graph(files)
        result = select(graph, changed_files(args.base, args.head, cwd), args.root)
    except (GraphError, GitError) as exc:
        print(f"sim_gate.affected: {exc}", file=sys.stderr)
        return 2
    if args.names:
        result = {key: [graph[path].name for path in paths] for key, paths in result.items()}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
