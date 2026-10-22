from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import core

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
FORBIDDEN_MODULES = ("adapters", "tools.sim_gate")
FORBIDDEN_TEXT = re.compile(r"(?<![\w-])(adapters|tools[./]sim_gate)[./]")
PRUNED = {"node_modules", "__pycache__", "venv"}


def forbidden(module):
    return any(module == name or module.startswith(name + ".") for name in FORBIDDEN_MODULES)


def imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def core_files(include_tests):
    for path in sorted(CORE.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if not include_tests and "tests" in path.relative_to(CORE).parts:
            continue
        yield path


def test_core_imports_nothing_from_adapters_or_dotnet_tools():
    found = []
    for path in core_files(include_tests=True):
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [f"{path.relative_to(ROOT).as_posix()}: {module}" for module in imported_modules(tree) if forbidden(module)]
    assert found == []


def test_core_production_files_name_no_adapter_path():
    found = []
    for path in core_files(include_tests=False):
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(ROOT).as_posix()}: {match.group(0)}" for match in FORBIDDEN_TEXT.finditer(text)]
    assert found == []


def test_import_detector_sees_every_spelling():
    tree = ast.parse("import adapters.dotnet.cache\nfrom tools.sim_gate import cache\nfrom tools import sim_gate\nimport tools.leak_guard\n")
    assert [module for module in imported_modules(tree) if forbidden(module)] == ["adapters.dotnet.cache", "tools.sim_gate", "tools.sim_gate.cache", "tools.sim_gate"]


def test_text_detector_sees_shell_and_module_paths():
    text = "sh adapters/dotnet/x.sh\npython3 -m tools.sim_gate.cache\nsh tools/sim_gate/x.sh\nsim-adapters-y\n"
    assert [match.group(0) for match in FORBIDDEN_TEXT.finditer(text)] == ["adapters/", "tools.sim_gate.", "tools/sim_gate/"]


def test_core_resolves_to_this_repository():
    assert {Path(entry).resolve() for entry in core.__path__} == {CORE}


def test_every_test_file_sits_under_a_testpath():
    testpaths = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["testpaths"]
    assert "core" in testpaths
    outside = []
    for directory, subdirs, files in os.walk(ROOT):
        subdirs[:] = [name for name in subdirs if not name.startswith(".") and name not in PRUNED]
        for name in files:
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                relative = (Path(directory) / name).relative_to(ROOT)
                if relative.parts[0] not in testpaths:
                    outside.append(relative.as_posix())
    assert outside == []


def test_default_collection_reaches_every_core_test_file():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    collected = {line.split("::", 1)[0] for line in result.stdout.splitlines() if "::" in line}
    expected = {path.relative_to(ROOT).as_posix() for path in (CORE / "tests").glob("test_*.py")}
    assert expected and expected <= collected
