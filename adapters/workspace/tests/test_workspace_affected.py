import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.workspace import affected

ROOT = "sim/workspace"
SHARED, ALPHA, BETA, GAMMA, SOLO = (f"{ROOT}/{p}" for p in ("libs/shared", "libs/alpha", "libs/beta", "apps/gamma", "libs/solo"))
ALL_TESTS = {SHARED, ALPHA, BETA, GAMMA, SOLO}
GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
       "-c", "core.autocrlf=false"]


def node(root, test=True):
    return {"name": root, "type": "lib", "data": {"root": root, "targets": {"test": {}} if test else {"lint": {}}}}


def edges(source, *targets):
    return [{"source": source, "target": t, "type": "static"} for t in targets]


GRAPH = {
    "graph": {
        "nodes": {
            "@sim/shared": node("libs/shared"),
            "@sim/alpha": node("libs/alpha"),
            "@sim/beta": node("libs/beta"),
            "@sim/gamma": node("apps/gamma"),
            "@sim/solo": node("libs/solo"),
        },
        "dependencies": {
            "@sim/shared": edges("@sim/shared", "npm:left-pad"),
            "@sim/alpha": edges("@sim/alpha", "@sim/shared"),
            "@sim/beta": edges("@sim/beta", "@sim/shared"),
            "@sim/gamma": edges("@sim/gamma", "@sim/alpha", "@sim/beta"),
            "@sim/solo": [],
        },
    }
}


def graph_with(change=None):
    document = copy.deepcopy(GRAPH)
    if change:
        change(document["graph"])
    return document


def pick(changed, document=GRAPH):
    return affected.select(affected.parse_graph(document, ROOT), changed, ROOT)


def test_one_lib_change_selects_that_lib_and_its_dependents_only():
    result = pick([f"{ALPHA}/index.js"])
    assert set(result["test"]) == {ALPHA, GAMMA}
    assert result["all"] is False


def test_shared_lib_change_selects_every_dependent_but_not_the_unrelated_lib():
    result = pick([f"{SHARED}/index.js"])
    assert set(result["test"]) == {SHARED, ALPHA, BETA, GAMMA}
    assert result["build"][0] == SHARED and result["build"][-1] == GAMMA


@pytest.mark.parametrize("file", [f"{ROOT}/nx.json", f"{ROOT}/package.json", f"{ROOT}/package-lock.json",
                                  f"{ROOT}/oracle.js", f"{ROOT}/libs/unknown/index.js"])
def test_file_owned_by_no_project_selects_all(file):
    result = pick([f"{ALPHA}/index.js", file])
    assert set(result["test"]) == ALL_TESTS
    assert result["all"] is True and file in result["reason"]


@pytest.mark.parametrize("file", ["package.json", ".npmrc", "sim/tsconfig.base.json"])
def test_configuration_in_an_ancestor_directory_selects_all(file):
    result = pick([f"{ALPHA}/index.js", file])
    assert set(result["test"]) == ALL_TESTS and result["all"] is True


def test_markdown_beside_a_project_change_does_not_widen_the_selection():
    result = pick([f"{ALPHA}/index.js", f"{ROOT}/README.md", "README.md"])
    assert set(result["test"]) == {ALPHA, GAMMA}


def test_file_in_an_unrelated_directory_is_ignored():
    assert set(pick([f"{ALPHA}/index.js", "tools/other.py", "sim/dotnet/global.json"])["test"]) == {ALPHA, GAMMA}


@pytest.mark.parametrize("changed", [[], [f"{ROOT}/README.md"], ["tools/other.py"]])
def test_a_change_that_reaches_no_test_project_selects_all_never_none(changed):
    result = pick(changed)
    assert set(result["test"]) == ALL_TESTS and result["all"] is True


def test_project_without_a_test_target_alone_selects_all():
    def add_docs(g):
        g["nodes"]["@sim/docs"] = node("libs/docs", test=False)
    result = pick([f"{ROOT}/libs/docs/page.js"], graph_with(add_docs))
    assert set(result["test"]) == ALL_TESTS and result["all"] is True


def test_file_in_a_nested_project_seeds_every_enclosing_project():
    def nest(g):
        g["nodes"]["@sim/inner"] = node("libs/alpha/inner")
    result = pick([f"{ALPHA}/inner/index.js"], graph_with(nest))
    assert set(result["test"]) == {ALPHA, f"{ALPHA}/inner", GAMMA}


