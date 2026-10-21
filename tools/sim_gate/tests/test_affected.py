import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.sim_gate import affected

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT = "sim/dotnet"

SERVICE_EDGES = {
    "Api": {"Service"},
    "Api.IntegrationTests": {"Api", "TestSupport"},
    "Core": set(),
    "Core.Tests": {"Core", "TestSupport"},
    "Data": {"Core"},
    "Other": set(),
    "Other.Tests": {"Other", "TestSupport"},
    "Service": {"Core", "Data", "Utils"},
    "Service.Tests": {"Service", "TestSupport"},
    "TestSupport": set(),
    "Utils": set(),
}
CYCLE = {"A": "C", "B": "A", "C": "B", "D": ""}
TEST_PROJECTS = {"Api.IntegrationTests", "Core.Tests", "Other.Tests", "Service.Tests"}
FULL_ORDER = [
    "Core", "Data", "Other", "Utils", "Service", "Api", "TestSupport",
    "Api.IntegrationTests", "Core.Tests", "Other.Tests", "Service.Tests",
]


def load(directory="service", prefix=ROOT):
    if directory == "cycle":
        return {f"{prefix}/src/{n}/{n}.csproj": csproj(reference(f"../{r}/{r}.csproj") if r else "") for n, r in CYCLE.items()}
    base = REPO_ROOT / ROOT
    found = [p for p in sorted(base.rglob("*.csproj")) if not {"bin", "obj"} & set(p.relative_to(base).parts)]
    return {f"{prefix}/{p.relative_to(base).as_posix()}": p.read_bytes() for p in found}


def service_graph():
    return affected.build_graph(load("service"))


def project_path(name):
    kind = "tests" if name in TEST_PROJECTS or name == "TestSupport" else "src"
    return f"{ROOT}/{kind}/{name}/{name}.csproj"


def names(paths):
    return [Path(p).stem for p in paths]


def pick(changed, directory="service"):
    graph = affected.build_graph(load(directory))
    return affected.select(graph, changed, ROOT)


def edges(graph):
    return {p.name: {graph[r].name for r in p.references} for p in graph.values()}


def csproj(body=""):
    return f'<Project Sdk="Microsoft.NET.Sdk">{body}</Project>'


def reference(include):
    return f'<ItemGroup><ProjectReference Include="{include}" /></ItemGroup>'


def test_solution_graph_has_the_expected_edges():
    assert edges(service_graph()) == SERVICE_EDGES


def test_test_projects_are_detected_by_property_or_by_the_test_sdk_package():
    graph = service_graph()
    assert {p.name for p in graph.values() if p.is_test} == TEST_PROJECTS
    by_package = affected.parse_project("a/T.csproj", csproj('<ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" /></ItemGroup>'))
    by_property = affected.parse_project("a/T.csproj", csproj("<PropertyGroup><IsTestProject>true</IsTestProject></PropertyGroup>"))
    neither = affected.parse_project("a/T.csproj", csproj("<PropertyGroup><IsTestProject>false</IsTestProject></PropertyGroup>"))
    assert (by_package.is_test, by_property.is_test, neither.is_test) == (True, True, False)


def test_build_order_is_dependencies_first_and_deterministic():
    order = affected.topological_order(service_graph())
    assert names(order) == FULL_ORDER
    position = {p: i for i, p in enumerate(order)}
    for path, project in service_graph().items():
        assert all(position[r] < position[path] for r in project.references)


def test_backslash_and_parent_segments_resolve_to_the_referenced_project():
    project = affected.parse_project("r/tests/T/T.csproj", csproj(reference("..\\..\\src\\C\\C.csproj")))
    assert project.references == ("r/src/C/C.csproj",)


def test_legacy_namespace_project_file_is_parsed():
    xml = '<?xml version="1.0" encoding="utf-8"?><Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">' + reference("..\\B\\B.csproj") + "</Project>"
    project = affected.parse_project("r/A/A.csproj", xml.encode("utf-8"))
    assert project.references == ("r/B/B.csproj",)


def test_reference_to_a_project_outside_the_graph_is_refused():
    files = {"r/A/A.csproj": csproj(reference("..\\Missing\\Missing.csproj"))}
    with pytest.raises(affected.GraphError, match="Missing.csproj"):
        affected.build_graph(files)


def test_unresolvable_msbuild_expression_in_a_reference_is_refused():
    with pytest.raises(affected.GraphError, match="MSBuild expression"):
        affected.parse_project("r/A/A.csproj", csproj(reference("$(SolutionDir)B\\B.csproj")))


def test_malformed_project_file_is_refused_with_its_path():
    with pytest.raises(affected.GraphError, match="r/A/A.csproj"):
        affected.parse_project("r/A/A.csproj", "<Project>")


