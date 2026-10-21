from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import workflow_policy as policy

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
LANE = "sim-gate-lane.yml"
ATTEMPT = "sim-gate-attempt.yml"
TOP = "sim-gate.yml"
SHA = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"


def test_the_shipped_workflows_satisfy_the_policy():
    assert policy.violations(WORKFLOWS) == []


MUTATIONS = {
    "token on a step of a lab-label job": (
        ATTEMPT,
        "      - name: Health\n        run: |",
        "      - name: Health\n        env:\n          SIM_ROUTE_TOKEN: ${{ secrets.SIM_ROUTE_TOKEN }}\n        run: |",
        "referenced outside the route job",
    ),
    "token passed to the called attempt workflow": (
        LANE,
        "      tag: ${{ inputs.lane }}-attempt\n",
        "      tag: ${{ inputs.lane }}-attempt\n      token: ${{ secrets.SIM_ROUTE_TOKEN }}\n",
        "referenced outside the route job",
    ),
    "token in a second step of the route job": (
        LANE,
        "      - name: Checkout\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n      - name: Set up Python",
        "      - name: Checkout\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        env:\n          SIM_ROUTE_TOKEN: ${{ secrets.SIM_ROUTE_TOKEN }}\n        with:\n          persist-credentials: false\n      - name: Set up Python",
        "exactly one step",
    ),
    "token in the route job env instead of a step": (
        LANE,
        "    timeout-minutes: 5\n    environment: sim-route\n",
        "    timeout-minutes: 5\n    environment: sim-route\n    env:\n      SIM_ROUTE_TOKEN: ${{ secrets.SIM_ROUTE_TOKEN }}\n",
        "outside a step env",
    ),
    "token echoed inside a run script": (
        LANE,
        'echo "target=${target}" >> "$GITHUB_OUTPUT"',
        'echo "${{ secrets.SIM_ROUTE_TOKEN }}" >> "$GITHUB_OUTPUT"',
        "expression inside run",
    ),
    "route job on the lab label": (
        LANE,
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment: sim-route",
        "    runs-on: ${{ inputs.lab_label }}\n    timeout-minutes: 5\n    environment: sim-route",
        "route job must run on a GitHub-hosted",
    ),
    "route job on a self-hosted label": (
        LANE,
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment: sim-route",
        "    runs-on: self-hosted\n    timeout-minutes: 5\n    environment: sim-route",
        "route job must run on a GitHub-hosted",
    ),
    "route job without its environment": (
        LANE,
        "    environment: sim-route\n",
        "",
        "environment sim-route",
    ),
    "route job without the fork guard": (
        LANE,
        "    if: ${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}\n    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment",
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment",
        "no fork guard",
    ),
    "fork guard that lets a fork through": (
        LANE,
        "github.event.pull_request.head.repo.full_name == github.repository }}",
        "github.event.pull_request.head.repo.full_name != '' }}",
        "runs for a fork pull request",
    ),
    "fork guard that blocks same-repository runs": (
        LANE,
        "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository",
        "github.event_name == 'never'",
        "skipped for a same-repository run",
    ),
    "attempt always on the lab label": (
        LANE,
        "runner: ${{ needs.route.outputs.target == 'onprem' && inputs.lab_label || 'ubuntu-24.04' }}",
        "runner: ${{ inputs.lab_label }}",
        "attempt runner for target",
    ),
    "attempt on the lab label when routing was skipped": (
        LANE,
        "runner: ${{ needs.route.outputs.target == 'onprem' && inputs.lab_label || 'ubuntu-24.04' }}",
        "runner: ${{ needs.route.outputs.target != 'cloud' && inputs.lab_label || 'ubuntu-24.04' }}",
        "attempt runner for target ''",
    ),
    "retry also on a test verdict": (
        LANE,
        "needs.attempt.outputs.verdict == 'infra'",
        "needs.attempt.outputs.verdict != 'pass'",
        "retry runs=True for verdict 'test'",
    ),
    "retry without any verdict condition": (
        LANE,
        "needs.attempt.outputs.verdict == 'infra' && ",
        "",
        "retry runs=True for verdict 'test'",
    ),
    "retry for any verdict through an or": (
        LANE,
        "needs.route.outputs.target == 'onprem' }}\n    permissions:\n      contents: read\n      actions: read\n      checks: read\n    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-retry",
        "needs.route.outputs.target == 'onprem' || needs.attempt.outputs.verdict == 'test' }}\n    permissions:\n      contents: read\n      actions: read\n      checks: read\n    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-retry",
        "retry runs=True for verdict 'test'",
    ),
    "retry without the on-prem condition": (
        LANE,
        " && needs.route.outputs.target == 'onprem' }}\n    permissions",
        " }}\n    permissions",
        "retry runs=True for verdict 'infra' on target 'cloud'",
    ),
    "retry on the lab label": (
        LANE,
        "      runner: ubuntu-24.04\n      fail_test: ${{ inputs.fail_test }}\n      kill_runner: ${{ inputs.simulate_kill == 'both' }}",
        "      runner: ${{ inputs.lab_label }}\n      fail_test: ${{ inputs.fail_test }}\n      kill_runner: ${{ inputs.simulate_kill == 'both' }}",
        "retry must run on a hosted label",
    ),
    "expression inside a run script": (
        ATTEMPT,
        "        run: sh tools/sim_gate/prejob.sh",
        "        run: echo ${{ inputs.tag }}",
        "expression inside run",
    ),
    "action pinned to a tag": (
        TOP,
        SHA + " # v7.0.1",
        "actions/checkout@v7",
        "not pinned to a full SHA",
    ),
    "action pinned to a short sha": (
        ATTEMPT,
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
        "actions/upload-artifact@cf430e0",
        "not pinned to a full SHA",
    ),
    "attempt timeout beyond the cap": (
        ATTEMPT,
        "    runs-on: ${{ inputs.runner }}\n    timeout-minutes: 25",
        "    runs-on: ${{ inputs.runner }}\n    timeout-minutes: 45",
        "per-attempt cap",
    ),
    "attempt plus retry beyond the budget": (
        ATTEMPT,
        "    runs-on: ${{ inputs.runner }}\n    timeout-minutes: 25",
        "    runs-on: ${{ inputs.runner }}\n    timeout-minutes: 30",
        "critical path",
    ),
    "pull_request_target trigger": (
        TOP,
        "on:\n  workflow_dispatch:",
        "on:\n  pull_request_target:\n  workflow_dispatch:",
        "forbidden trigger pull_request_target",
    ),
    "workflow_run trigger": (
        TOP,
        "on:\n  workflow_dispatch:",
        "on:\n  workflow_run:\n  workflow_dispatch:",
        "forbidden trigger workflow_run",
    ),
    "issue_comment trigger": (
        TOP,
        "on:\n  workflow_dispatch:",
        "on:\n  issue_comment:\n  workflow_dispatch:",
        "forbidden trigger issue_comment",
    ),
    "pull_request trigger on the entry workflow": (
        TOP,
        "on:\n  workflow_dispatch:",
        "on:\n  pull_request:\n  workflow_dispatch:",
        "workflow_dispatch only",
    ),
    "continue-on-error": (
        ATTEMPT,
        "    timeout-minutes: 25\n",
        "    timeout-minutes: 25\n    continue-on-error: true\n",
        "continue-on-error",
    ),
    "secrets inherit": (
        LANE,
        "    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-attempt",
        "    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    secrets: inherit\n    with:\n      tag: ${{ inputs.lane }}-attempt",
        "secrets: inherit",
    ),
    "write permission": (
        ATTEMPT,
        "    timeout-minutes: 5\n    permissions:\n      contents: read\n      actions: read\n      checks: read\n",
        "    timeout-minutes: 5\n    permissions:\n      contents: write\n      actions: read\n      checks: read\n",
        "permission jobs.classify.permissions.contents is write",
    ),
    "workflow permissions widened": (
        TOP,
        "permissions:\n  contents: read\n\njobs:",
        "permissions:\n  contents: read\n  packages: read\n\njobs:",
        "workflow permissions must be contents: read only",
    ),
    "secret-looking name read from vars": (
        LANE,
        "          LAB_LABEL: ${{ inputs.lab_label }}",
        "          LAB_LABEL: ${{ vars.SIM_ROUTE_TOKEN }}",
        "read from vars",
    ),
    "dotnet job without setup-dotnet": (
        ATTEMPT,
        "      - name: Set up .NET\n        uses: actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0\n        with:\n          global-json-file: sim/dotnet/global.json\n",
        "",
        "runs dotnet without a prior actions/setup-dotnet step",
    ),
    "setup-dotnet after the first dotnet command": (
        ATTEMPT,
        "      - name: Set up .NET\n        uses: actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0\n        with:\n          global-json-file: sim/dotnet/global.json\n      - name: Health\n",
        "      - name: Health\n",
        "runs dotnet without a prior actions/setup-dotnet step",
    ),
    "setup-dotnet with a floating version instead of global.json": (
        ATTEMPT,
        "          global-json-file: sim/dotnet/global.json",
        "          dotnet-version: 10.0.x",
        "setup-dotnet must use global-json-file",
    ),
    "environment on the attempt job": (
        ATTEMPT,
        "    runs-on: ${{ inputs.runner }}\n    timeout-minutes: 25",
        "    runs-on: ${{ inputs.runner }}\n    environment: sim-cache\n    timeout-minutes: 25",
        "must not use an environment",
    ),
}


