from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import workflow_policy as policy

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
LANE = "sim-gate-lane.yml"
ATTEMPT = "sim-gate-attempt.yml"
TOP = "sim-gate.yml"
RETEST = "sim-retest.yml"
CLASSIFY_PIN = "the Classify step must run exactly the pinned classify invocation"
HYGIENE = "      - name: Hygiene\n        if: ${{ runner.environment == 'self-hosted' }}\n        env:\n          SIM_SURVIVORS: dotnet testhost vstest.console VBCSCompiler\n"
TRUSTED = "      - name: Checkout trusted\n        if: ${{ runner.environment == 'self-hosted' }}\n"
SHA = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
ENVIRONMENT = "    environment: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n"


def test_the_shipped_workflows_satisfy_the_policy():
    assert policy.violations(WORKFLOWS, root=ROOT) == []


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
        "needs.route.outputs.target == 'onprem' }}\n    permissions:\n      contents: read\n      actions: read\n      checks: read\n      id-token: write\n    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-retry",
        "needs.route.outputs.target == 'onprem' || needs.attempt.outputs.verdict == 'test' }}\n    permissions:\n      contents: read\n      actions: read\n      checks: read\n      id-token: write\n    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-retry",
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
        "      runner: ubuntu-24.04\n      hosted: true\n      fail_test: ${{ inputs.fail_test }}\n      kill_runner: ${{ inputs.simulate_kill == 'both' }}",
        "      runner: ${{ inputs.lab_label }}\n      hosted: true\n      fail_test: ${{ inputs.fail_test }}\n      kill_runner: ${{ inputs.simulate_kill == 'both' }}",
        "retry must run on a hosted label",
    ),
    "expression inside a run script": (
        ATTEMPT,
        "        run: sh adapters/dotnet/cache_build.sh tripwire \"$MARKERS\"",
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
    "the tailnet join failing the lane": (
        ATTEMPT,
        "        continue-on-error: true\n        timeout-minutes: 3\n",
        "        timeout-minutes: 3\n",
        "the tailnet join must be continue-on-error: true",
    ),
    "the tailnet join without its id": (
        ATTEMPT,
        "        id: join\n",
        "",
        "must have id: join",
    ),
    "the tripwire step blind to the join outcome": (
        ATTEMPT,
        "          SIM_TAILNET_JOIN: ${{ steps.join.outcome }}\n",
        "",
        "must pass SIM_TAILNET_JOIN",
    ),
    "continue-on-error on a step other than the tailnet join": (
        ATTEMPT,
        "      - name: Build\n",
        "      - name: Build\n        continue-on-error: true\n",
        "continue-on-error at jobs.run.steps",
    ),
    "the plan never planning the whole lane": (
        ATTEMPT,
        "          FULL: ${{ inputs.default_tip && ",
        "          FULL: ${{ false && ",
        "plan the whole lane exactly when the cache saves",
    ),
    "the plan planning the whole lane off the default branch tip": (
        ATTEMPT,
        "          FULL: ${{ inputs.default_tip && ",
        "          FULL: ${{ ",
        "plan the whole lane exactly when the cache saves",
    ),
    "the plan ignoring FULL": (
        ATTEMPT,
        "              set -- --all\n",
        "              set -- --affected \"$RUNNER_TEMP/affected.json\"\n",
        "must pass --all when FULL is 1",
    ),
    "validate back to a character check that admits pull request refs": (
        TOP,
        "        run: sh core/resolve_refs.sh\n",
        "        run: echo ok\n",
        "validate must resolve every PR input with sh core/resolve_refs.sh",
    ),
    "validate checking out the requested head instead of the dispatch commit": (
        TOP,
        "          fetch-depth: 0\n          persist-credentials: false\n      - name: Resolve refs",
        "          ref: ${{ inputs.pr_branch }}\n          fetch-depth: 0\n          persist-credentials: false\n      - name: Resolve refs",
        "validate must check out the dispatch commit itself",
    ),
    "validate without the full history to check branch containment": (
        TOP,
        "          fetch-depth: 0\n          persist-credentials: false\n      - name: Resolve refs",
        "          persist-credentials: false\n      - name: Resolve refs",
        "validate must check out the dispatch commit itself",
    ),
    "validate not publishing the resolved head": (
        TOP,
        "      pr_sha: ${{ steps.resolve.outputs.pr_sha }}\n",
        "      pr_sha: ${{ inputs.pr_sha }}\n",
        "validate must output pr_sha, base_sha, default_tip",
    ),
    "the unit lane given the unresolved head": (
        TOP,
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.pr_sha }}\n",
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ inputs.pr_branch }}\n",
        "job unit reads an unresolved PR input",
    ),
    "the integration lane given the unresolved base": (
        TOP,
        "      lane: integration\n      base: ${{ needs.validate.outputs.base_sha }}\n",
        "      lane: integration\n      base: ${{ inputs.base }}\n",
        "lane integration must take head, base and default_tip from validate only",
    ),
    "a lane told it is the default tip without validate": (
        TOP,
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.pr_sha }}\n      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n",
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.pr_sha }}\n      default_tip: true\n",
        "lane unit must take head, base and default_tip from validate only",
    ),
    "the lane attempt claiming the default tip": (
        LANE,
        "      default_tip: ${{ inputs.default_tip }}\n      cache: ${{ inputs.cache }}\n      runner: ${{ needs.route",
        "      default_tip: true\n      cache: ${{ inputs.cache }}\n      runner: ${{ needs.route",
        "the lane attempt must pass head and default_tip through unchanged",
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
    "classify without the reason file": (
        ATTEMPT,
        ' --reason-file "$RUNNER_TEMP/reason.txt"',
        "",
        CLASSIFY_PIN,
    ),
    "classify without the run job result": (
        ATTEMPT,
        ' --run-result "$RUN_RESULT"',
        "",
        CLASSIFY_PIN,
    ),
    "classify with the run job result from another source": (
        ATTEMPT,
        "          RUN_RESULT: ${{ needs.run.result }}",
        "          RUN_RESULT: success",
        CLASSIFY_PIN,
    ),
    "classify with a forced run result appended": (
        ATTEMPT,
        ' --infra-step "Set up .NET")',
        ' --infra-step "Set up .NET" --run-result success)',
        CLASSIFY_PIN,
    ),
    "classify with the run result overwritten in the script": (
        ATTEMPT,
        "          set -eu\n          verdict=$(python3 -m core.classify",
        "          set -eu\n          RUN_RESULT=success\n          verdict=$(python3 -m core.classify",
        CLASSIFY_PIN,
    ),
    "classify without the adapter setup step as infra": (
        ATTEMPT,
        ' --infra-step "Set up .NET")',
        ")",
        CLASSIFY_PIN,
    ),
    "classify with the test step named as infra": (
        ATTEMPT,
        ' --infra-step "Set up .NET")',
        ' --infra-step "Set up .NET" --infra-step Test)',
        CLASSIFY_PIN,
    ),
    "gate without the unit reason": (
        TOP,
        ' --reason "unit=${UNIT_RETRY_REASON:-$UNIT_ATTEMPT_REASON}" ;;',
        " ;;",
        "the Decide step must run exactly the pinned gate invocation",
    ),
    "gate passing when validate selected nothing": (
        TOP,
        '              *) echo "gate: red [test]: validate did not select lanes"; exit 1 ;;\n          esac\n          case "$INTEGRATION_SELECTED"',
        '              *) ;;\n          esac\n          case "$INTEGRATION_SELECTED"',
        "the Decide step must run exactly the pinned gate invocation",
    ),
    "gate not waiting for validate": (
        TOP,
        "    needs:\n      - validate\n      - unit\n",
        "    needs:\n      - unit\n",
        "the gate must need validate and every lane",
    ),
    "a lane job without the validate selection guard": (
        TOP,
        "    if: ${{ needs.validate.outputs.lane_unit == 'true' }}\n",
        "",
        "job unit must run exactly when validate selects it",
    ),
    "a lane job guarded on another lane's selection": (
        TOP,
        "    if: ${{ needs.validate.outputs.lane_unit == 'true' }}\n",
        "    if: ${{ needs.validate.outputs.lane_integration == 'true' }}\n",
        "job unit must run exactly when validate selects it",
    ),
    "a lane job the validate lane list does not know": (
        TOP,
        "          KNOWN_LANES: unit integration\n",
        "          KNOWN_LANES: unit\n",
        "the lane names differ between",
    ),
    "a validate lane output with no lane job": (
        TOP,
        "      lane_integration: ${{ steps.resolve.outputs.lane_integration }}\n",
        "      lane_integration: ${{ steps.resolve.outputs.lane_integration }}\n      lane_e2e: ${{ steps.resolve.outputs.lane_e2e }}\n",
        "the lane names differ between",
    ),
    "the entry with an eleventh input": (
        TOP,
        "      retest_of:\n",
        "      extra:\n        required: false\n        type: string\n      retest_of:\n",
        "the entry contract is",
    ),
    "the entry without the pr_author input": (
        TOP,
        "      pr_author:\n        description: Login of the pull request author, or empty\n        required: false\n        default: \"\"\n        type: string\n",
        "",
        "the entry contract is",
    ),
    "pr_branch defaulting to another branch": (
        TOP,
        "        default: main\n",
        "        default: develop\n",
        "input pr_branch has default 'develop'",
    ),
    "cache as a free string": (
        TOP,
        "        default: \"use\"\n        type: choice\n",
        "        default: \"use\"\n        type: string\n",
        "input cache has type 'string'",
    ),
    "a fault option no job reads": (
        TOP,
        "          - kill_both\n",
        "          - kill_both\n          - fail_health_once\n",
        "fault options are",
    ),
    "the unit lane ignoring the fail_test fault": (
        TOP,
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.pr_sha }}\n      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n      cache: ${{ inputs.cache }}\n      fail_test: ${{ inputs.fault == 'fail_test' }}\n",
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.pr_sha }}\n      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n      cache: ${{ inputs.cache }}\n      fail_test: false\n",
        "lane unit maps fault fail_test",
    ),
    "the integration lane killing on the onprem-only fault everywhere": (
        TOP,
        "      fail_test: ${{ inputs.fault == 'fail_test' }}\n      simulate_kill: ${{ inputs.fault == 'kill_onprem' && 'onprem' || inputs.fault == 'kill_both' && 'both' || 'none' }}\n      lab_label: ${{ vars.SIM_LAB_LABEL_INTEGRATION }}",
        "      fail_test: ${{ inputs.fault == 'fail_test' }}\n      simulate_kill: ${{ inputs.fault == 'kill_onprem' && 'both' || inputs.fault == 'kill_both' && 'both' || 'none' }}\n      lab_label: ${{ vars.SIM_LAB_LABEL_INTEGRATION }}",
        "lane integration maps fault kill_onprem",
    ),
    "a lane dropping the cache input": (
        TOP,
        "      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n      cache: ${{ inputs.cache }}\n",
        "      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n",
        "lane unit must pass cache",
    ),
    "the run name never marking a retest": (
        TOP,
        " && format(' retest of {0} (Retest)', inputs.retest_of) || ''",
        " && '' || ''",
        "must contain 'retest of 1' and end with (Retest)",
    ),
    "the run name marking every run as a retest": (
        TOP,
        "inputs.retest_of != '' && format(' retest of {0} (Retest)', inputs.retest_of) || ''",
        "format(' retest of {0} (Retest)', inputs.retest_of)",
        "without retest_of must not say retest",
    ),
    "the run name with the retest marker in the middle": (
        TOP,
        "format('sim-gate {0} {1} from {2}{3}{4}'",
        "format('sim-gate {0}{4} {1} from {2}{3}'",
        "must contain 'retest of 1' and end with (Retest)",
    ),
    "the run name hiding a partial lane selection": (
        TOP,
        "inputs.lanes != '' && format(' lanes {0}', inputs.lanes) || ''",
        "''",
        "must name the selected lanes unit",
    ),
    "the run name naming lanes on a full run": (
        TOP,
        "inputs.lanes != '' && format(' lanes {0}', inputs.lanes) || ''",
        "format(' lanes {0}', inputs.lanes)",
        "must not name lanes when every lane runs",
    ),
    "the run name with the lanes after the retest marker": (
        TOP,
        "format('sim-gate {0} {1} from {2}{3}{4}'",
        "format('sim-gate {0} {1} from {2}{4}{3}'",
        "must end with ' retest of 7 (Retest)'",
    ),
    "the retest stand-in on a push trigger": (
        RETEST,
        "on:\n  workflow_dispatch:",
        "on:\n  push:\n  workflow_dispatch:",
        "sim-retest.yml: must be workflow_dispatch only",
    ),
    "the retest stand-in on workflow_run": (
        RETEST,
        "on:\n  workflow_dispatch:",
        "on:\n  workflow_run:\n    workflows: [sim-gate]\n  workflow_dispatch:",
        "sim-retest.yml: forbidden trigger workflow_run",
    ),
    "the retest stand-in stub asking for actions write": (
        RETEST,
        "    timeout-minutes: 1\n",
        "    timeout-minutes: 1\n    permissions:\n      contents: read\n      actions: read\n",
        "sim-retest.yml: job requeue permissions are",
    ),
    "the gate job given actions read": (
        TOP,
        "    if: ${{ always() }}\n    runs-on: ubuntu-24.04\n",
        "    if: ${{ always() }}\n    permissions:\n      contents: read\n      actions: read\n    runs-on: ubuntu-24.04\n",
        "sim-gate.yml: job gate permissions are",
    ),
    "the classify job without checks read": (
        ATTEMPT,
        "      contents: read\n      actions: read\n      checks: read\n    outputs:\n      verdict:",
        "      contents: read\n      actions: read\n    outputs:\n      verdict:",
        "job classify permissions are",
    ),
    "the tailnet join without a step timeout": (
        ATTEMPT,
        "        continue-on-error: true\n        timeout-minutes: 3\n",
        "        continue-on-error: true\n",
        "the tailnet join step needs timeout-minutes of 5 or less",
    ),
    "the tailnet join with a step timeout above the cap": (
        ATTEMPT,
        "        continue-on-error: true\n        timeout-minutes: 3\n",
        "        continue-on-error: true\n        timeout-minutes: 10\n",
        "the tailnet join step needs timeout-minutes of 5 or less",
    ),
    "lane without the retry reason output": (
        LANE,
        "      retry_reason:" + chr(10),
        "      retry_renamed:" + chr(10),
        "does not output retry_reason",
    ),
    "a secret read in the Test step env": (
        ATTEMPT,
        "          KILL_RUNNER: ${{ inputs.kill_runner }}\n",
        "          KILL_RUNNER: ${{ inputs.kill_runner }}\n          LEAK: ${{ secrets.OTHER_SECRET }}\n",
        "secret OTHER_SECRET is read at a place that is not allowed",
    ),
    "the route token read in a second job": (
        LANE,
        "  attempt:\n    name: attempt\n",
        "  attempt:\n    name: attempt\n    env:\n      T: ${{ secrets.SIM_ROUTE_TOKEN }}\n",
        "secret SIM_ROUTE_TOKEN is read at a place that is not allowed",
    ),
    "hygiene step deleted": (
        ATTEMPT,
        "      - name: Hygiene\n",
        "      - name: Cleanup\n",
        "has no Hygiene step",
    ),
    "hygiene step only on hosted runners": (
        ATTEMPT,
        HYGIENE,
        HYGIENE.replace("'self-hosted'", "'github-hosted'"),
        "Hygiene step must run on self-hosted runners only",
    ),
    "hygiene step without a guard": (
        ATTEMPT,
        HYGIENE,
        HYGIENE.replace("        if: ${{ runner.environment == 'self-hosted' }}\n", ""),
        "Hygiene step must run on self-hosted runners only",
    ),
    "hygiene step running the head tree copy": (
        ATTEMPT,
        "              sh .sim-trusted/core/prejob.sh || status=$?\n",
        "              sh core/prejob.sh || status=$?\n",
        "must run the trusted copy exactly",
    ),
    "hygiene step back to the head tree one-liner": (
        ATTEMPT,
        "        run: |\n          set -u\n          status=0\n          if [ -L .sim-trusted ]",
        "        run: sh core/prejob.sh\n        shell: |\n          set -u\n          status=0\n          if [ -L .sim-trusted ]",
        "must run the trusted copy exactly",
    ),
    "hygiene step keeping the trusted copy in the workspace": (
        ATTEMPT,
        "          rm -rf .sim-trusted\n",
        "",
        "must run the trusted copy exactly",
    ),
    "hygiene step skipping the dispatch commit check": (
        ATTEMPT,
        ' || [ "$(git -C .sim-trusted rev-parse HEAD)" != "$GITHUB_SHA" ]',
        "",
        "must run the trusted copy exactly",
    ),
    "hygiene step trusting a directory that is not its own checkout": (
        ATTEMPT,
        " || [ ! -d .sim-trusted/.git ]",
        "",
        "must run the trusted copy exactly",
    ),
    "hygiene step with core survivor names gone": (
        ATTEMPT,
        "          SIM_SURVIVORS: dotnet testhost vstest.console VBCSCompiler\n",
        "          SIM_SURVIVORS: node\n",
        "must pass exactly SIM_SURVIVORS",
    ),
    "hygiene step after the first dotnet step": (
        ATTEMPT,
        HYGIENE,
        "      - name: Health\n        run: echo early\n" + HYGIENE,
        "must come before",
    ),
    "the trusted checkout pinned to the head": (
        ATTEMPT,
        "          path: .sim-trusted\n",
        "          ref: ${{ inputs.head }}\n          path: .sim-trusted\n",
        "the trusted checkout must not take a ref",
    ),
    "the trusted checkout keeping credentials": (
        ATTEMPT,
        "          path: .sim-trusted\n          persist-credentials: false\n",
        "          path: .sim-trusted\n          persist-credentials: true\n",
        "the trusted checkout must be actions/checkout with exactly",
    ),
    "the trusted checkout of the whole tree": (
        ATTEMPT,
        "          sparse-checkout: core\n",
        "",
        "the trusted checkout must be actions/checkout with exactly",
    ),
    "the trusted checkout deleted": (
        ATTEMPT,
        "      - name: Checkout trusted\n",
        "      - name: Checkout spare\n",
        "has no Checkout trusted step",
    ),
    "the trusted checkout on hosted runners too": (
        ATTEMPT,
        TRUSTED,
        "      - name: Checkout trusted\n",
        "the trusted checkout must run on self-hosted runners only",
    ),
    "retry still runs after a cache tripwire": (
        LANE,
        " && needs.attempt.outputs.reason != 'cache tripwire'",
        "",
        "with reason 'cache tripwire'",
    ),
    "lab label input without the opaque warning": (
        LANE,
        "        description: Opaque runner label; it is printed unmasked in public logs, so never a host name\n",
        "",
        "must say the label is public in logs",
    ),
    "both lanes on one lab label": (
        TOP,
        "lab_label: ${{ vars.SIM_LAB_LABEL_INTEGRATION }}",
        "lab_label: ${{ vars.SIM_LAB_LABEL_UNIT }}",
        "share one lab label expression",
    ),
    "unit lane on a shared label variable": (
        TOP,
        "lab_label: ${{ vars.SIM_LAB_LABEL_UNIT }}",
        "lab_label: ${{ vars.SIM_LAB_LABEL }}",
        "lane unit must use its own variable SIM_LAB_LABEL_UNIT",
    ),
    "attempt checkout without a ref": (
        ATTEMPT,
        "          ref: ${{ inputs.head }}\n          fetch-depth: 0",
        "          fetch-depth: 0",
        "must equal the head given to affected",
    ),
    "attempt checkout of another commit than the selection head": (
        ATTEMPT,
        "          ref: ${{ inputs.head }}\n          fetch-depth: 0",
        "          ref: ${{ github.sha }}\n          fetch-depth: 0",
        "must equal the head given to affected",
    ),
    "affected selecting against another head than the checkout": (
        ATTEMPT,
        "          HEAD_REF: ${{ inputs.head }}",
        "          HEAD_REF: ${{ inputs.base }}",
        "must equal the head given to affected",
    ),
    "tailnet join without a hosted-only guard": (
        ATTEMPT,
        "      - name: Join the tailnet\n        id: join\n        if: ${{ runner.environment == 'github-hosted' }}\n",
        "      - name: Join the tailnet\n        id: join\n",
        "tailnet credentials may be used only by the tailscale action on a github-hosted runner",
    ),
    "tailnet join on self-hosted runners": (
        ATTEMPT,
        "      - name: Join the tailnet\n        id: join\n        if: ${{ runner.environment == 'github-hosted' }}\n",
        "      - name: Join the tailnet\n        id: join\n        if: ${{ runner.environment == 'self-hosted' }}\n",
        "tailnet credentials may be used only by the tailscale action on a github-hosted runner",
    ),
    "tailnet client id read by another step": (
        ATTEMPT,
        "          FAIL_TEST: ${{ inputs.fail_test && '1' || '0' }}\n",
        "          FAIL_TEST: ${{ inputs.fail_test && '1' || '0' }}\n          SIM_TAILNET_CLIENT_ID: ${{ secrets.SIM_TAILNET_CLIENT_ID }}\n",
        "tailnet credentials may be used only by the tailscale action",
    ),
    "tailnet audience read in the route job": (
        LANE,
        "          LAB_LABEL: ${{ inputs.lab_label }}\n",
        "          LAB_LABEL: ${{ inputs.lab_label }}\n          T: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "secret SIM_TAILNET_AUDIENCE is read at a place that is not allowed",
    ),
    "tailnet credential given to another action input": (
        ATTEMPT,
        "          audience: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "          audience: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n          ping: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "secret SIM_TAILNET_AUDIENCE is read at a place that is not allowed",
    ),
    "tailscale with a stored auth key": (
        ATTEMPT,
        "          tags: tag:ci-sim\n",
        "          tags: tag:ci-sim\n          authkey: ${{ secrets.SIM_TAILNET_AUTHKEY }}\n",
        "workload identity, not a stored auth key",
    ),
    "tailscale with a stored OAuth secret": (
        ATTEMPT,
        "          tags: tag:ci-sim\n",
        "          tags: tag:ci-sim\n          oauth-secret: ${{ secrets.SIM_TAILNET_OAUTH_SECRET }}\n",
        "workload identity, not a stored auth key",
    ),
    "tailscale with other tags": (
        ATTEMPT,
        "          tags: tag:ci-sim\n",
        "          tags: tag:ci-sim,tag:lab-cache\n",
        "must join with tags: tag:ci-sim",
    ),
    "tailscale without tags": (
        ATTEMPT,
        "          tags: tag:ci-sim\n",
        "",
        "must join with tags: tag:ci-sim",
    ),
    "tailscale accepting routes": (
        ATTEMPT,
        "          args: --accept-routes=false\n",
        "",
        "--accept-routes=false",
    ),
    "tailscale using its binary cache": (
        ATTEMPT,
        "          use-cache: \"false\"\n",
        "          use-cache: \"true\"\n",
        "must not use its binary cache",
    ),
    "tailscale without the audience": (
        ATTEMPT,
        "          audience: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "",
        "needs oauth-client-id and audience",
    ),
    "cache endpoint read in the route job": (
        LANE,
        "          LAB_LABEL: ${{ inputs.lab_label }}\n",
        "          LAB_LABEL: ${{ inputs.lab_label }}\n          SIM_CACHE_ENDPOINT_LAB: ${{ secrets.SIM_CACHE_ENDPOINT_LAB }}\n",
        "secret SIM_CACHE_ENDPOINT_LAB is read at a place that is not allowed",
    ),
    "cache endpoint read in the classify job": (
        ATTEMPT,
        "          ARTIFACT_NAME: markers-${{ inputs.tag }}\n",
        "          ARTIFACT_NAME: markers-${{ inputs.tag }}\n          SIM_CACHE_ENDPOINT_TAILNET: ${{ secrets.SIM_CACHE_ENDPOINT_TAILNET }}\n",
        "secret SIM_CACHE_ENDPOINT_TAILNET is read at a place that is not allowed",
    ),
    "cache endpoint taken from a repository variable": (
        ATTEMPT,
        "          SIM_CACHE_ENDPOINT_LAB: ${{ secrets.SIM_CACHE_ENDPOINT_LAB }}\n          SIM_CACHE_ENDPOINT_TAILNET: ${{ secrets.SIM_CACHE_ENDPOINT_TAILNET }}\n          SIM_CACHE_READ_KEY_ID",
        "          SIM_CACHE_ENDPOINT_LAB: ${{ vars.SIM_CACHE_ENDPOINT_LAB }}\n          SIM_CACHE_ENDPOINT_TAILNET: ${{ secrets.SIM_CACHE_ENDPOINT_TAILNET }}\n          SIM_CACHE_READ_KEY_ID",
        "read from vars",
    ),
    "reader key read in the route job": (
        LANE,
        "          LAB_LABEL: ${{ inputs.lab_label }}\n",
        "          LAB_LABEL: ${{ inputs.lab_label }}\n          SIM_CACHE_READ_SECRET: ${{ secrets.SIM_CACHE_READ_SECRET }}\n",
        "secret SIM_CACHE_READ_SECRET is read at a place that is not allowed",
    ),
    "dotnet test that rebuilds": (
        ATTEMPT,
        "-c Release --no-build --filter",
        "-c Release --filter",
        "dotnet test without --no-build",
    ),
    "a raw dotnet build that bypasses the cache": (
        ATTEMPT,
        '          touch "$MARKERS/build"\n',
        '          dotnet build sim/dotnet/Sim.sln\n          touch "$MARKERS/build"\n',
        "dotnet build outside cache_build.sh bypasses the cache",
    ),
    "the build step not going through the cache script": (
        ATTEMPT,
        'sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/build.txt" "$RUNNER_TEMP/built.txt"',
        "echo build",
        "Build step must run exactly the pinned script",
    ),
    "the cache tripwire step removed": (
        ATTEMPT,
        '      - name: Cache tripwire\n        env:\n          SIM_TAILNET_JOIN: ${{ steps.join.outcome }}\n          SIM_CACHE_ENDPOINT_LAB: ${{ secrets.SIM_CACHE_ENDPOINT_LAB }}\n          SIM_CACHE_ENDPOINT_TAILNET: ${{ secrets.SIM_CACHE_ENDPOINT_TAILNET }}\n          SIM_CACHE_READ_KEY_ID: ${{ secrets.SIM_CACHE_READ_KEY_ID }}\n          SIM_CACHE_READ_SECRET: ${{ secrets.SIM_CACHE_READ_SECRET }}\n        run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS"\n',
        "",
        "tripwire step must come before the Build step",
    ),
    "the cache tripwire step running something else": (
        ATTEMPT,
        'run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS"',
        "run: echo clear",
        "cache tripwire step must run exactly",
    ),
    "the cache tripwire step before the plan": (
        ATTEMPT,
        "      - name: Plan\n",
        "      - name: Cache tripwire\n        run: sh adapters/dotnet/cache_build.sh tripwire \"$MARKERS\"\n      - name: Plan\n",
        "tripwire step must come after Plan",
    ),
    "the save step running something else": (
        ATTEMPT,
        'run: sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        "run: echo save",
        "writer key must run exactly",
    ),
    "the build step calling the cache script at a stale path": (
        ATTEMPT,
        'sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/build.txt"',
        'sh old/cache_build.sh build "$RUNNER_TEMP/build.txt"',
        "Build step must run exactly the pinned script",
    ),
    "the cache tripwire step calling the cache script at a stale path": (
        ATTEMPT,
        'run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS"',
        'run: sh old/cache_build.sh tripwire "$MARKERS"',
        "cache tripwire step must run exactly",
    ),
    "the save step calling the cache script at a stale path": (
        ATTEMPT,
        'run: sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        'run: sh old/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        "writer key must run exactly",
    ),
    "the save step with a command appended": (
        ATTEMPT,
        'run: sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        'run: sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"; env > "$RUNNER_TEMP/leak.txt"',
        "writer key must run exactly",
    ),
    "the cache tripwire step with a command appended": (
        ATTEMPT,
        'run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS"',
        'run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS" || true',
        "cache tripwire step must run exactly",
    ),
    "the build step with a command appended": (
        ATTEMPT,
        '          touch "$MARKERS/build"\n',
        '          touch "$MARKERS/build"\n          rm -rf "$MARKERS"\n',
        "Build step must run exactly the pinned script",
    ),
    "the save step without the default branch name for the tip check": (
        ATTEMPT,
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n",
        "",
        "must pass the default branch name",
    ),
    "the save step taking the tip name from an input": (
        ATTEMPT,
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n",
        "          SIM_DEFAULT_BRANCH: ${{ inputs.head }}\n",
        "must pass the default branch name",
    ),
    "the attempt job always writing": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: sim-cache-write\n",
        "the attempt environment is 'sim-cache-write'",
    ),
    "the attempt job never writing": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: sim-cache-read\n",
        "wanted 'sim-cache-write'",
    ),
    "the attempt job without an environment": (
        ATTEMPT,
        ENVIRONMENT,
        "",
        "must choose its environment with an expression",
    ),
    "the attempt environment taken from the ref name so a tag named like the default branch writes": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ inputs.default_tip && github.ref_name == github.event.repository.default_branch && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n",
        "for ref refs/tags/main",
    ),
    "the attempt environment writing for any head": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n",
        "default_tip False",
    ),
    "the attempt environment writing with fail_test on": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.hosted == true && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n",
        "fail_test True",
    ),
    "the attempt environment writing from a lab runner": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.fail_test == false && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n",
        "hosted False",
    ),
    "the attempt environment writing from a topic branch": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ inputs.default_tip && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n",
        "for ref refs/heads/topic",
    ),
    "the attempt environment comparing the head by name, where a tag MAIN matches main": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.head == github.event.repository.default_branch && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || 'sim-cache-read' }}\n",
        "must not compare inputs.head by name",
    ),
    "the attempt environment writing for a case variant of the default branch name": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.head == github.event.repository.default_branch && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || 'sim-cache-read' }}\n",
        "head MAIN, default_tip False, fail_test False, hosted True; wanted 'sim-cache-read'",
    ),
    "the attempt environment giving a lab runner the hosted reader environment": (
        ATTEMPT,
        ENVIRONMENT,
        "    environment: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || 'sim-cache-read' }}\n",
        "wanted 'sim-cache-lab'",
    ),
    "a ref name read anywhere because a tag can carry the default branch name": (
        ATTEMPT,
        "          FAIL_TEST: ${{ inputs.fail_test && '1' || '0' }}\n",
        "          FAIL_TEST: ${{ inputs.fail_test && '1' || '0' }}\n          WHERE: ${{ github.ref_name }}\n",
        "github.ref_name is refused",
    ),
    "the route job without its environment so its secret would be empty": (
        LANE,
        "    environment: sim-route\n",
        "",
        "reads secrets without an environment",
    ),
    "the lane treating a lab label run as hosted": (
        LANE,
        "      hosted: ${{ needs.route.outputs.target != 'onprem' || inputs.lab_label == '' }}\n",
        "      hosted: true\n",
        "hosted input disagrees with the runner",
    ),
    "a hosted retry declared as not hosted": (
        LANE,
        "      hosted: true\n",
        "      hosted: false\n",
        "the retry must be hosted",
    ),
    "id-token write on the classify job": (
        ATTEMPT,
        "      actions: read\n      checks: read\n    outputs:",
        "      actions: read\n      checks: read\n      id-token: write\n    outputs:",
        "permission jobs.classify.permissions.id-token is write",
    ),
    "id-token write on the route job": (
        LANE,
        "    environment: sim-route\n",
        "    environment: sim-route\n    permissions:\n      contents: read\n      id-token: write\n",
        "permission jobs.route.permissions.id-token is write",
    ),
    "id-token write for the whole workflow": (
        ATTEMPT,
        "permissions:\n  contents: read\n\njobs:",
        "permissions:\n  contents: read\n  id-token: write\n\njobs:",
        "workflow permissions must be contents: read only",
    ),
    "the attempt job without id-token write so workload identity cannot work": (
        ATTEMPT,
        "      contents: read\n      id-token: write\n",
        "      contents: read\n",
        "exactly contents: read and id-token: write",
    ),
}