def test_two_projects_with_one_name_are_refused():
    files = {"r/x/A/A.csproj": csproj(), "r/y/A/A.csproj": csproj()}
    with pytest.raises(affected.GraphError, match="two projects are named A"):
        affected.build_graph(files)


def test_cycle_is_refused_and_the_message_names_the_cycle():
    graph = affected.build_graph(load("cycle"))
    with pytest.raises(affected.CycleError, match=r"cycle: A -> C -> B -> A$"):
        affected.topological_order(graph)


def test_cycle_is_refused_even_when_nothing_in_it_changed():
    graph = affected.build_graph(load("cycle"))
    with pytest.raises(affected.CycleError):
        affected.select(graph, [f"{ROOT}/src/D/D.cs"], ROOT)


def test_a_project_referencing_itself_is_a_cycle():
    graph = affected.build_graph({"r/A/A.csproj": csproj(reference("A.csproj"))})
    with pytest.raises(affected.CycleError, match="A -> A"):
        affected.topological_order(graph)


def test_core_change_reaches_every_transitive_dependent_and_no_one_else():
    result = pick([f"{ROOT}/src/Core/Stamp.cs"])
    assert names(result["build"]) == ["Core", "Data", "Service", "Api", "Api.IntegrationTests", "Core.Tests", "Service.Tests"]
    assert names(result["test"]) == ["Api.IntegrationTests", "Core.Tests", "Service.Tests"]


def test_utils_change_skips_data_and_core_but_reaches_api():
    result = pick([f"{ROOT}/src/Utils/Stamp.cs"])
    assert names(result["build"]) == ["Utils", "Service", "Api", "Api.IntegrationTests", "Service.Tests"]


def test_other_change_is_isolated_from_the_service_chain():
    result = pick([f"{ROOT}/src/Other/Stamp.cs"])
    assert names(result["build"]) == ["Other", "Other.Tests"]
    assert names(result["test"]) == ["Other.Tests"]


def test_change_in_a_leaf_selects_only_the_leaf_and_its_test():
    result = pick([f"{ROOT}/src/Api/Endpoint.cs"])
    assert names(result["build"]) == ["Api", "Api.IntegrationTests"]


def test_test_project_change_selects_only_that_test_project():
    result = pick([f"{ROOT}/tests/Core.Tests/StampTests.cs"])
    assert names(result["build"]) == ["Core.Tests"]
    assert names(result["test"]) == ["Core.Tests"]


def test_shared_test_support_change_selects_every_test_project():
    result = pick([f"{ROOT}/tests/TestSupport/Oracle.cs"])
    assert names(result["build"]) == ["TestSupport", "Api.IntegrationTests", "Core.Tests", "Other.Tests", "Service.Tests"]
    assert set(names(result["test"])) == TEST_PROJECTS


def test_test_list_holds_only_test_projects():
    result = pick([f"{ROOT}/src/Core/Stamp.cs", f"{ROOT}/src/Other/Stamp.cs"])
    assert set(result["test"]) <= {project_path(n) for n in TEST_PROJECTS}
    assert all(p in result["build"] for p in result["test"])


def test_two_changes_are_a_union_in_dependency_order():
    result = pick([f"{ROOT}/src/Other/Stamp.cs", f"{ROOT}/src/Utils/Stamp.cs"])
    assert names(result["build"]) == ["Other", "Utils", "Service", "Api", "Api.IntegrationTests", "Other.Tests", "Service.Tests"]


def test_a_sibling_directory_with_a_shared_name_prefix_is_not_the_project():
    files = {"r/src/Core/Core.csproj": csproj(), "r/src/Core.Extra/Core.Extra.csproj": csproj()}
    graph = affected.build_graph(files)
    result = affected.select(graph, ["r/src/Core.Extra/X.cs"], "r")
    assert names(result["build"]) == ["Core.Extra"]


def test_project_file_change_counts_as_a_change_of_that_project():
    result = pick([f"{ROOT}/src/Data/Data.csproj"])
    assert names(result["build"]) == ["Data", "Service", "Api", "Api.IntegrationTests", "Service.Tests"]


@pytest.mark.parametrize("file", [
    "Directory.Build.props",
    "Directory.Build.targets",
    "Directory.Packages.props",
    "global.json",
    "Sim.sln",
    "Sim.slnx",
    "nuget.config",
    "NuGet.Config",
    "src/Directory.Build.props",
    "src/Core/Directory.Build.props",
    "build/common.props",
    "build/Shared.cs",
])
def test_a_build_wide_file_marks_every_project_affected(file):
    result = pick([f"{ROOT}/{file}"])
    assert names(result["build"]) == FULL_ORDER
    assert set(names(result["test"])) == TEST_PROJECTS