def test_workspace_root_project_owns_no_file():
    def root_project(g):
        g["nodes"]["sim-workspace"] = node(".")
    result = pick([f"{ALPHA}/index.js", f"{ROOT}/nx.json"], graph_with(root_project))
    assert result["all"] is True and "sim-workspace" in result["test"]


def test_dependency_cycle_still_selects_every_member():
    def cycle(g):
        g["dependencies"]["@sim/shared"] = edges("@sim/shared", "@sim/gamma")
    result = pick([f"{SHARED}/index.js"], graph_with(cycle))
    assert set(result["test"]) == {SHARED, ALPHA, BETA, GAMMA}


@pytest.mark.parametrize("change", [
    lambda g: g["dependencies"]["@sim/alpha"].append({"source": "@sim/alpha", "target": "@sim/missing"}),
    lambda g: g["dependencies"].update({"@sim/ghost": []}),
    lambda g: g["nodes"]["@sim/alpha"]["data"].update(root="../outside"),
    lambda g: g["nodes"]["@sim/beta"]["data"].update(root="libs/alpha"),
    lambda g: g["nodes"].update({k: node(v["data"]["root"], test=False) for k, v in g["nodes"].items()}),
    lambda g: g["nodes"]["@sim/alpha"].pop("data"),
    lambda g: g.pop("dependencies"),
])
def test_malformed_graph_is_an_error(change):
    with pytest.raises(affected.GraphError):
        affected.parse_graph(graph_with(change), ROOT)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    top = tmp_path / "checkout"
    workspace = top / ROOT
    write(workspace / "package.json", json.dumps({"name": "w", "private": True, "workspaces": ["libs/*", "apps/*"]}))
    write(workspace / "nx.json", "{}\n")
    for name in ("libs/shared", "libs/alpha", "libs/beta", "apps/gamma", "libs/solo"):
        write(workspace / name / "package.json", json.dumps({"name": name, "scripts": {"test": "jest"}}))
        write(workspace / name / "index.js", f"exports.name = () => '{name}';\n")
    write(top / "graph.json", json.dumps(GRAPH))
    git(top, "init", "-q")
    git(top, "add", "-A")
    git(top, "commit", "-q", "-m", "base")
    return top


def commit(top, relative, text):
    write(top / relative, text)
    git(top, "add", "-A")
    git(top, "commit", "-q", "-m", "change")
    return git(top, "rev-parse", "HEAD")


def run(monkeypatch, capsys, top, *argv):
    monkeypatch.chdir(top)
    code = affected.main(["--root", ROOT, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_selects_from_the_git_diff(monkeypatch, capsys, repo):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, f"{ALPHA}/index.js", "exports.name = () => 'alpha-v2';\n")
    code, out, _ = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 0 and set(json.loads(out)["test"]) == {ALPHA, GAMMA}
    code, out, _ = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"),
                       "--names")
    assert set(json.loads(out)["test"]) == {"@sim/alpha", "@sim/gamma"}


def test_cli_refuses_a_checkout_that_is_not_head(monkeypatch, capsys, repo):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, f"{ALPHA}/index.js", "exports.name = () => 'alpha-v2';\n")
    git(repo, "checkout", "-q", base)
    code, _, err = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 2 and "not" in err


def test_cli_refuses_a_dirty_workspace(monkeypatch, capsys, repo):
    head = git(repo, "rev-parse", "HEAD")
    write(repo / ROOT / "libs" / "extra" / "package.json", "{}")
    code, _, err = run(monkeypatch, capsys, repo, "--base", head, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 2 and "untracked" in err


def test_cli_refuses_a_workspace_with_a_test_script_missing_from_the_graph(monkeypatch, capsys, repo):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, f"{ROOT}/libs/hidden/package.json", json.dumps({"name": "hidden", "scripts": {"test": "jest"}}))
    code, _, err = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 2 and "libs/hidden" in err


def test_cli_refuses_a_negated_workspace_pattern(monkeypatch, capsys, repo):
    base = git(repo, "rev-parse", "HEAD")
    manifest = {"name": "w", "workspaces": ["libs/*", "apps/*", "!libs/solo"]}
    head = commit(repo, f"{ROOT}/package.json", json.dumps(manifest))
    code, _, err = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 2 and "not supported" in err


