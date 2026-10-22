import json

import pytest

from adapters.workspace import affected, plan

ROOT = "sim/workspace"
SHARED, ALPHA, BETA, GAMMA, SOLO = (f"{ROOT}/{p}" for p in ("libs/shared", "libs/alpha", "libs/beta", "apps/gamma", "libs/solo"))


def node(root):
    return {"data": {"root": root, "targets": {"test": {}}}}


GRAPH = {"graph": {
    "nodes": {"shared": node("libs/shared"), "alpha": node("libs/alpha"), "beta": node("libs/beta"),
              "gamma": node("apps/gamma"), "solo": node("libs/solo")},
    "dependencies": {"alpha": [{"target": "shared"}], "beta": [{"target": "shared"}],
                     "gamma": [{"target": "alpha"}, {"target": "beta"}]},
}}


def selection(changed):
    return affected.select(affected.parse_graph(GRAPH, ROOT), changed, ROOT)


def run(tmp_path, capsys, document, *argv):
    path = tmp_path / "affected.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    code = plan.main(["--affected", str(path), "--lane", "unit", *argv])
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


def test_test_list_is_the_affected_test_projects(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, selection([f"{ALPHA}/index.js"]), "--emit", "test")
    assert code == 0 and set(out) == {ALPHA, GAMMA}


def test_build_list_adds_unaffected_dependencies_in_dependency_order(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, selection([f"{ALPHA}/index.js"]), "--emit", "build")
    assert code == 0 and set(out) == {SHARED, ALPHA, BETA, GAMMA}
    assert out.index(SHARED) < out.index(ALPHA) < out.index(GAMMA) and out.index(BETA) < out.index(GAMMA)


def test_names_prints_nx_project_names(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, selection([f"{ALPHA}/index.js"]), "--emit", "test", "--names")
    assert code == 0 and set(out) == {"alpha", "gamma"}


def test_fallback_selection_plans_every_test_project(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, selection([f"{ROOT}/nx.json"]), "--emit", "test")
    assert code == 0 and set(out) == {SHARED, ALPHA, BETA, GAMMA, SOLO}


@pytest.mark.parametrize("break_it, message", [
    (lambda d: d.update(test=[]), "zero tests"),
    (lambda d: d.update(test=["sim/workspace/libs/ghost"]), "is not a project in the selection"),
    (lambda d: d["projects"][ALPHA].update(test=False), "has no test target"),
    (lambda d: d["projects"][GAMMA].update(dependencies=["sim/workspace/libs/ghost"]), "which is not in projects"),
    (lambda d: d.pop("projects"), "KeyError"),
    (lambda d: d.update(projects=[]), "projects is not an object"),
])
def test_unusable_selection_is_an_error_never_an_empty_plan(tmp_path, capsys, break_it, message):
    document = selection([f"{ALPHA}/index.js"])
    break_it(document)
    code, out, err = run(tmp_path, capsys, document, "--emit", "test")
    assert code == 2 and out == [] and message in err


def test_unknown_lane_is_refused(tmp_path, capsys):
    path = tmp_path / "affected.json"
    path.write_text(json.dumps(selection([])), encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        plan.main(["--affected", str(path), "--lane", "integration", "--emit", "test"])
    assert raised.value.code == 2
