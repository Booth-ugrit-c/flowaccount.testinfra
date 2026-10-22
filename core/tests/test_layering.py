from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import core
import workflow_policy as policy

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
ADAPTERS = ROOT / "adapters"
FORBIDDEN_MODULES = ("adapters",)
FORBIDDEN_TEXT = re.compile(r"(?<![\w-])adapters[./]")
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


def test_core_imports_nothing_from_adapters():
    found = []
    for path in core_files(include_tests=True):
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [f"{path.relative_to(ROOT).as_posix()}: {module}" for module in imported_modules(tree) if forbidden(module)]
    assert found == []


def private_core_names(tree):
    aliases = {}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "core" or alias.name.startswith("core."):
                    aliases[alias.asname or "core"] = alias.name if alias.asname else "core"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module == "core" or str(node.module).startswith("core.")):
            for alias in node.names:
                if private(alias.name):
                    found.add(f"{node.lineno}: {node.module}.{alias.name}")
                elif node.module == "core":
                    aliases[alias.asname or alias.name] = f"core.{alias.name}"
    for node in ast.walk(tree):
        chain = []
        value = node
        while isinstance(value, ast.Attribute):
            chain.insert(0, value.attr)
            value = value.value
        if chain and isinstance(value, ast.Name) and value.id in aliases and any(private(part) for part in chain):
            found.add(f"{node.lineno}: {aliases[value.id]}.{'.'.join(chain)}")
    return sorted(found)


def private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def test_private_name_detector_sees_every_spelling():
    source = (
        "from core.store import _open_store, sign\n"
        "from core import store as client\n"
        "import core.store\n"
        "import core.gate as gate\n"
        "client._open_store('READ')\n"
        "core.store._hmac(b'', '')\n"
        "gate._lanes\n"
        "client.open_store('READ').get\n"
        "client.__name__\n"
        "other._private\n"
    )
    assert private_core_names(ast.parse(source)) == [
        "1: core.store._open_store", "5: core.store._open_store", "6: core.store._hmac", "7: core.gate._lanes",
    ]


def test_adapters_use_only_public_core_names():
    found = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        if "tests" in path.relative_to(ADAPTERS).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [f"{path.relative_to(ROOT).as_posix()}:{entry}" for entry in private_core_names(tree)]
    assert found == []


def test_an_adapters_package_elsewhere_on_the_path_is_not_run(tmp_path):
    shadow = tmp_path / "adapters" / "dotnet"
    shadow.mkdir(parents=True)
    (shadow.parent / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "affected.py").write_text('print("shadow adapters ran")\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "adapters.dotnet.affected", "--help"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "shadow adapters ran" not in result.stdout + result.stderr
    assert result.returncode == 0 and "adapters.dotnet.affected" in result.stdout


def test_core_production_files_name_no_adapter_path():
    found = []
    for path in core_files(include_tests=False):
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(ROOT).as_posix()}: {match.group(0)}" for match in FORBIDDEN_TEXT.finditer(text)]
    assert found == []


def test_import_detector_sees_every_spelling():
    tree = ast.parse("import adapters.dotnet.cache\nfrom adapters.workspace import cache\nfrom adapters import dotnet\nimport tools.leak_guard\nimport adapters_x\n")
    assert [module for module in imported_modules(tree) if forbidden(module)] == ["adapters.dotnet.cache", "adapters.workspace", "adapters.workspace.cache", "adapters", "adapters.dotnet"]


def test_text_detector_sees_shell_and_module_paths():
    text = "sh adapters/dotnet/x.sh\npython3 -m adapters.workspace.cache\nsim-adapters-y\nmy_adapters.z\n"
    assert [match.group(0) for match in FORBIDDEN_TEXT.finditer(text)] == ["adapters/", "adapters."]


def test_core_resolves_to_this_repository():
    assert {Path(entry).resolve() for entry in core.__path__} == {CORE}
    assert core.__file__ is not None and Path(core.__file__).resolve() == CORE / "__init__.py"


def test_a_core_package_elsewhere_on_the_path_is_not_run(tmp_path):
    shadow = tmp_path / "core"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "gate.py").write_text('print("shadow core ran")\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "core.gate", "--lane", "unit=pass"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "shadow core ran" not in result.stdout + result.stderr
    assert "gate: pass" in result.stdout


def test_workflow_lint_runs_the_policy_and_layering_tests_by_path():
    jobs = policy.parse((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    runs = [str(step.get("run", "")).split() for step in jobs["workflow-lint"]["steps"]]
    pytest_runs = [words for words in runs if words[:3] == ["python", "-m", "pytest"]]
    assert len(pytest_runs) == 1, runs
    assert {"core/tests/test_workflow_policy.py", "core/tests/test_layering.py"} <= set(pytest_runs[0][3:])


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