def test_cli_turns_an_unreadable_graph_into_an_error(monkeypatch, capsys, repo):
    head = git(repo, "rev-parse", "HEAD")
    code, _, err = run(monkeypatch, capsys, repo, "--base", head, "--head", head, "--graph", str(repo / "missing.json"))
    assert code == 2 and "cannot read" in err


def test_cli_turns_a_failing_nx_into_an_error(monkeypatch, capsys, repo):
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("SIM_NX", sys.executable)
    code, _, err = run(monkeypatch, capsys, repo, "--base", head, "--head", head)
    assert code == 2 and "nx graph failed" in err


def test_cli_refuses_an_option_shaped_ref(monkeypatch, capsys, repo):
    code, _, err = run(monkeypatch, capsys, repo, "--base=--output=x", "--head", "HEAD")
    assert code == 2 and "must not start" in err


def test_cli_never_prints_an_empty_test_list(monkeypatch, capsys, repo):
    head = git(repo, "rev-parse", "HEAD")
    code, out, _ = run(monkeypatch, capsys, repo, "--base", head, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 0 and set(json.loads(out)["test"]) == ALL_TESTS


def select_after_alpha_change(monkeypatch, capsys, repo, relative, source):
    commit(repo, relative, source)
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, f"{ALPHA}/index.js", "exports.name = () => 'alpha-v2';\n")
    code, out, err = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 0, err
    return json.loads(out)


@pytest.mark.parametrize("source", [
    'const a = require("../alpha/index");\n',
    "const a = require('../alpha');\n",
    'const a = require("@sim/alpha");\n',
    'const a = require("@sim/alpha/index.js");\n',
    'import { name } from "../alpha/index.js";\n',
    'import "../alpha/index.js";\n',
    'export * from "../alpha/index.js";\n',
    'const a = import("../alpha/index.js");\n',
    'const a = require.resolve("../alpha/index");\n',
    'jest.mock("../alpha/index");\n',
    'const a = jest.requireActual("@sim/alpha");\n',
    "const a = require(`../alpha/index`);\n",
])
def test_an_undeclared_reference_to_a_changed_project_selects_all(monkeypatch, capsys, repo, source):
    result = select_after_alpha_change(monkeypatch, capsys, repo, f"{SOLO}/index.js", source)
    assert set(result["test"]) == ALL_TESTS and result["all"] is True
    assert f"{SOLO}/index.js" in result["reason"]


@pytest.mark.parametrize("source", [
    "const a = require(name);\n",
    "const a = require(`../${name}/index`);\n",
    "const a = import(name);\n",
    'const { createRequire } = require("node:module");\nconst load = createRequire(__filename);\n',
])
def test_a_reference_the_scan_cannot_read_selects_all(monkeypatch, capsys, repo, source):
    result = select_after_alpha_change(monkeypatch, capsys, repo, f"{SOLO}/index.js", source)
    assert set(result["test"]) == ALL_TESTS and "not a string literal" in result["reason"]


@pytest.mark.parametrize("relative, source", [
    (f"{SOLO}/index.js", 'const t = require("../../../tools/helper");\n'),
    (f"{SOLO}/index.js", 'const t = require("/opt/helper");\n'),
    (f"{ROOT}/oracle.js", 'const a = require("@sim/solo");\n'),
    (f"{ROOT}/oracle.js", 'const a = require("./libs/solo/index");\n'),
    (f"{SOLO}/jest.config.json", '{"moduleNameMapper": {"^shared$": "<rootDir>/../alpha/index.js"}}\n'),
    (f"{SOLO}/tsconfig.json", '{"compilerOptions": {"paths": {"a": ["../alpha/index.ts"]}}}\n'),
    (f"{SOLO}/jest.config.json", '{"roots": ["../../../tools"]}\n'),
])
def test_a_reference_outside_the_graph_selects_all(monkeypatch, capsys, repo, relative, source):
    result = select_after_alpha_change(monkeypatch, capsys, repo, relative, source)
    assert set(result["test"]) == ALL_TESTS and relative in result["reason"]