@pytest.fixture
def copy(tmp_path):
    target = tmp_path / "workflows"
    target.mkdir()
    for path in WORKFLOWS.glob("sim-gate*.yml"):
        shutil.copy(path, target / path.name)
    return target


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_mutated_copy_is_refused(copy, name):
    filename, old, new, expected = MUTATIONS[name]
    path = copy / filename
    text = path.read_text(encoding="utf-8")
    assert old in text, "the mutation no longer matches the workflow"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    found = policy.violations(copy)
    assert any(expected in line for line in found), found


def test_a_missing_workflow_is_refused(copy):
    (copy / ATTEMPT).unlink()
    assert any("workflow set is" in line for line in policy.violations(copy))


def test_parser_reads_nested_maps_lists_block_scalars_and_flow_lists():
    document = policy.parse(
        "a:\n  b: one # trailing\n  c:\n    - x: 1\n      y: '2'\n    - plain\n  d: [p, q]\n  e: |\n    line1\n      line2\n"
        "f: ${{ a == 'b' }}\n"
    )
    assert document == {
        "a": {"b": "one", "c": [{"x": "1", "y": "2"}, "plain"], "d": ["p", "q"], "e": "line1\n  line2\n"},
        "f": "${{ a == 'b' }}",
    }


@pytest.mark.parametrize("text", ["a: {b: 1}", "a: &x 1", "a:\n\tb: 1", "a: 1\na: 2", "a: [1,\n  2]"])
def test_parser_refuses_what_it_cannot_read_instead_of_guessing(text):
    with pytest.raises(policy.Unsupported):
        policy.parse(text)