@pytest.fixture
def copy(tmp_path):
    target = tmp_path / "workflows"
    target.mkdir()
    for path in WORKFLOWS.glob("*.yml"):
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


def mutate(copy, filename, old, new):
    path = copy / filename
    text = path.read_text(encoding="utf-8")
    assert old in text, "the mutation no longer matches the workflow"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_a_non_reader_secret_passed_into_the_attempt_workflow_is_refused(copy):
    mutate(
        copy,
        LANE,
        "      tag: ${{ inputs.lane }}-attempt\n",
        "      tag: ${{ inputs.lane }}-attempt\n",
    )
    path = copy / LANE
    text = path.read_text(encoding="utf-8")
    old = "    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-attempt"
    assert old in text
    path.write_text(
        text.replace(old, old.replace("    with:", "    secrets:\n      OTHER_SECRET: ${{ secrets.OTHER_SECRET }}\n    with:"), 1),
        encoding="utf-8",
    )
    found = policy.violations(copy)
    assert any("no secrets mapping or declaration is allowed" in line for line in found), found
    assert any("secret OTHER_SECRET is read" in line for line in found), found


READER_STEP_ENV = "          KILL_RUNNER: ${{ inputs.kill_runner }}\n"


def test_the_reader_key_may_be_read_by_the_attempt_job_on_any_runner(copy):
    mutate(copy, ATTEMPT, READER_STEP_ENV, READER_STEP_ENV + "          SIM_CACHE_READ_KEY: ${{ secrets.SIM_CACHE_READ_KEY }}\n")
    assert policy.violations(copy) == []