@pytest.mark.parametrize("relative, source", [
    (f"{GAMMA}/index.js", 'const a = require("@sim/alpha");\nconst s = require("../../libs/shared/index");\n'),
    (f"{GAMMA}/index.test.js", 'const o = require("../../oracle");\nconst g = require("./index");\n'),
    (f"{SOLO}/index.js", 'const fs = require("node:fs");\nconst j = require("jest");\nimport path from "path";\n'),
    (f"{SOLO}/README.md", 'require("../alpha")\n'),
    (f"{ROOT}/oracle.js", 'const fs = require("node:fs");\n'),
    (f"{GAMMA}/jest.config.json", '{"roots": ["<rootDir>", "../../libs/alpha"], "testMatch": ["./**/*.test.js"]}\n'),
    (f"{ROOT}/nx.json", '{"$schema": "./node_modules/nx/schemas/nx-schema.json"}\n'),
])
def test_a_declared_or_non_project_reference_keeps_the_selection(monkeypatch, capsys, repo, relative, source):
    result = select_after_alpha_change(monkeypatch, capsys, repo, relative, source)
    assert set(result["test"]) == {ALPHA, GAMMA} and result["all"] is False


def test_a_tracked_symlink_selects_all(monkeypatch, capsys, repo):
    commit(repo, f"{SOLO}/index.js", "exports.name = () => 'solo';\n")
    blob = subprocess.run([*GIT, "hash-object", "-w", "--stdin"], cwd=repo, input="../alpha", text=True,
                          check=True, capture_output=True).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{SOLO}/linked")
    git(repo, "commit", "-q", "-m", "link")
    git(repo, "reset", "-q", "--hard")
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, f"{ALPHA}/index.js", "exports.name = () => 'alpha-v2';\n")
    code, out, err = run(monkeypatch, capsys, repo, "--base", base, "--head", head, "--graph", str(repo / "graph.json"))
    assert code == 0, err
    result = json.loads(out)
    assert set(result["test"]) == ALL_TESTS and f"{SOLO}/linked" in result["reason"]


def test_the_shipped_sim_has_no_undeclared_reference_and_the_scan_reads_it():
    top = Path(__file__).resolve().parents[3]
    graph = affected.parse_graph(GRAPH, ROOT)
    references = affected.references(top, ROOT)
    assert (f"{GAMMA}/index.js", "@sim/alpha") in references
    assert (f"{GAMMA}/index.test.js", "../../oracle") in references
    assert affected.undeclared_reference(graph, top, ROOT) is None


def test_sim_workspace_declares_every_dependency_it_imports():
    workspace = Path(__file__).resolve().parents[3] / ROOT
    declared = {}
    for manifest in [*workspace.glob("libs/*/package.json"), *workspace.glob("apps/*/package.json")]:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        declared[manifest.parent] = set(data.get("dependencies", {}))
    assert len(declared) == 5
    for directory, names in declared.items():
        source = (directory / "index.js").read_text(encoding="utf-8")
        imported = {line.split('"')[1] for line in source.splitlines() if 'require("@sim/' in line}
        assert imported == names, directory.name


@pytest.mark.parametrize("bin_field, expected", [
    ({"nx": "./dist/bin/nx.js", "nx-cloud": "./dist/bin/cloud.js"}, "dist/bin/nx.js"),
    ("./bin/nx.js", "bin/nx.js"),
])
def test_nx_entry_follows_the_package_bin_field(tmp_path, bin_field, expected):
    package = tmp_path / "node_modules" / "nx"
    write(package / "package.json", json.dumps({"name": "nx", "bin": bin_field}))
    write(package / expected, "")
    assert affected.nx_entry(tmp_path) == (package / expected).resolve()


@pytest.mark.parametrize("manifest", [None, {"name": "nx"}, {"bin": {"nx": "../../evil.js"}}, {"bin": "./missing.js"}])
def test_nx_entry_refuses_a_missing_or_escaping_executable(tmp_path, manifest):
    write(tmp_path / "evil.js", "")
    if manifest is not None:
        write(tmp_path / "node_modules" / "nx" / "package.json", json.dumps(manifest))
    with pytest.raises(affected.GraphError):
        affected.nx_entry(tmp_path)


def test_sim_workspace_never_runs_install_scripts():
    npmrc = (Path(__file__).resolve().parents[3] / ROOT / ".npmrc").read_text(encoding="utf-8")
    assert "ignore-scripts=true" in [line.strip() for line in npmrc.splitlines()]


def test_sim_workspace_never_replays_a_cached_test_result():
    nx = json.loads((Path(__file__).resolve().parents[3] / ROOT / "nx.json").read_text(encoding="utf-8"))
    assert nx["targetDefaults"]["test"]["cache"] is False