@pytest.mark.parametrize(
    "expression, context, expected",
    [
        ("${{ a == 'x' && b || 'fallback' }}", {"a": "x", "b": "lab"}, "lab"),
        ("${{ a == 'x' && b || 'fallback' }}", {"a": "y", "b": "lab"}, "fallback"),
        ("${{ a == 'x' && b || 'fallback' }}", {"a": "x", "b": ""}, "fallback"),
        ("${{ a == 'x' && b || 'fallback' }}", {}, "fallback"),
        ("${{ a != 'x' }}", {"a": "y"}, True),
        ("${{ a == 'X' }}", {"a": "x"}, True),
        ("${{ a == '' }}", {}, True),
        ("${{ !cancelled() && a == 'x' }}", {"a": "x"}, True),
        ("${{ x || y == 'a' && z == 'b' }}", {"x": "", "y": "a", "z": "b"}, True),
        ("${{ x || y == 'a' && z == 'b' }}", {"x": "", "y": "a", "z": "c"}, False),
        ("literal", {}, "literal"),
    ],
)
def test_expression_semantics_follow_github(expression, context, expected):
    assert policy.evaluate(expression, context) == expected


@pytest.mark.parametrize("expression", ["${{ (a) }}", "${{ contains(a, 'b') }}", "${{ success() }}", "${{ a < b }}"])
def test_expression_evaluator_refuses_what_it_does_not_model(expression):
    with pytest.raises(policy.Unsupported):
        policy.evaluate(expression, {})


WRITER_JOB = """  writer:
    if: GUARD
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - name: Save
        env:
          SIM_CACHE_WRITE_KEY: ${{ secrets.SIM_CACHE_WRITE_KEY }}
        run: echo save
"""


def add_writer_job(copy, guard):
    path = copy / LANE
    text = path.read_text(encoding="utf-8")
    body = WRITER_JOB.replace("    if: GUARD\n", "" if guard is None else f"    if: {guard}\n")
    assert "jobs:\n  route:" in text
    path.write_text(text.replace("jobs:\n  route:", "jobs:\n" + body + "  route:", 1), encoding="utf-8")


DEFAULT_ONLY = "${{ github.ref_name == github.event.repository.default_branch }}"


def test_a_cache_writer_job_gated_on_the_default_branch_is_accepted(copy):
    add_writer_job(copy, DEFAULT_ONLY)
    assert policy.violations(copy) == []


def test_the_bare_form_of_the_default_branch_guard_is_accepted_too(copy):
    add_writer_job(copy, "github.ref_name == github.event.repository.default_branch")
    assert policy.violations(copy) == []


@pytest.mark.parametrize(
    "guard",
    [
        None,
        "${{ github.ref_name != '' }}",
        "${{ always() }}",
        "${{ github.ref_name != github.event.repository.default_branch }}",
        "${{ github.event_name == 'workflow_dispatch' }}",
    ],
)
def test_a_cache_writer_job_that_can_run_off_the_default_branch_is_refused(copy, guard):
    add_writer_job(copy, guard)
    assert any("SIM_CACHE_WRITE" in line for line in policy.violations(copy))


def test_a_cache_writer_key_in_workflow_level_env_is_refused(copy):
    path = copy / ATTEMPT
    text = path.read_text(encoding="utf-8")
    old = "permissions:\n  contents: read\n\njobs:"
    assert old in text
    path.write_text(text.replace(old, "permissions:\n  contents: read\n\nenv:\n  SIM_CACHE_WRITE_KEY: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n\njobs:", 1), encoding="utf-8")
    assert any("outside a job gated" in line for line in policy.violations(copy))


def test_a_cache_writer_key_passed_to_a_called_workflow_without_a_guard_is_refused(copy):
    path = copy / LANE
    text = path.read_text(encoding="utf-8")
    old = "      tag: ${{ inputs.lane }}-attempt\n"
    assert old in text
    path.write_text(text.replace(old, old + "      write_key: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n", 1), encoding="utf-8")
    assert any("SIM_CACHE_WRITE" in line for line in policy.violations(copy))