def test_the_reader_key_passed_to_the_attempt_workflow_through_secrets_is_refused(copy):
    path = copy / LANE
    text = path.read_text(encoding="utf-8")
    old = "    uses: ./.github/workflows/sim-gate-attempt.yml # zizmor: ignore[self-repository]\n    with:\n      tag: ${{ inputs.lane }}-attempt"
    assert old in text
    new = old.replace("    with:", "    secrets:\n      SIM_CACHE_READ_KEY_ID: ${{ secrets.SIM_CACHE_READ_KEY_ID }}\n    with:")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    found = policy.violations(copy)
    assert any("no secrets mapping or declaration is allowed" in line for line in found), found
    assert any("SIM_CACHE_READ_KEY_ID is read at a place that is not allowed" in line for line in found), found


def test_the_reader_key_under_a_different_name_or_expression_is_refused(copy):
    mutate(copy, ATTEMPT, READER_STEP_ENV, READER_STEP_ENV + "          SIM_CACHE_READ_KEY: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n")
    assert any("not allowed" in line for line in policy.violations(copy))


def test_the_writer_key_in_a_step_env_of_an_unguarded_attempt_job_is_refused(copy):
    mutate(copy, ATTEMPT, READER_STEP_ENV, READER_STEP_ENV + "          SIM_CACHE_WRITE_KEY: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n")
    assert any("SIM_CACHE_WRITE" in line for line in policy.violations(copy))


