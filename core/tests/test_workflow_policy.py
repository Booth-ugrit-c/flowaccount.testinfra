from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import workflow_policy as policy

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
TOP = "sim-gate.yml"
RETEST = "sim-retest.yml"
ROUTE = "tpl-route.yml"
BUILD = "tpl-dotnet-build.yml"
LANE = "tpl-dotnet-lane.yml"
CLASSIFY_PIN = "the classify job must be exactly the pinned job"
DECIDE_PIN = "the Decide step must be exactly the pinned gate invocation"
HYGIENE = "      - name: Hygiene\n        if: ${{ runner.environment == 'self-hosted' }}\n        env:\n          SIM_SURVIVORS: dotnet testhost vstest.console VBCSCompiler\n"
TRUSTED = "      - name: Checkout trusted\n        if: ${{ runner.environment == 'self-hosted' }}\n"
SHA = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
ENVIRONMENT = (
    "    environment: ${{ needs.route.outputs.target == 'onprem' && inputs.runner_label != '' && 'sim-cache-lab' || inputs.default_tip"
    " && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.sim_fault == '' && 'sim-cache-write' || 'sim-cache-read' }}\n"
)
BUILD_STEP = "      - name: Build\n        env:\n          FAIL_TEST: ${{ inputs.sim_fault == 'fail_test' && '1' || '0' }}\n"
BUILD_WITH = "tpl-dotnet-build.yml # zizmor: ignore[self-repository]\n    with:\n      pr_number: ${{ needs.validate.outputs.pr_number }}\n"
UNIT_WITH = "tpl-dotnet-lane.yml # zizmor: ignore[self-repository]\n    with:\n      pr_number: ${{ needs.validate.outputs.pr_number }}\n"
WITH_REST = (
    "      pr_branch: ${{ needs.validate.outputs.pr_branch }}\n      pr_author: ${{ needs.validate.outputs.pr_author }}\n"
    "      pr_sha: ${{ needs.validate.outputs.pr_sha }}\n      base_sha: ${{ needs.validate.outputs.base_sha }}\n"
    "      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n"
)
UNIT_GUARD = "    if: ${{ needs.validate.outputs.lane_unit == 'true' && needs.build.outputs.verdict == 'pass' }}\n"
GATE_NEEDS = "    needs:\n      - validate\n      - build\n      - unit\n      - integration\n"
TRIPWIRE = (
    "      - name: Cache tripwire\n        env:\n          SIM_TAILNET_JOIN: ${{ steps.join.outcome }}\n"
    "          SIM_CACHE_ENDPOINT_LAB: ${{ secrets.SIM_CACHE_ENDPOINT_LAB }}\n          SIM_CACHE_ENDPOINT_TAILNET: ${{ secrets.SIM_CACHE_ENDPOINT_TAILNET }}\n"
    "          SIM_CACHE_READ_KEY_ID: ${{ secrets.SIM_CACHE_READ_KEY_ID }}\n          SIM_CACHE_READ_SECRET: ${{ secrets.SIM_CACHE_READ_SECRET }}\n"
    '        run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS"\n'
)
BUILT = '      - name: Built\n        run: touch "$MARKERS/build"\n'
ZERO = "      - name: Zero tests\n        id: zero\n"
HEALTH_DOTNET = '          (cd "$SIM_ROOT" && dotnet --version)\n'
SAVE_GUARD = (
    "        if: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    " && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}\n"
)
FULL = (
    "          FULL: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    " && inputs.sim_fault == '' && runner.environment == 'github-hosted' && '1' || '0' }}\n"
)
SIM_FAULT = "      sim_fault: ${{ inputs.fault != 'none' && inputs.fault || '' }}\n"
READER = "          SIM_CACHE_READ_KEY_ID: ${{ secrets.SIM_CACHE_READ_KEY_ID }}\n"
WRITER = "          SIM_CACHE_WRITE_KEY_ID: ${{ secrets.SIM_CACHE_WRITE_KEY_ID }}\n"
ENDPOINT = "          SIM_CACHE_ENDPOINT_LAB: ${{ secrets.SIM_CACHE_ENDPOINT_LAB }}\n"
LANE_RUN_TOP = "    timeout-minutes: 25\n    env:\n"
DOWNLOAD_WITH = "          name: ${{ inputs.artifact }}\n          path: ${{ runner.temp }}/build\n"


def test_the_shipped_workflows_satisfy_the_policy():
    assert policy.violations(WORKFLOWS, root=ROOT) == []