def test_deleted_project_directory_marks_everything_affected():
    graph = service_graph()
    result = affected.select(graph, [f"{ROOT}/src/Gone/Stamp.cs"], ROOT)
    assert names(result["build"]) == FULL_ORDER


@pytest.mark.parametrize("file", ["README.md", "docs/guide.md", "tools/other.py", "sim/other/Directory.Build.props", "global.json"])
def test_a_file_outside_the_root_selects_nothing(file):
    assert pick([file]) == {"build": [], "test": []}


@pytest.mark.parametrize("file", ["README.md", ".gitignore", "notes.txt"])
def test_an_inert_file_inside_the_root_selects_nothing(file):
    assert pick([f"{ROOT}/{file}"]) == {"build": [], "test": []}


def test_no_change_selects_nothing():
    assert pick([]) == {"build": [], "test": []}


GIT_ENV = {
    k: v for k, v in os.environ.items() if not k.startswith("GIT_")
} | {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(repo, *args):
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, text=True, env=GIT_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def write(repo, relative, content):
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    for path, content in load("service").items():
        write(tmp_path, path, content)
    for name in ("Core", "Utils", "Other"):
        write(tmp_path, f"{ROOT}/src/{name}/Stamp.cs", f"// {name}\n")
    write(tmp_path, "README.md", "x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def run_cli(*extra, capsys, base="HEAD~1", head="HEAD"):
    code = affected.main(["--base", base, "--head", head, *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_prints_the_contract_json_for_a_real_diff(repo, capsys):
    write(repo, f"{ROOT}/src/Core/Stamp.cs", "// Core changed\n")
    commit(repo)
    code, out, _ = run_cli(capsys=capsys)
    assert code == 0
    parsed = json.loads(out)
    assert list(parsed) == ["build", "test"]
    assert names(parsed["build"]) == ["Core", "Data", "Service", "Api", "Api.IntegrationTests", "Core.Tests", "Service.Tests"]
    assert parsed["build"][0] == f"{ROOT}/src/Core/Core.csproj"
    assert names(parsed["test"]) == ["Api.IntegrationTests", "Core.Tests", "Service.Tests"]


def test_cli_names_flag_prints_project_names(repo, capsys):
    write(repo, f"{ROOT}/src/Other/Stamp.cs", "// Other changed\n")
    commit(repo)
    code, out, _ = run_cli("--names", capsys=capsys)
    assert code == 0
    assert json.loads(out) == {"build": ["Other", "Other.Tests"], "test": ["Other.Tests"]}


def test_a_file_moved_between_projects_selects_both_projects(repo, capsys):
    git(repo, "mv", f"{ROOT}/src/Utils/Stamp.cs", f"{ROOT}/src/Other/Stamp.cs.moved")
    commit(repo)
    code, out, _ = run_cli("--names", capsys=capsys)
    assert code == 0
    assert {"Utils", "Other"} <= set(json.loads(out)["build"])


def test_the_graph_is_read_at_head_not_from_the_working_tree(repo, capsys):
    write(repo, f"{ROOT}/src/Core/Stamp.cs", "// Core changed\n")
    commit(repo)
    write(repo, f"{ROOT}/src/Data/Data.csproj", csproj())
    code, out, _ = run_cli("--names", capsys=capsys)
    assert code == 0
    assert "Data" in json.loads(out)["build"]


def test_cli_refuses_a_cycle_with_exit_2_and_a_message(repo, capsys):
    write(repo, f"{ROOT}/src/Core/Core.csproj", csproj(reference("..\\Data\\Data.csproj")))
    commit(repo)
    code, out, err = run_cli(capsys=capsys)
    assert code == 2
    assert out == ""
    assert "project reference cycle: Core -> Data -> Core" in err


def test_cli_refuses_a_root_without_projects(repo, capsys):
    code, out, err = run_cli("--root", "sim/missing", capsys=capsys)
    assert code == 2
    assert out == ""
    assert "no .csproj found under sim/missing" in err


def test_cli_refuses_an_unknown_ref(repo, capsys):
    code, out, err = run_cli(capsys=capsys, base="no-such-ref")
    assert code == 2
    assert out == ""
    assert "git diff failed" in err


@pytest.mark.parametrize("flag", ["--base", "--head"])
def test_cli_refuses_a_ref_that_looks_like_a_git_option(repo, capsys, flag):
    other = "HEAD~1" if flag == "--head" else "HEAD"
    code = affected.main([f"{flag}=--output=leaked", f"--{'base' if flag == '--head' else 'head'}", other])
    out = capsys.readouterr()
    assert code == 2
    assert out.out == ""
    assert "must not start with '-'" in out.err
    assert not [p for p in repo.iterdir() if p.name.startswith("leaked")]