def test_a_workflow_call_secrets_declaration_for_a_non_reader_secret_is_refused(copy):
    mutate(
        copy,
        ATTEMPT,
        "on:\n  workflow_call:\n    inputs:",
        "on:\n  workflow_call:\n    secrets:\n      OTHER_SECRET:\n        required: false\n    inputs:",
    )
    assert any("no secrets mapping or declaration is allowed" in line for line in policy.violations(copy))


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
        ("${{ format('refs/heads/{0}', a) }}", {"a": "main"}, "refs/heads/main"),
        ("${{ r == format('refs/heads/{0}', a) }}", {"a": "main", "r": "refs/tags/main"}, False),
        ("${{ r == format('refs/heads/{0}', a) }}", {"a": "main", "r": "refs/heads/main"}, True),
        ("${{ format('{0}-{1}', a, b) }}", {"a": "x", "b": "y"}, "x-y"),
    ],
)
def test_expression_semantics_follow_github(expression, context, expected):
    assert policy.evaluate(expression, context) == expected


@pytest.mark.parametrize("expression", ["${{ (a) }}", "${{ contains(a, 'b') }}", "${{ success() }}", "${{ a < b }}"])
def test_expression_evaluator_refuses_what_it_does_not_model(expression):
    with pytest.raises(policy.Unsupported):
        policy.evaluate(expression, {})