MUTATIONS = {
    "token on a step of a work job": (
        BUILD,
        "      - name: Health\n        run: |",
        "      - name: Health\n        env:\n          SIM_ROUTE_TOKEN: ${{ secrets.SIM_ROUTE_TOKEN }}\n        run: |",
        "referenced outside the route job",
    ),
    "token passed to the called route workflow": (
        BUILD,
        "      runner_label: ${{ inputs.runner_label }}\n",
        "      runner_label: ${{ inputs.runner_label }}\n      token: ${{ secrets.SIM_ROUTE_TOKEN }}\n",
        "referenced outside the route job",
    ),
    "token in a second step of the route job": (
        ROUTE,
        "      - name: Set up Python\n        uses:",
        "      - name: Set up Python\n        env:\n          SIM_ROUTE_TOKEN: ${{ secrets.SIM_ROUTE_TOKEN }}\n        uses:",
        "must appear in exactly one step",
    ),
    "token in the route job env instead of a step": (
        ROUTE,
        "    environment: sim-route\n",
        "    environment: sim-route\n    env:\n      SIM_ROUTE_TOKEN: ${{ secrets.SIM_ROUTE_TOKEN }}\n",
        "used outside a step env",
    ),
    "token echoed inside a run script": (
        ROUTE,
        'target=$(python3 -m core.route "$LAB_LABEL")',
        'target=$(python3 -m core.route "$LAB_LABEL") ${{ secrets.SIM_ROUTE_TOKEN }}',
        "expression inside run",
    ),
    "route job on the lab label": (
        ROUTE,
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment: sim-route\n",
        "    runs-on: ${{ inputs.runner_label }}\n    timeout-minutes: 5\n    environment: sim-route\n",
        "route job must run on a GitHub-hosted ubuntu label",
    ),
    "route job on a self-hosted label": (
        ROUTE,
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    environment: sim-route\n",
        "    runs-on: self-hosted\n    timeout-minutes: 5\n    environment: sim-route\n",
        "route job must run on a GitHub-hosted ubuntu label",
    ),
    "route job without its environment": (ROUTE, "    environment: sim-route\n", "", "route job must use environment sim-route"),
    "route job without its environment so its secret would be empty": (
        ROUTE,
        "    environment: sim-route\n",
        "",
        "reads secrets without an environment",
    ),
    "route job without the fork guard": (
        ROUTE,
        "    if: ${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}\n",
        "",
        "route job has no fork guard",
    ),
    "fork guard that lets a fork through": (
        ROUTE,
        "    if: ${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}\n",
        "    if: ${{ github.event_name != 'never' }}\n",
        "route job runs for a fork pull request",
    ),
    "fork guard that blocks same-repository runs": (
        ROUTE,
        "    if: ${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}\n",
        "    if: ${{ github.event_name == 'never' }}\n",
        "route job is skipped for a same-repository run",
    ),
    "lab label input without the opaque warning": (
        ROUTE,
        "        description: Opaque runner label; it is printed unmasked in public logs, so never a host name\n",
        "        description: Runner label\n",
        "must say the label is public in logs",
    ),
    "build run always on the lab label": (
        BUILD,
        "    runs-on: ${{ needs.route.outputs.target == 'onprem' && inputs.runner_label || 'ubuntu-24.04' }}\n",
        "    runs-on: ${{ inputs.runner_label }}\n",
        "the run job runs on",
    ),
    "lane run on the lab label when routing was skipped": (
        LANE,
        "    runs-on: ${{ needs.route.outputs.target == 'onprem' && inputs.runner_label || 'ubuntu-24.04' }}\n",
        "    runs-on: ${{ needs.route.outputs.target != 'cloud' && inputs.runner_label || 'ubuntu-24.04' }}\n",
        "the run job runs on",
    ),
    "lane run skipped when the route is skipped": (
        LANE,
        "    needs: route\n    if: ${{ !cancelled() }}\n",
        "    needs: route\n",
        "must need route and run unless cancelled",
    ),
    "route call with another label": (
        BUILD,
        "      runner_label: ${{ inputs.runner_label }}\n",
        "      runner_label: ${{ inputs.pr_branch }}\n",
        "the route job must call tpl-route.yml",
    ),
    "both lanes on one lab label": (
        TOP,
        "      runner_label: ${{ vars.SIM_LAB_LABEL_UNIT }}\n",
        "      runner_label: ${{ vars.SIM_LAB_LABEL_INTEGRATION }}\n",
        "two jobs share one lab label expression",
    ),
    "unit lane on a shared label variable": (
        TOP,
        "      runner_label: ${{ vars.SIM_LAB_LABEL_UNIT }}\n",
        "      runner_label: ${{ vars.SIM_LAB_LABEL }}\n",
        "must use its own variable SIM_LAB_LABEL_UNIT",
    ),
    "the build on the unit label": (
        TOP,
        "      runner_label: ${{ vars.SIM_LAB_LABEL_BUILD }}\n",
        "      runner_label: ${{ vars.SIM_LAB_LABEL_UNIT }}\n",
        "must use its own variable SIM_LAB_LABEL_BUILD",
    ),
    "expression inside a run script": (
        BUILD,
        'run: sh adapters/dotnet/build_artifact.sh pack "$RUNNER_TEMP/out/build.tar"',
        'run: sh adapters/dotnet/build_artifact.sh pack "$RUNNER_TEMP/out/build.tar" ${{ inputs.pr_branch }}',
        "expression inside run",
    ),
    "action pinned to a tag": (
        LANE,
        "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
        "actions/download-artifact@v8",
        "action not pinned to a full SHA",
    ),
    "action pinned to a short sha": (
        BUILD,
        "tailscale/github-action@d1b6cd204f8dceda5b3eaad7f1f767be390056cd",
        "tailscale/github-action@d1b6cd2",
        "action not pinned to a full SHA",
    ),
    "build run timeout beyond the cap": (BUILD, "    timeout-minutes: 25\n    environment:", "    timeout-minutes: 31\n    environment:", "per-run cap"),
    "lane plus build beyond the budget": (LANE, LANE_RUN_TOP, "    timeout-minutes: 30\n    env:\n", "worst-case critical path"),
    "pull_request_target trigger": (TOP, "on:\n  workflow_dispatch:", "on:\n  pull_request_target:\n  workflow_dispatch:", "forbidden trigger pull_request_target"),
    "workflow_run trigger": (TOP, "on:\n  workflow_dispatch:", "on:\n  workflow_run:\n    workflows: [x]\n  workflow_dispatch:", "forbidden trigger workflow_run"),
    "issue_comment trigger": (TOP, "on:\n  workflow_dispatch:", "on:\n  issue_comment:\n  workflow_dispatch:", "forbidden trigger issue_comment"),
    "pull_request trigger on the entry workflow": (TOP, "on:\n  workflow_dispatch:", "on:\n  pull_request:\n  workflow_dispatch:", "must be workflow_dispatch only"),
    "a template with a push trigger": (ROUTE, "on:\n  workflow_call:", "on:\n  push:\n  workflow_call:", "tpl-route.yml: must be workflow_call only"),
    "the tailnet join failing the lane": (BUILD, "        continue-on-error: true\n", "", "must be continue-on-error: true"),
    "the tailnet join without its id": (
        BUILD,
        "      - name: Join the tailnet\n        id: join\n",
        "      - name: Join the tailnet\n",
        "must have id: join",
    ),
    "the tripwire step blind to the join outcome": (
        BUILD,
        "          SIM_TAILNET_JOIN: ${{ steps.join.outcome }}\n",
        "          SIM_TAILNET_JOIN: success\n",
        "must pass SIM_TAILNET_JOIN",
    ),
    "continue-on-error on a step other than the tailnet join": (
        BUILD,
        "      - name: Pack\n",
        "      - name: Pack\n        continue-on-error: true\n",
        "continue-on-error at jobs.run.steps",
    ),
    "the tailnet join without a step timeout": (BUILD, "        timeout-minutes: 3\n", "", "needs timeout-minutes of 5 or less"),
    "the tailnet join with a step timeout above the cap": (BUILD, "        timeout-minutes: 3\n", "        timeout-minutes: 30\n", "needs timeout-minutes of 5 or less"),
    "the tailnet join with an extra key": (
        BUILD,
        "        timeout-minutes: 3\n",
        "        timeout-minutes: 3\n        env:\n          X: y\n",
        "the tailnet join step must have exactly",
    ),
    "the plan never planning every project": (BUILD, FULL, '          FULL: "0"\n', "must plan every project exactly when the cache saves"),
    "the plan planning every project off the default branch tip": (
        BUILD,
        FULL,
        "          FULL: ${{ runner.environment == 'github-hosted' && '1' || '0' }}\n",
        "must plan every project exactly when the cache saves",
    ),
    "the plan ignoring FULL": (BUILD, 'if [ "$FULL" = 1 ]; then', 'if [ "$FULL" = 2 ]; then', "must pass --all when FULL is 1"),
    "the plan building one lane only": (BUILD, "--lane all --emit build", "--lane unit --emit build", "one build serves every lane"),
    "validate resolving with something else": (TOP, "        run: sh core/resolve_refs.sh\n", "        run: echo resolved\n", "validate must resolve every PR input"),
    "validate checking out the requested head instead of the dispatch commit": (
        TOP,
        "          fetch-depth: 0\n          persist-credentials: false\n",
        "          ref: ${{ inputs.pr_sha }}\n          fetch-depth: 0\n          persist-credentials: false\n",
        "validate must check out the dispatch commit",
    ),
    "validate without the full history to check branch containment": (
        TOP,
        "          fetch-depth: 0\n",
        "          fetch-depth: 1\n",
        "validate must check out the dispatch commit",
    ),
    "validate not publishing the resolved head": (TOP, "      pr_sha: ${{ steps.resolve.outputs.pr_sha }}\n", "", "validate must output"),
    "the unit lane given the unresolved head": (
        TOP,
        UNIT_WITH + WITH_REST,
        UNIT_WITH + WITH_REST.replace("${{ needs.validate.outputs.pr_sha }}", "${{ inputs.pr_sha }}"),
        "job unit reads an unresolved PR input",
    ),
    "the build given the unresolved base": (
        TOP,
        BUILD_WITH + WITH_REST,
        BUILD_WITH + WITH_REST.replace("${{ needs.validate.outputs.base_sha }}", "${{ inputs.base }}"),
        "job build reads an unresolved PR input",
    ),
    "a lane told it is the default tip without validate": (
        TOP,
        UNIT_WITH + WITH_REST,
        UNIT_WITH + WITH_REST.replace("${{ needs.validate.outputs.default_tip == 'true' }}", "${{ true }}"),
        "job unit must pass default_tip",
    ),
    "the build checking out another ref than the selection head": (
        BUILD,
        "          ref: ${{ inputs.pr_sha }}\n",
        "          ref: ${{ inputs.pr_branch }}\n",
        "tpl-dotnet-build.yml: the run job must check out inputs.pr_sha",
    ),
    "the lane checkout without a ref": (LANE, "          ref: ${{ inputs.pr_sha }}\n", "", "tpl-dotnet-lane.yml: the run job must check out inputs.pr_sha"),
    "the lane checking out the base": (
        LANE,
        "          ref: ${{ inputs.pr_sha }}\n",
        "          ref: ${{ inputs.base_sha }}\n",
        "tpl-dotnet-lane.yml: the run job must check out inputs.pr_sha",
    ),
    "affected selecting against another head than the checkout": (
        BUILD,
        "          HEAD_SHA: ${{ inputs.pr_sha }}\n",
        "          HEAD_SHA: ${{ inputs.pr_branch }}\n",
        "must use the checked-out head",
    ),
    "continue-on-error on a job": (TOP, "    timeout-minutes: 5\n    steps:\n      - name: Checkout\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n", "    timeout-minutes: 5\n    continue-on-error: true\n    steps:\n      - name: Checkout\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n", "continue-on-error at jobs.gate"),
    "secrets inherit": (TOP, "    uses: ./.github/workflows/tpl-dotnet-build.yml", "    secrets: inherit\n    uses: ./.github/workflows/tpl-dotnet-build.yml", "secrets: inherit"),
    "write permission": (TOP, "      checks: read\n      id-token: write\n", "      checks: write\n      id-token: write\n", "permission jobs.build.permissions.checks is write"),
    "workflow permissions widened": (BUILD, "permissions:\n  contents: read\n\njobs:", "permissions:\n  contents: write\n\njobs:", "workflow permissions must be contents: read only"),
    "secret-looking name read from vars": (TOP, "      runner_label: ${{ vars.SIM_LAB_LABEL_BUILD }}\n", "      runner_label: ${{ vars.SIM_TOKEN }}\n", "secret-looking name"),
    "dotnet job without setup-dotnet": (
        LANE,
        "      - name: Set up .NET\n        uses: actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0\n        with:\n          global-json-file: ${{ inputs.solution_root }}/global.json\n",
        "",
        "runs dotnet without a prior actions/setup-dotnet step",
    ),
    "setup-dotnet after the first dotnet command": (
        BUILD,
        "      - name: Set up .NET\n",
        "      - name: Early\n        run: dotnet --info\n      - name: Set up .NET\n",
        "runs dotnet without a prior actions/setup-dotnet step",
    ),
    "setup-dotnet with a floating version instead of global.json": (
        BUILD,
        "          global-json-file: ${{ inputs.solution_root }}/global.json\n",
        "          dotnet-version: 10.x\n",
        "setup-dotnet must use global-json-file",
    ),
    "classify without the reason file": (BUILD, ' --reason-file "$RUNNER_TEMP/reason.txt"', "", CLASSIFY_PIN),
    "classify without the run job result": (BUILD, ' --run-result "$RUN_RESULT"', "", CLASSIFY_PIN),
    "classify with the run job result from another source": (LANE, "          RUN_RESULT: ${{ needs.run.result }}\n", "          RUN_RESULT: success\n", CLASSIFY_PIN),
    "classify with a forced run result appended": (BUILD, "--done-marker build)", "--done-marker build --run-result success)", CLASSIFY_PIN),
    "classify with the run result overwritten in the script": (
        LANE,
        "          set -eu\n          verdict=$(python3 -m core.classify",
        "          set -eu\n          RUN_RESULT=success\n          verdict=$(python3 -m core.classify",
        CLASSIFY_PIN,
    ),
    "classify without the adapter setup step as infra": (LANE, ' --infra-step "Set up .NET"', "", CLASSIFY_PIN),
    "classify with the build step named as infra": (BUILD, '--infra-step "Upload build"', '--infra-step "Build"', CLASSIFY_PIN),
    "build classify without the build done marker": (BUILD, " --done-marker build", "", CLASSIFY_PIN),
    "lane classify without the download as infra": (LANE, ' --infra-step "Download build"', "", CLASSIFY_PIN),
    "a step inserted before Classify": (
        LANE,
        "      - name: Classify\n",
        '      - name: Path\n        run: echo /tmp/x >> "$GITHUB_PATH"\n      - name: Classify\n',
        CLASSIFY_PIN,
    ),
    "verdict pass hardcoded in the classify job outputs": (BUILD, "      verdict: ${{ steps.verdict.outputs.verdict }}\n", "      verdict: pass\n", CLASSIFY_PIN),
    "verdict pass hardcoded in the workflow outputs": (
        LANE,
        "        value: ${{ jobs.classify.outputs.verdict }}\n",
        "        value: pass\n",
        "tpl-dotnet-lane.yml: the workflow outputs must be exactly",
    ),
    "the build artifact output fixed in the workflow outputs": (
        BUILD,
        "        value: ${{ jobs.run.outputs.artifact }}\n",
        "        value: dotnet-build-1\n",
        "tpl-dotnet-build.yml: the workflow outputs must be exactly",
    ),
    "the executed count not taken from the guard": (
        LANE,
        "      executed: ${{ steps.zero.outputs.executed }}\n",
        '      executed: "9"\n',
        "the run job outputs must be exactly",
    ),
    "gate without the unit reason": (TOP, ' --reason "unit=${UNIT_REASON}"', "", DECIDE_PIN),
    "gate passing when validate selected nothing": (TOP, '*) echo "gate: red [test]: validate did not select lanes"; exit 1 ;;', "*) ;;", DECIDE_PIN),
    "gate without the build lane verdict": (TOP, '--lane "build=${BUILD_VERDICT}"', '--lane "build=pass"', DECIDE_PIN),
    "gate without the retest flag": (TOP, '    true) set -- "$@" --retest ;;', "    true) ;;", DECIDE_PIN),
    "gate without the verdict file": (TOP, ' --verdict-file "$RUNNER_TEMP/verdict/verdict.json"', "", DECIDE_PIN),
    "gate taking the executed count of another lane": (TOP, '--executed "unit=${UNIT_EXECUTED}"', '--executed "unit=${INTEGRATION_EXECUTED}"', DECIDE_PIN),
    "gate env with an extra variable": (TOP, "          STACK: ${{ inputs.stack }}\n", "          STACK: ${{ inputs.stack }}\n          BASH_ENV: /tmp/x\n", DECIDE_PIN),
    "gate reading the build verdict from elsewhere": (TOP, "          BUILD_VERDICT: ${{ needs.build.outputs.verdict }}\n", "          BUILD_VERDICT: pass\n", DECIDE_PIN),
    "gate not waiting for validate": (TOP, GATE_NEEDS, GATE_NEEDS.replace("      - validate\n", ""), "the gate must need validate, build and every lane"),
    "gate not waiting for the build": (TOP, GATE_NEEDS, GATE_NEEDS.replace("      - build\n", ""), "the gate must need validate, build and every lane"),
    "the verdict artifact under another name": (TOP, "          name: sim-gate-verdict\n", "          name: verdict\n", "the pinned Upload verdict"),
    "the verdict upload only on success": (TOP, "      - name: Upload verdict\n        if: ${{ always() }}\n", "      - name: Upload verdict\n", "the pinned Upload verdict"),
    "a lane job without the validate selection guard": (
        TOP,
        UNIT_GUARD,
        "    if: ${{ needs.build.outputs.verdict == 'pass' }}\n",
        "lane unit runs=True for selection 'false'",
    ),
    "a lane job guarded on another lane's selection": (TOP, UNIT_GUARD, UNIT_GUARD.replace("lane_unit", "lane_integration"), "lane unit runs="),
    "a lane job that runs after a failed build": (
        TOP,
        UNIT_GUARD,
        "    if: ${{ needs.validate.outputs.lane_unit == 'true' }}\n",
        "lane unit runs=True for selection 'true' and build verdict 'test'",
    ),
    "a lane job that does not need the build": (
        TOP,
        "    needs:\n      - validate\n      - build\n" + UNIT_GUARD,
        "    needs:\n      - validate\n" + UNIT_GUARD,
        "lane unit must need the build job",
    ),
    "a lane job taking another artifact": (
        TOP,
        "      artifact: ${{ needs.build.outputs.artifact }}\n",
        "      artifact: dotnet-build-1\n",
        "must take artifact from needs.build.outputs.artifact",
    ),
    "a lane job the validate lane list does not know": (TOP, "          KNOWN_LANES: unit integration\n", "          KNOWN_LANES: unit\n", "the lane names differ"),
    "a validate lane output with no lane job": (
        TOP,
        "      lane_integration: ${{ steps.resolve.outputs.lane_integration }}\n",
        "      lane_integration: ${{ steps.resolve.outputs.lane_integration }}\n      lane_extra: ${{ steps.resolve.outputs.lane_extra }}\n",
        "the lane names differ",
    ),
    "the entry with an eleventh input": (
        TOP,
        "      retest_of:\n        description: Run id",
        "      extra:\n        required: false\n        type: string\n      retest_of:\n        description: Run id",
        "dispatch inputs are",
    ),
    "the entry without the pr_author input": (
        TOP,
        '      pr_author:\n        description: Login of the pull request author, or empty\n        required: false\n        default: ""\n        type: string\n',
        "",
        "dispatch inputs are",
    ),
    "pr_branch defaulting to another branch": (TOP, "        default: main\n", "        default: develop\n", "input pr_branch has default"),
    "cache as a free string": (TOP, '        default: "use"\n        type: choice\n', '        default: "use"\n        type: string\n', "input cache has type"),
    "a fault option no job reads": (TOP, "          - kill_both\n", "          - kill_both\n          - fail_build\n", "fault option fail_build is not referenced by any job"),
    "the build ignoring the fail_test fault": (TOP, SIM_FAULT, "      sim_fault: ''\n", "job build maps fault fail_test"),
    "a lane mapping none to a fault": (
        TOP,
        SIM_FAULT + "      lane: unit\n",
        "      sim_fault: ${{ inputs.fault }}\n      lane: unit\n",
        "job unit maps fault none to sim_fault 'none'",
    ),
    "the build injecting the failing test for any fault": (
        BUILD,
        BUILD_STEP,
        BUILD_STEP.replace("inputs.sim_fault == 'fail_test'", "inputs.sim_fault != ''"),
        "injects the failing test for fault 'kill_both'",
    ),
    "the lane killing on the onprem-only fault everywhere": (
        LANE,
        "inputs.sim_fault == 'kill_onprem' && needs.route.outputs.target == 'onprem' }}",
        "inputs.sim_fault == 'kill_onprem' }}",
        "kills the runner=True for fault 'kill_onprem' on target 'cloud'",
    ),
    "a lane dropping the cache input": (TOP, "      cache: ${{ inputs.cache }}\n", "      cache: use\n", "must pass cache"),
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
    "the run name hiding a partial lane selection": (TOP, "inputs.lanes != '' && format(' lanes {0}', inputs.lanes) || ''", "''", "must name the selected lanes unit"),
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
    "the retest stand-in on a push trigger": (RETEST, "on:\n  workflow_dispatch:", "on:\n  push:\n  workflow_dispatch:", "sim-retest.yml: must be workflow_dispatch only"),
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
        "    if: ${{ always() }}\n    runs-on: ubuntu-24.04\n    permissions:\n      contents: read\n      actions: read\n",
        "sim-gate.yml: job gate permissions are",
    ),
    "the classify job without checks read": (
        BUILD,
        "      actions: read\n      checks: read\n    outputs:\n      verdict:",
        "      actions: read\n    outputs:\n      verdict:",
        "tpl-dotnet-build.yml: job classify permissions are",
    ),
    "id-token write on the classify job": (
        LANE,
        "      actions: read\n      checks: read\n    outputs:\n      verdict:",
        "      actions: read\n      checks: read\n      id-token: write\n    outputs:\n      verdict:",
        "permission jobs.classify.permissions.id-token is write",
    ),
    "id-token write on the route job": (
        ROUTE,
        "    environment: sim-route\n",
        "    environment: sim-route\n    permissions:\n      contents: read\n      id-token: write\n",
        "permission jobs.route.permissions.id-token is write",
    ),
    "id-token write for the whole workflow": (
        BUILD,
        "permissions:\n  contents: read\n\njobs:",
        "permissions:\n  contents: read\n  id-token: write\n\njobs:",
        "workflow permissions must be contents: read only",
    ),
    "the build run job without id-token write so workload identity cannot work": (
        BUILD,
        "    permissions:\n      contents: read\n      id-token: write\n    env:\n",
        "    permissions:\n      contents: read\n    env:\n",
        "tpl-dotnet-build.yml: job run permissions are",
    ),
    "id-token write on a lane caller": (
        TOP,
        "      checks: read\n    uses: ./.github/workflows/tpl-dotnet-lane.yml",
        "      checks: read\n      id-token: write\n    uses: ./.github/workflows/tpl-dotnet-lane.yml",
        "permission jobs.unit.permissions.id-token is write",
    ),
    "id-token write on the lane run job": (
        LANE,
        LANE_RUN_TOP,
        "    timeout-minutes: 25\n    permissions:\n      contents: read\n      id-token: write\n    env:\n",
        "permission jobs.run.permissions.id-token is write",
    ),
    "a secret read in the lane Test step env": (
        LANE,
        "          PARALLEL: ${{ inputs.parallel }}\n",
        "          PARALLEL: ${{ inputs.parallel }}\n" + READER,
        "secret SIM_CACHE_READ_KEY_ID is read at a place that is not allowed",
    ),
    "a lane run job with an environment": (
        LANE,
        LANE_RUN_TOP,
        "    timeout-minutes: 25\n    environment: sim-cache-read\n    env:\n",
        "the lane run job must have no environment",
    ),
    "a reader key in the Build step env": (BUILD, BUILD_STEP, BUILD_STEP + READER, "the Build step must hold no secret"),
    "an endpoint in the Build step env": (BUILD, BUILD_STEP, BUILD_STEP + ENDPOINT, "secret SIM_CACHE_ENDPOINT_LAB is read at a place that is not allowed"),
    "a reader key in the Health step": (
        BUILD,
        "      - name: Health\n        run: |",
        "      - name: Health\n        env:\n" + READER + "        run: |",
        "secret SIM_CACHE_READ_KEY_ID is read at a place that is not allowed",
    ),
    "a writer key in the restore step": (
        BUILD,
        "          SIM_CACHE_MODE: ${{ inputs.cache }}\n",
        "          SIM_CACHE_MODE: ${{ inputs.cache }}\n" + WRITER,
        "secret SIM_CACHE_WRITE_KEY_ID is read at a place that is not allowed",
    ),
    "a reader key in the save step": (
        BUILD,
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n",
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n" + READER,
        "secret SIM_CACHE_READ_KEY_ID is read at a place that is not allowed",
    ),
    "cache endpoint read in the route job": (
        ROUTE,
        "          LAB_LABEL: ${{ inputs.runner_label }}\n",
        "          LAB_LABEL: ${{ inputs.runner_label }}\n" + ENDPOINT,
        "secret SIM_CACHE_ENDPOINT_LAB is read at a place that is not allowed",
    ),
    "cache endpoint read in the classify job": (
        BUILD,
        "          RUN_RESULT: ${{ needs.run.result }}\n",
        "          RUN_RESULT: ${{ needs.run.result }}\n" + ENDPOINT,
        "secret SIM_CACHE_ENDPOINT_LAB is read at a place that is not allowed",
    ),
    "cache endpoint taken from a repository variable": (
        BUILD,
        ENDPOINT,
        "          SIM_CACHE_ENDPOINT_LAB: ${{ vars.SIM_CACHE_ENDPOINT_LAB }}\n",
        "secret-looking name",
    ),
    "reader key read in the route job": (
        ROUTE,
        "          LAB_LABEL: ${{ inputs.runner_label }}\n",
        "          LAB_LABEL: ${{ inputs.runner_label }}\n" + READER,
        "secret SIM_CACHE_READ_KEY_ID is read at a place that is not allowed",
    ),
    "tailnet join without a hosted-only guard": (
        BUILD,
        "      - name: Join the tailnet\n        id: join\n        if: ${{ runner.environment == 'github-hosted' }}\n",
        "      - name: Join the tailnet\n        id: join\n",
        "tailnet credentials may be used only by the tailscale action on a github-hosted runner",
    ),
    "tailnet join on self-hosted runners": (
        BUILD,
        "      - name: Join the tailnet\n        id: join\n        if: ${{ runner.environment == 'github-hosted' }}\n",
        "      - name: Join the tailnet\n        id: join\n        if: ${{ runner.environment == 'self-hosted' }}\n",
        "tailnet credentials may be used only by the tailscale action on a github-hosted runner",
    ),
    "tailnet client id read by another step": (
        BUILD,
        BUILD_STEP,
        BUILD_STEP + "          SIM_TAILNET_CLIENT_ID: ${{ secrets.SIM_TAILNET_CLIENT_ID }}\n",
        "tailnet credentials may be used only by the tailscale action",
    ),
    "tailnet audience read in the route job": (
        ROUTE,
        "          LAB_LABEL: ${{ inputs.runner_label }}\n",
        "          LAB_LABEL: ${{ inputs.runner_label }}\n          T: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "secret SIM_TAILNET_AUDIENCE is read at a place that is not allowed",
    ),
    "tailnet credential given to another action input": (
        BUILD,
        "          audience: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "          audience: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n          ping: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n",
        "secret SIM_TAILNET_AUDIENCE is read at a place that is not allowed",
    ),
    "tailscale with a stored auth key": (
        BUILD,
        "          tags: tag:ci-sim\n",
        "          tags: tag:ci-sim\n          authkey: ${{ secrets.SIM_TAILNET_AUTHKEY }}\n",
        "workload identity, not a stored auth key",
    ),
    "tailscale with a stored OAuth secret": (
        BUILD,
        "          tags: tag:ci-sim\n",
        "          tags: tag:ci-sim\n          oauth-secret: ${{ secrets.SIM_TAILNET_OAUTH_SECRET }}\n",
        "workload identity, not a stored auth key",
    ),
    "tailscale with other tags": (BUILD, "          tags: tag:ci-sim\n", "          tags: tag:ci-sim,tag:lab-cache\n", "must join with tags: tag:ci-sim"),
    "tailscale without tags": (BUILD, "          tags: tag:ci-sim\n", "", "must join with tags: tag:ci-sim"),
    "tailscale accepting routes": (BUILD, "          args: --accept-routes=false\n", "", "--accept-routes=false"),
    "tailscale using its binary cache": (BUILD, '          use-cache: "false"\n', '          use-cache: "true"\n', "must not use its binary cache"),
    "tailscale without the audience": (BUILD, "          audience: ${{ secrets.SIM_TAILNET_AUDIENCE }}\n", "", "needs oauth-client-id and audience"),
    "hygiene step deleted": (BUILD, "      - name: Hygiene\n", "      - name: Cleanup\n", "has no Hygiene step"),
    "hygiene step only on hosted runners": (BUILD, HYGIENE, HYGIENE.replace("'self-hosted'", "'github-hosted'"), "Hygiene step must run on self-hosted runners only"),
    "hygiene step without a guard": (
        LANE,
        HYGIENE,
        HYGIENE.replace("        if: ${{ runner.environment == 'self-hosted' }}\n", ""),
        "Hygiene step must run on self-hosted runners only",
    ),
    "hygiene step running the head tree copy": (
        LANE,
        "              sh .sim-trusted/core/prejob.sh || status=$?\n",
        "              sh core/prejob.sh || status=$?\n",
        "must run the trusted copy exactly",
    ),
    "hygiene step keeping the trusted copy in the workspace": (BUILD, "          rm -rf .sim-trusted\n", "", "must run the trusted copy exactly"),
    "hygiene step skipping the dispatch commit check": (
        BUILD,
        ' || [ "$(git -C .sim-trusted rev-parse HEAD)" != "$GITHUB_SHA" ]',
        "",
        "must run the trusted copy exactly",
    ),
    "hygiene step trusting a directory that is not its own checkout": (LANE, " || [ ! -d .sim-trusted/.git ]", "", "must run the trusted copy exactly"),
    "hygiene step with core survivor names gone": (
        BUILD,
        "          SIM_SURVIVORS: dotnet testhost vstest.console VBCSCompiler\n",
        "          SIM_SURVIVORS: node\n",
        "must pass exactly SIM_SURVIVORS",
    ),
    "hygiene step with a shell key": (BUILD, HYGIENE, HYGIENE + "        shell: bash\n", "the Hygiene step must have exactly name, if, env and run"),
    "the trusted checkout pinned to the head": (
        BUILD,
        "          path: .sim-trusted\n",
        "          ref: ${{ inputs.pr_sha }}\n          path: .sim-trusted\n",
        "the trusted checkout must not take a ref",
    ),
    "the trusted checkout keeping credentials": (
        LANE,
        "          path: .sim-trusted\n          persist-credentials: false\n",
        "          path: .sim-trusted\n          persist-credentials: true\n",
        "the trusted checkout must be actions/checkout with exactly",
    ),
    "the trusted checkout of the whole tree": (BUILD, "          sparse-checkout: core\n", "", "the trusted checkout must be actions/checkout with exactly"),
    "the trusted checkout deleted": (LANE, "      - name: Checkout trusted\n", "      - name: Checkout spare\n", "has no Checkout trusted step"),
    "the trusted checkout on hosted runners too": (BUILD, TRUSTED, "      - name: Checkout trusted\n", "the trusted checkout must run on self-hosted runners only"),
    "a step between the trusted checkout and Hygiene": (
        LANE,
        "      - name: Hygiene\n",
        "      - name: Copy\n        run: cp core/prejob.sh .sim-trusted/core/prejob.sh\n      - name: Hygiene\n",
        "Hygiene must follow the trusted checkout directly",
    ),
    "dotnet test that rebuilds": (BUILD, HEALTH_DOTNET, '          (cd "$SIM_ROOT" && dotnet --version && dotnet test x.csproj)\n', "dotnet test without --no-build"),
    "a raw dotnet build that bypasses the cache": (BUILD, HEALTH_DOTNET, '          (cd "$SIM_ROOT" && dotnet build x.csproj)\n', "dotnet build outside cache_build.sh"),
    "the build step not going through the cache script": (
        BUILD,
        'sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/build.txt"',
        'sh adapters/dotnet/other.sh build "$RUNNER_TEMP/build.txt"',
        "the Build step must run exactly the pinned script",
    ),
    "the build step calling the cache script at a stale path": (
        BUILD,
        'sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/build.txt"',
        'sh tools/sim_gate/cache_build.sh build "$RUNNER_TEMP/build.txt"',
        "the Build step must run exactly the pinned script",
    ),
    "the build step with a command appended": (
        BUILD,
        '"$RUNNER_TEMP/hits.txt" "$RUNNER_TEMP/built.txt"\n',
        '"$RUNNER_TEMP/hits.txt" "$RUNNER_TEMP/built.txt" && env\n',
        "the Build step must run exactly the pinned script",
    ),
    "the cache tripwire step removed": (BUILD, TRIPWIRE, "", "the cache tripwire step must come before the Build step"),
    "the cache tripwire step running something else": (
        BUILD,
        '        run: sh adapters/dotnet/cache_build.sh tripwire "$MARKERS"\n',
        "        run: echo clear\n",
        "the cache tripwire step must run exactly",
    ),
    "the cache tripwire step with a command appended": (
        BUILD,
        'cache_build.sh tripwire "$MARKERS"',
        'cache_build.sh tripwire "$MARKERS" || true',
        "the cache tripwire step must run exactly",
    ),
    "the cache tripwire step before the plan": (BUILD, TRIPWIRE + "      - name: Restore cache\n", "      - name: Restore cache\n", "the cache tripwire step must come before the Build step"),
    "the restore step without the fault switch": (
        BUILD,
        "      - name: Restore cache\n        env:\n          FAIL_TEST: ${{ inputs.sim_fault == 'fail_test' && '1' || '0' }}\n",
        "      - name: Restore cache\n        env:\n          FAIL_TEST: \"0\"\n",
        "the Restore cache step must be exactly the pinned step",
    ),
    "the restore step running the build": (
        BUILD,
        'sh adapters/dotnet/cache_build.sh restore "$RUNNER_TEMP/build.txt" "$RUNNER_TEMP/hits.txt"',
        'sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/build.txt" "$RUNNER_TEMP/hits.txt" "$RUNNER_TEMP/built.txt"',
        "the Restore cache step must be exactly the pinned step",
    ),
    "the save step running something else": (
        BUILD,
        'sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        'sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/other.txt"',
        "must run exactly sh adapters/dotnet/cache_build.sh save",
    ),
    "the save step calling the cache script at a stale path": (
        BUILD,
        'sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        'sh tools/sim_gate/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        "must run exactly sh adapters/dotnet/cache_build.sh save",
    ),
    "the save step with a command appended": (
        BUILD,
        'sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        'sh adapters/dotnet/cache_build.sh save "$RUNNER_TEMP/built.txt" && env',
        "must run exactly sh adapters/dotnet/cache_build.sh save",
    ),
    "the save step without the default branch name for the tip check": (
        BUILD,
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n",
        "",
        "the save step env must be exactly",
    ),
    "the save step taking the tip name from an input": (
        BUILD,
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n",
        "          SIM_DEFAULT_BRANCH: ${{ inputs.pr_branch }}\n",
        "the save step env must be exactly",
    ),
    "a shell key on the writer step": (BUILD, "      - name: Save cache\n", "      - name: Save cache\n        shell: sh -e {0}\n", "must be named Save cache and have exactly name, if, env and run"),
    "BASH_ENV on the writer step": (
        BUILD,
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n",
        "          SIM_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n          BASH_ENV: /tmp/x\n",
        "the save step env must be exactly",
    ),
    "defaults at the workflow level": (
        BUILD,
        "permissions:\n  contents: read\n\njobs:",
        "permissions:\n  contents: read\n\ndefaults:\n  run:\n    shell: bash\n\njobs:",
        "defaults change what a pinned step runs",
    ),
    "the built marker before the upload": (
        BUILD,
        "      - name: Upload build\n",
        BUILT + "      - name: Upload build\n",
        "tpl-dotnet-build.yml: the run job steps are",
    ),
    "the build upload under a fixed name": (
        BUILD,
        "          name: dotnet-build-${{ github.run_attempt }}\n",
        "          name: dotnet-build\n",
        "the Upload build step must be exactly the pinned step",
    ),
    "the build upload keeping the artifact for a month": (
        BUILD,
        "          if-no-files-found: error\n          retention-days: 1\n",
        "          if-no-files-found: error\n          retention-days: 30\n",
        "the Upload build step must be exactly the pinned step",
    ),
    "the build run job naming its artifact differently from the upload": (
        BUILD,
        "      artifact: dotnet-build-${{ github.run_attempt }}\n",
        "      artifact: dotnet-build-x\n",
        "the run job outputs must be exactly",
    ),
    "the build run job under another name": (BUILD, "    name: run dotnet-build\n", "    name: run build\n", "the run job must be named"),
    "the build job always writing": (BUILD, ENVIRONMENT, "    environment: sim-cache-write\n", "the run environment is 'sim-cache-write'"),
    "the build job never writing": (BUILD, ENVIRONMENT, "    environment: sim-cache-read\n", "the run environment is 'sim-cache-read'"),
    "the build job without an environment": (BUILD, ENVIRONMENT, "", "must choose its environment with an expression"),
    "the build environment taken from the ref name so a tag named like the default branch writes": (
        BUILD,
        ENVIRONMENT,
        ENVIRONMENT.replace("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", "github.ref_name == github.event.repository.default_branch"),
        "github.ref_name is refused",
    ),
    "the build environment writing for any head": (BUILD, ENVIRONMENT, ENVIRONMENT.replace("inputs.default_tip\n", "").replace("|| inputs.default_tip && ", "|| "), "wanted 'sim-cache-read'"),
    "the build environment writing with a fault on": (BUILD, ENVIRONMENT, ENVIRONMENT.replace(" && inputs.sim_fault == ''", ""), "fault 'fail_test'; wanted 'sim-cache-read'"),
    "the build environment writing from a lab runner": (
        BUILD,
        ENVIRONMENT,
        "    environment: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.sim_fault == '' && 'sim-cache-write'"
        " || needs.route.outputs.target == 'onprem' && inputs.runner_label != '' && 'sim-cache-lab' || 'sim-cache-read' }}\n",
        "wanted 'sim-cache-lab'",
    ),
    "the build environment writing from a topic branch": (
        BUILD,
        ENVIRONMENT,
        ENVIRONMENT.replace("github.ref == format('refs/heads/{0}'", "github.ref != format('refs/heads/{0}'"),
        "ref refs/heads/topic, default_tip True, fault ''; wanted 'sim-cache-read'",
    ),
    "the build environment comparing the head by name, where a tag MAIN matches main": (
        BUILD,
        ENVIRONMENT,
        ENVIRONMENT.replace("|| inputs.default_tip && ", "|| inputs.pr_sha == github.event.repository.default_branch && "),
        "must not compare the PR head by name",
    ),
    "the build environment giving a lab runner the hosted reader environment": (
        BUILD,
        ENVIRONMENT,
        ENVIRONMENT.replace("'sim-cache-lab'", "'sim-cache-read'"),
        "wanted 'sim-cache-lab'",
    ),
    "the build environment calling an onprem target without a label a lab runner": (
        BUILD,
        ENVIRONMENT,
        ENVIRONMENT.replace(" && inputs.runner_label != ''", ""),
        "for target 'onprem', label ''",
    ),
    "a ref name read anywhere because a tag can carry the default branch name": (
        TOP,
        "      cache: ${{ inputs.cache }}\n",
        "      cache: ${{ github.ref_name }}\n",
        "github.ref_name is refused",
    ),
    "a Build step in the lane": (
        LANE,
        "      - name: Unpack\n",
        "      - name: Build\n        run: echo build\n      - name: Unpack\n",
        "the lane has a step named Build",
    ),
    "a Plan step in the lane": (
        LANE,
        "      - name: Set up .NET\n",
        '      - name: Plan\n        run: python3 -m adapters.dotnet.plan --all --lane unit --emit test > "$RUNNER_TEMP/x.txt"\n      - name: Set up .NET\n',
        "the lane has a step named Plan",
    ),
    "an Affected step in the lane": (
        LANE,
        "      - name: Set up .NET\n",
        "      - name: Affected\n        run: echo affected\n      - name: Set up .NET\n",
        "the lane has a step named Affected",
    ),
    "the cache script hidden in the lane Test step": (
        LANE,
        "          status=0\n          sh adapters/dotnet/test_lane.sh",
        '          status=0\n          sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/a" "$RUNNER_TEMP/b" "$RUNNER_TEMP/c"\n          sh adapters/dotnet/test_lane.sh',
        "the lane builds or selects at",
    ),
    "the affected selection hidden in the lane": (
        LANE,
        "          status=0\n          sh adapters/dotnet/test_lane.sh",
        "          status=0\n          python3 -m adapters.dotnet.affected --base a --head b\n          sh adapters/dotnet/test_lane.sh",
        "the lane builds or selects at",
    ),
    "dotnet build in the lane": (LANE, HEALTH_DOTNET, '          (cd "$SIM_ROOT" && dotnet build x.csproj)\n', "the lane runs dotnet other than --version"),
    "dotnet msbuild in the lane": (LANE, HEALTH_DOTNET, '          (cd "$SIM_ROOT" && dotnet --version && dotnet msbuild x.csproj)\n', "the lane builds or selects at"),
    "dotnet restore in the lane": (LANE, HEALTH_DOTNET, '          (cd "$SIM_ROOT" && dotnet --version && dotnet restore)\n', "the lane builds or selects at"),
    "dotnet test without --no-build in the lane": (LANE, HEALTH_DOTNET, '          (cd "$SIM_ROOT" && dotnet --version && dotnet test x.csproj)\n', "dotnet test without --no-build"),
    "the lane downloading another run's artifact": (
        LANE,
        DOWNLOAD_WITH,
        DOWNLOAD_WITH + '          run-id: "1"\n          github-token: ${{ github.token }}\n',
        "must download inputs.artifact of this run",
    ),
    "the lane downloading a fixed artifact name": (
        LANE,
        DOWNLOAD_WITH,
        "          name: dotnet-build-1\n          path: ${{ runner.temp }}/build\n",
        "must download inputs.artifact of this run",
    ),
    "the lane downloading into the workspace": (LANE, DOWNLOAD_WITH, "          name: ${{ inputs.artifact }}\n          path: build\n", "must download inputs.artifact of this run"),
    "a step between the download and the unpack": (
        LANE,
        "      - name: Unpack\n",
        "      - name: Look\n        run: ls\n      - name: Unpack\n",
        "the Unpack step must follow the Download build step directly",
    ),
    "a head-tree script before the download": (
        LANE,
        "      - name: Download build\n",
        "      - name: Early\n        run: sh adapters/dotnet/test_lane.sh a b c 1\n      - name: Download build\n",
        "Unpack must be the first step that runs anything from the head tree",
    ),
    "a second job calling the build template": (
        TOP,
        "tpl-dotnet-lane.yml # zizmor: ignore[self-repository]\n    with:\n      pr_number: ${{ needs.validate.outputs.pr_number }}\n" + WITH_REST + "      runner_label: ${{ vars.SIM_LAB_LABEL_INTEGRATION }}",
        "tpl-dotnet-build.yml # zizmor: ignore[self-repository]\n    with:\n      pr_number: ${{ needs.validate.outputs.pr_number }}\n" + WITH_REST + "      runner_label: ${{ vars.SIM_LAB_LABEL_INTEGRATION }}",
        "exactly one job, sim-gate.yml build, may call tpl-dotnet-build.yml",
    ),
    "the zero-tests guard behind an if": (LANE, ZERO, ZERO + "        if: ${{ always() }}\n", "the Zero tests step must run with no if and no continue-on-error"),
    "the zero-tests guard allowed to fail": (LANE, ZERO, ZERO + "        continue-on-error: true\n", "the Zero tests step must run with no if and no continue-on-error"),
    "the zero-tests guard removed": (LANE, "      - name: Zero tests\n", "      - name: Count\n", "the Zero tests step must run with no if and no continue-on-error"),
    "the zero-tests guard reading another list": (
        LANE,
        'python3 -m core.zero_tests --list "$RUNNER_TEMP/build/$LANE.txt"',
        'python3 -m core.zero_tests --list "$RUNNER_TEMP/empty.txt"',
        "the Zero tests step must be exactly the pinned step",
    ),
    "the lane test step ignoring a failed project": (LANE, '"$filter" "$PARALLEL" || status=1\n', '"$filter" "$PARALLEL" || true\n', "the Test step must be exactly the pinned step"),
    "the unpack step running something else": (LANE, 'sh adapters/dotnet/build_artifact.sh unpack "$RUNNER_TEMP/build/build.tar"', "tar -xf build.tar", "the Unpack step must be exactly the pinned step"),
    "a step between Hygiene and the download": (
        LANE,
        "      - name: Download build\n",
        "      - name: Look\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n      - name: Download build\n",
        "the Download build step must follow Hygiene directly",
    ),
    "the affected selection against another base": (
        BUILD,
        "          BASE_SHA: ${{ inputs.base_sha }}\n",
        "          BASE_SHA: ${{ inputs.pr_sha }}\n",
        "the affected selection must use inputs.base_sha",
    ),
    "the route target hardcoded in the route outputs": (
        ROUTE,
        "        value: ${{ jobs.route.outputs.target }}\n",
        "        value: onprem\n",
        "tpl-route.yml: the workflow outputs must be exactly",
    ),
    "the build classify job with an environment": (
        BUILD,
        "    name: classify dotnet-build\n",
        "    name: classify dotnet-build\n    environment: sim-cache-read\n",
        "job classify must have no environment",
    ),
    "the gate skipped when a lane fails": (TOP, "    if: ${{ always() }}\n    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n", "the gate must run always"),
    "the build not waiting for validate": (
        TOP,
        "    name: build\n    needs: validate\n",
        "    name: build\n    needs:\n      - validate\n      - gate\n",
        "the build job must need validate",
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


CALLER = "    uses: ./.github/workflows/tpl-dotnet-build.yml # zizmor: ignore[self-repository]\n    with:\n"


def test_a_secret_passed_into_a_template_is_refused(copy):
    mutate(copy, TOP, CALLER, CALLER.replace("    with:", "    secrets:\n      OTHER_SECRET: ${{ secrets.OTHER_SECRET }}\n    with:"))
    found = policy.violations(copy)
    assert any("no secrets mapping or declaration is allowed" in line for line in found), found
    assert any("secret OTHER_SECRET is read" in line for line in found), found


def test_the_reader_key_passed_to_the_build_template_through_secrets_is_refused(copy):
    mutate(copy, TOP, CALLER, CALLER.replace("    with:", "    secrets:\n" + READER.replace("          ", "      ") + "    with:"))
    found = policy.violations(copy)
    assert any("no secrets mapping or declaration is allowed" in line for line in found), found
    assert any("SIM_CACHE_READ_KEY_ID is read at a place that is not allowed" in line for line in found), found


def test_the_reader_key_under_a_different_name_or_expression_is_refused(copy):
    mutate(copy, BUILD, READER, "          SIM_CACHE_READ_KEY_ID: ${{ secrets.SIM_CACHE_WRITE_KEY_ID }}\n")
    assert any("not allowed" in line for line in policy.violations(copy))


def test_the_writer_key_in_a_step_env_of_an_unguarded_step_is_refused(copy):
    mutate(copy, BUILD, BUILD_STEP, BUILD_STEP + "          SIM_CACHE_WRITE_KEY: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n")
    assert any("SIM_CACHE_WRITE" in line for line in policy.violations(copy))


def test_a_workflow_call_secrets_declaration_is_refused(copy):
    mutate(copy, BUILD, "on:\n  workflow_call:\n    inputs:", "on:\n  workflow_call:\n    secrets:\n      OTHER_SECRET:\n        required: false\n    inputs:")
    assert any("no secrets mapping or declaration is allowed" in line for line in policy.violations(copy))


@pytest.mark.parametrize("name", [BUILD, LANE, ROUTE])
def test_a_missing_workflow_is_refused(copy, name):
    (copy / name).unlink()
    assert any("workflow set is" in line for line in policy.violations(copy))


@pytest.mark.parametrize("name", ["sim-gate-lane.yml", "sim-gate-attempt.yml"])
def test_the_old_lane_workflows_are_gone_and_refused_if_they_return(copy, name):
    assert not (WORKFLOWS / name).exists()
    (copy / name).write_text((WORKFLOWS / LANE).read_text(encoding="utf-8"), encoding="utf-8")
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


def with_save_guard(copy, guard):
    mutate(copy, BUILD, SAVE_GUARD, "" if guard is None else f"        if: {guard}\n")


def test_the_bare_form_of_the_save_guard_is_accepted_too(copy):
    with_save_guard(
        copy,
        "inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        " && inputs.sim_fault == '' && runner.environment == 'github-hosted'",
    )
    found = policy.violations(copy)
    assert not any("Save cache" in line or "writer" in line or "default branch" in line for line in found), found


GATED = "must itself be gated on the default branch"
REF_MAIN = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"


@pytest.mark.parametrize(
    "guard, message",
    [
        (None, GATED),
        ("${{ github.ref_name == github.event.repository.default_branch && inputs.default_tip && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.default_tip && inputs.sim_fault == '' }}}}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.default_tip && runner.environment == 'github-hosted' }}}}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.default_tip && inputs.sim_fault != 'fail_test' && runner.environment == 'github-hosted' }}}}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.default_tip && inputs.sim_fault == '' && runner.environment == 'self-hosted' }}}}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}}}", GATED),
        ("${{ inputs.default_tip && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}", GATED),
        ("${{ github.ref != '' && inputs.default_tip && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}", GATED),
        ("${{ always() }}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.pr_sha == github.event.repository.default_branch && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}}}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.pr_sha == github.event.repository.default_branch && inputs.sim_fault == '' && runner.environment == 'github-hosted' }}}}", "must not compare the PR head by name"),
        ("${{ github.event_name == 'workflow_dispatch' }}", GATED),
        (f"${{{{ {REF_MAIN} && inputs.default_tip && inputs.sim_fault == 'fail_test' && runner.environment == 'github-hosted' }}}}", GATED),
    ],
)
def test_a_cache_save_step_that_can_run_off_the_default_branch_or_with_a_fault_is_refused(copy, guard, message):
    with_save_guard(copy, guard)
    assert any(message in line for line in policy.violations(copy)), policy.violations(copy)


def test_a_cache_writer_key_in_workflow_level_env_is_refused(copy):
    mutate(
        copy,
        BUILD,
        "permissions:\n  contents: read\n\njobs:",
        "permissions:\n  contents: read\n\nenv:\n  SIM_CACHE_WRITE_KEY: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n\njobs:",
    )
    assert any("outside a job gated" in line for line in policy.violations(copy))


def test_a_cache_writer_key_passed_to_a_called_workflow_without_a_guard_is_refused(copy):
    mutate(copy, TOP, CALLER, CALLER + "      write_key: ${{ secrets.SIM_CACHE_WRITE_KEY }}\n")
    assert any("SIM_CACHE_WRITE" in line for line in policy.violations(copy))


def test_ci_shellchecks_the_hook_and_every_sim_gate_script():
    jobs = policy.parse((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    runs = [str(step.get("run", "")).split() for step in jobs["workflow-lint"]["steps"]]
    shellcheck = [words for words in runs if words[:1] == ["shellcheck"]]
    assert len(shellcheck) == 1, runs
    assert {".githooks/pre-push", "core/*.sh", "adapters/dotnet/*.sh"} <= set(shellcheck[0][1:])
    assert {path.name for path in (WORKFLOWS.parents[1] / "core").glob("*.sh")} >= {"kill_own_runner.sh", "prejob.sh", "resolve_refs.sh"}
    assert {path.name for path in (WORKFLOWS.parents[1] / "adapters" / "dotnet").glob("*.sh")} >= {"cache_build.sh", "build_artifact.sh", "test_lane.sh"}


CI_STEP = "      - name: Run tests\n        run: python -m pytest\n"


def test_the_zero_tests_guard_before_the_test_step_is_refused(copy):
    text = (copy / LANE).read_text(encoding="utf-8")
    start = text.index(ZERO)
    block = text[start:text.index("      - name: Remove services\n")]
    mutate(copy, LANE, block, "")
    mutate(copy, LANE, "      - name: Test\n", block + "      - name: Test\n")
    assert any("the Zero tests step must come after the Test step" in line for line in policy.violations(copy))


def test_the_restore_before_the_tripwire_is_refused(copy):
    text = (copy / BUILD).read_text(encoding="utf-8")
    start = text.index("      - name: Restore cache\n")
    block = text[start:text.index(BUILD_STEP)]
    mutate(copy, BUILD, block, "")
    mutate(copy, BUILD, TRIPWIRE, block + TRIPWIRE)
    assert any("the Restore cache step must come after the tripwire and before the Build step" in line for line in policy.violations(copy))


def test_the_trusted_checkout_before_the_head_checkout_is_refused(copy):
    text = (copy / BUILD).read_text(encoding="utf-8")
    start = text.index("      - name: Checkout trusted\n")
    block = text[start:text.index("      - name: Hygiene\n")]
    mutate(copy, BUILD, block, "")
    mutate(copy, BUILD, "      - name: Checkout\n", block + "      - name: Checkout\n")
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
        (ROUTE, "    environment: sim-route\n", "    environment: sim-cache-write\n", "the cache writer key or environment is named at jobs.route.environment"),
        (LANE, LANE_RUN_TOP, "    timeout-minutes: 25\n    environment: sim-cache-write\n    env:\n", "the cache writer key or environment is named at jobs.run.environment"),
        (BUILD, BUILD_STEP, BUILD_STEP + "          SIM_CACHE_WRITE_SECRET: ${{ secrets.SIM_CACHE_WRITE_SECRET }}\n", "the cache writer key or environment is named at jobs.run.steps"),
    ],
)
def test_every_workflow_file_is_scanned_for_the_writer_tier_and_whole_secrets_reads(copy, filename, old, new, expected):
    mutate(copy, filename, old, new)
    found = policy.violations(copy)
    assert any(expected in line for line in found), found


def test_a_workflow_file_outside_the_policy_is_refused(copy):
    (copy / "extra.yaml").write_text("name: extra\non:\n  push:\npermissions:\n  contents: read\njobs:\n  x:\n    runs-on: ubuntu-24.04\n", encoding="utf-8")
    assert any("a new workflow needs a policy entry" in line for line in policy.violations(copy))


def test_a_workflow_the_policy_cannot_read_is_refused(copy):
    mutate(copy, "ci.yml", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env: {A: b}\n")
    assert any("ci.yml: the policy cannot read this workflow" in line for line in policy.violations(copy))


def test_every_shipped_invocation_resolves_and_the_scan_sees_the_known_ones():
    assert policy.invocation_violations(WORKFLOWS, ROOT) == []


@pytest.mark.parametrize(
    "filename, old, new, expected",
    [
        (TOP, '          python3 -m core.gate "$@"', '          python3 -m core.gates "$@"', "python -m core.gates does not resolve"),
        (ROUTE, "python3 -m core.route", "python3 -m core.route_old", "python -m core.route_old does not resolve"),
        (ROUTE, "python3 -m core.route", "python3 -m tools.sim_gate.route", "python -m tools.sim_gate.route does not resolve"),
        (BUILD, '          python3 -m adapters.dotnet.plan "$@" --root "$SIM_ROOT" --lane all', '          python3 \\\n              -m adapters.dotnet.planx "$@" --root "$SIM_ROOT" --lane all', "python -m adapters.dotnet.planx does not resolve"),
        (TOP, "        run: sh core/resolve_refs.sh\n", "        run: sh tools/sim_gate/resolve_refs.sh\n", "tools/sim_gate/resolve_refs.sh does not resolve"),
        (BUILD, "              sh .sim-trusted/core/prejob.sh || status=$?\n", "              sh .sim-trusted/core/prejob_old.sh || status=$?\n", ".sim-trusted/core/prejob_old.sh does not resolve"),
        (LANE, 'sh "$GITHUB_WORKSPACE/core/kill_own_runner.sh"', 'bash "$GITHUB_WORKSPACE/core/kill_runner.sh"', "core/kill_runner.sh does not resolve"),
        (LANE, 'sh "$GITHUB_WORKSPACE/core/kill_own_runner.sh"', '"$GITHUB_WORKSPACE/core/kill_own_runner.sh" && ./core/missing.sh', "./core/missing.sh does not resolve"),
        (LANE, "sh adapters/dotnet/test_lane.sh", "sh adapters/dotnet/lane.sh", "adapters/dotnet/lane.sh does not resolve"),
        (LANE, "python3 -m core.zero_tests", "python3 -m core.zero_test", "python -m core.zero_test does not resolve"),
        ("trial-gate.yml", "python3 -m workload run", "python3 workload/missing.py run", "workload/missing.py does not resolve"),
        (TOP, "    uses: ./.github/workflows/tpl-dotnet-build.yml", "    uses: ./.github/workflows/tpl-dotnet-builds.yml", "uses ./.github/workflows/tpl-dotnet-builds.yml, which does not exist"),
        (ROUTE, "python3 -m core.route", "python3 -c 'import core.route'", "no workflow runs python -m core.route"),
        (LANE, "python3 -m core.zero_tests", "python3 -c 'import core.zero_tests'", "no workflow runs python -m core.zero_tests"),
        (LANE, "sh adapters/dotnet/test_lane.sh", "cat adapters/dotnet/test_lane.sh", "no workflow runs adapters/dotnet/test_lane.sh"),
        (TOP, "        run: sh core/resolve_refs.sh\n", "        run: cat core/resolve_refs.sh\n", "no workflow runs core/resolve_refs.sh"),
        (ROUTE, "python3 -m core.route", "python3 -I -m core.nothere", "python -m core.nothere does not resolve"),
        (TOP, "        run: sh core/resolve_refs.sh\n", "        run: . core/nothere.sh\n", "core/nothere.sh does not resolve"),
    ],
)
def test_an_invocation_that_does_not_resolve_at_the_head_is_refused(copy, filename, old, new, expected):
    mutate(copy, filename, old, new)
    found = policy.invocation_violations(copy, ROOT)
    assert any(expected in line for line in found), found


def test_the_lane_test_runner_must_pass_no_build(tmp_path):
    script = tmp_path / "adapters" / "dotnet" / "test_lane.sh"
    script.parent.mkdir(parents=True)
    text = (ROOT / "adapters" / "dotnet" / "test_lane.sh").read_text(encoding="utf-8")
    assert " --no-build" in text
    script.write_text(text, encoding="utf-8")
    assert policy.lane_script_violations(tmp_path) == []
    script.write_text(text.replace(" --no-build", ""), encoding="utf-8")
    assert any("must pass --no-build" in line for line in policy.lane_script_violations(tmp_path))