SAVE_GUARD = (
    "        if: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    " && inputs.fail_test == false && runner.environment == 'github-hosted' }}\n"
)


def with_save_guard(copy, guard):
    mutate(copy, ATTEMPT, SAVE_GUARD, "" if guard is None else f"        if: {guard}\n")


def test_the_bare_form_of_the_save_guard_is_accepted_too(copy):
    with_save_guard(
        copy,
        "inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        " && inputs.fail_test == false && runner.environment == 'github-hosted'",
    )
    assert policy.violations(copy) == []


GATED = "must itself be gated on the default branch"


@pytest.mark.parametrize(
    "guard, message",
    [
        (None, GATED),
        ("${{ github.ref_name == github.event.repository.default_branch && inputs.default_tip && inputs.fail_test == false && runner.environment == 'github-hosted' }}", GATED),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.default_tip && inputs.fail_test == false }}", GATED),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.default_tip && runner.environment == 'github-hosted' }}", "must not run when fail_test is on"),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.default_tip && inputs.fail_test == false && runner.environment == 'self-hosted' }}", GATED),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.fail_test == false && runner.environment == 'github-hosted' }}", GATED),
        ("${{ inputs.default_tip && inputs.fail_test == false && runner.environment == 'github-hosted' }}", GATED),
        ("${{ github.ref != '' && inputs.default_tip && inputs.fail_test == false && runner.environment == 'github-hosted' }}", GATED),
        ("${{ always() }}", GATED),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.head == github.event.repository.default_branch && inputs.fail_test == false && runner.environment == 'github-hosted' }}", GATED),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.head == github.event.repository.default_branch && inputs.fail_test == false && runner.environment == 'github-hosted' }}", "must not compare inputs.head by name"),
        ("${{ github.event_name == 'workflow_dispatch' }}", GATED),
        ("${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.default_tip && inputs.fail_test == true && runner.environment == 'github-hosted' }}", GATED),
    ],
)
def test_a_cache_save_step_that_can_run_off_the_default_branch_or_with_fail_test_is_refused(copy, guard, message):
    with_save_guard(copy, guard)
    assert any(message in line for line in policy.violations(copy)), policy.violations(copy)


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


def test_ci_shellchecks_the_hook_and_every_sim_gate_script():
    jobs = policy.parse((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    runs = [str(step.get("run", "")).split() for step in jobs["workflow-lint"]["steps"]]
    shellcheck = [words for words in runs if words[:1] == ["shellcheck"]]
    assert len(shellcheck) == 1, runs
    assert {".githooks/pre-push", "core/*.sh", "adapters/dotnet/*.sh"} <= set(shellcheck[0][1:])
    assert {path.name for path in (WORKFLOWS.parents[1] / "core").glob("*.sh")} >= {"kill_own_runner.sh", "prejob.sh", "resolve_refs.sh"}
    assert {path.name for path in (WORKFLOWS.parents[1] / "adapters" / "dotnet").glob("*.sh")} >= {"cache_build.sh"}


CI_STEP = "      - name: Run tests\n        run: python -m pytest\n"


def test_the_trusted_checkout_before_the_head_checkout_is_refused(copy):
    text = (copy / ATTEMPT).read_text(encoding="utf-8")
    start = text.index("      - name: Checkout trusted\n")
    block = text[start:text.index("      - name: Hygiene\n")]
    mutate(copy, ATTEMPT, block, "")
    mutate(copy, ATTEMPT, "      - name: Checkout\n", block + "      - name: Checkout\n")
    assert any("must come after the head checkout" in line for line in policy.violations(copy))


@pytest.mark.parametrize(
    "filename, old, new, expected",
    [
        ("ci.yml", CI_STEP, CI_STEP + "        env:\n          K: ${{ secrets.SIM_CACHE_WRITE_KEY_ID }}\n", "ci.yml: names the cache writer key"),
        ("ci.yml", CI_STEP, CI_STEP + "        env:\n          K: ${{ secrets.sim_cache_write_secret }}\n", "ci.yml: names the cache writer key"),
        ("ci.yml", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    environment: sim-cache-write\n", "ci.yml: names the cache writer key"),
        ("ci.yml", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    environment:\n      name: SIM-CACHE-WRITE\n", "ci.yml: names the cache writer key"),
        ("ci.yml", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    environment: \"sim-cache-\\x77rite\"\n", "ci.yml: YAML escape sequences are refused"),
        ("ci.yml", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    environment: sim-route\n", "ci.yml: environment at jobs.test.environment is not in the environment allowlist"),
        ("ci.yml", CI_STEP, CI_STEP + "        env:\n          ALL: ${{ toJSON(secrets) }}\n", "ci.yml: the secrets context may only be read by name"),
        ("trial-gate.yml", "          JOB: ${{ toJSON(matrix) }}\n", "          JOB: ${{ secrets[format('SIM_CACHE_{0}_SECRET', 'WRITE')] }}\n", "the secrets context may only be read by name"),
        ("ci.yml", "        run: python -m pytest\n", "        run: python -m pytest ${{ github.event.pull_request.title }}\n", "ci.yml: expression inside run"),
        (LANE, "    environment: sim-route\n", "    environment: sim-cache-write\n", "the cache writer key or environment is named at jobs.route.environment"),
        (ATTEMPT, "          KILL_RUNNER: ${{ inputs.kill_runner }}\n", "          KILL_RUNNER: ${{ inputs.kill_runner }}\n          SIM_CACHE_WRITE_SECRET: ${{ secrets.SIM_CACHE_WRITE_SECRET }}\n", "the cache writer key or environment is named at jobs.run.steps"),
    ],
)
def test_every_workflow_file_is_scanned_for_the_writer_tier_and_whole_secrets_reads(copy, filename, old, new, expected):
    mutate(copy, filename, old, new)
    found = policy.violations(copy)
    assert any(expected in line for line in found), found


def test_a_workflow_file_outside_the_policy_is_refused(copy):
    (copy / "extra.yaml").write_text("name: extra\non:\n  push:\npermissions:\n  contents: read\njobs: {}\n".replace("jobs: {}", "jobs:\n  x:\n    runs-on: ubuntu-24.04\n"), encoding="utf-8")
    assert any("a new workflow needs a policy entry" in line for line in policy.violations(copy))


def test_a_workflow_the_policy_cannot_read_is_refused(copy):
    mutate(copy, "ci.yml", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env: {A: b}\n")
    assert any("ci.yml: the policy cannot read this workflow" in line for line in policy.violations(copy))


def test_every_shipped_invocation_resolves_and_the_scan_sees_the_known_ones():
    assert policy.invocation_violations(WORKFLOWS, ROOT) == []


@pytest.mark.parametrize(
    "filename, old, new, expected",
    [
        (TOP, "          python3 -m core.gate \"$@\"\n", "          python3 -m core.gates \"$@\"\n", "python -m core.gates does not resolve"),
        (LANE, "python3 -m core.route", "python3 -m core.route_old", "python -m core.route_old does not resolve"),
        (LANE, "python3 -m core.route", "python3 -m tools.sim_gate.route", "python -m tools.sim_gate.route does not resolve"),
        (ATTEMPT, '          python3 -m adapters.dotnet.plan "$@" --lane "$LANE" --emit build', '          python3 \\\n              -m adapters.dotnet.planx "$@" --lane "$LANE" --emit build', "python -m adapters.dotnet.planx does not resolve"),
        (TOP, "        run: sh core/resolve_refs.sh\n", "        run: sh tools/sim_gate/resolve_refs.sh\n", "tools/sim_gate/resolve_refs.sh does not resolve"),
        (ATTEMPT, "              sh .sim-trusted/core/prejob.sh || status=$?\n", "              sh .sim-trusted/core/prejob_old.sh || status=$?\n", ".sim-trusted/core/prejob_old.sh does not resolve"),
        (ATTEMPT, 'sh "$GITHUB_WORKSPACE/core/kill_own_runner.sh"', 'bash "$GITHUB_WORKSPACE/core/kill_runner.sh"', "core/kill_runner.sh does not resolve"),
        (ATTEMPT, 'sh "$GITHUB_WORKSPACE/core/kill_own_runner.sh"', '"$GITHUB_WORKSPACE/core/kill_own_runner.sh" && ./core/missing.sh', "./core/missing.sh does not resolve"),
        ("trial-gate.yml", "python3 -m workload run", "python3 workload/missing.py run", "workload/missing.py does not resolve"),
        (TOP, "    uses: ./.github/workflows/sim-gate-lane.yml", "    uses: ./.github/workflows/sim-gate-lanes.yml", "uses ./.github/workflows/sim-gate-lanes.yml, which does not exist"),
        (LANE, "python3 -m core.route", "python3 -c 'import core.route'", "no workflow runs python -m core.route"),
        (TOP, "        run: sh core/resolve_refs.sh\n", "        run: . core/resolve_refs.sh\n", "no workflow runs core/resolve_refs.sh"),
    ],
)
def test_an_invocation_that_does_not_resolve_at_the_head_is_refused(copy, filename, old, new, expected):
    mutate(copy, filename, old, new)
    found = policy.invocation_violations(copy, ROOT)
    assert any(expected in line for line in found), found
