from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import workflow_policy as policy

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
LANE = "sim-gate-lane.yml"
ATTEMPT = "sim-gate-attempt.yml"
TOP = "sim-gate.yml"
SHA = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
ENVIRONMENT = "    environment: ${{ inputs.default_tip && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && inputs.fail_test == false && inputs.hosted == true && 'sim-cache-write' || inputs.hosted == true && 'sim-cache-read' || 'sim-cache-lab' }}\n"


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
        "        run: sh core/prejob.sh",
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
        "        continue-on-error: true\n        uses: tailscale/github-action@",
        "        uses: tailscale/github-action@",
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
        "validate must resolve head and base with sh core/resolve_refs.sh",
    ),
    "validate checking out the requested head instead of the dispatch commit": (
        TOP,
        "          fetch-depth: 0\n          persist-credentials: false\n      - name: Resolve refs",
        "          ref: ${{ inputs.head }}\n          fetch-depth: 0\n          persist-credentials: false\n      - name: Resolve refs",
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
        "      head_sha: ${{ steps.resolve.outputs.head_sha }}\n",
        "      head_sha: ${{ inputs.head }}\n",
        "validate must output head_sha, base_sha and default_tip",
    ),
    "the unit lane given the unresolved head": (
        TOP,
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.head_sha }}\n",
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ inputs.head }}\n",
        "job unit reads the unresolved ref",
    ),
    "the integration lane given the unresolved base": (
        TOP,
        "      lane: integration\n      base: ${{ needs.validate.outputs.base_sha }}\n",
        "      lane: integration\n      base: ${{ inputs.base }}\n",
        "lane integration must take head, base and default_tip from validate only",
    ),
    "a lane told it is the default tip without validate": (
        TOP,
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.head_sha }}\n      default_tip: ${{ needs.validate.outputs.default_tip == 'true' }}\n",
        "      lane: unit\n      base: ${{ needs.validate.outputs.base_sha }}\n      head: ${{ needs.validate.outputs.head_sha }}\n      default_tip: true\n",
        "lane unit must take head, base and default_tip from validate only",
    ),
    "the lane attempt claiming the default tip": (
        LANE,
        "      default_tip: ${{ inputs.default_tip }}\n      runner: ${{ needs.route",
        "      default_tip: true\n      runner: ${{ needs.route",
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
        "does not capture the infra reason",
    ),
    "gate without the unit reason": (
        TOP,
        '            --reason "unit=${UNIT_RETRY_REASON:-$UNIT_ATTEMPT_REASON}" \\\n',
        "",
        "infra reason of lane unit",
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
        "      - name: Hygiene\n        if: ${{ runner.environment == 'self-hosted' }}\n        run: sh core/prejob.sh\n",
        "",
        "has no Hygiene step",
    ),
    "hygiene step only on hosted runners": (
        ATTEMPT,
        "        if: ${{ runner.environment == 'self-hosted' }}\n        run: sh core/prejob.sh",
        "        if: ${{ runner.environment == 'github-hosted' }}\n        run: sh core/prejob.sh",
        "self-hosted runners only",
    ),
    "hygiene step without a guard": (
        ATTEMPT,
        "        if: ${{ runner.environment == 'self-hosted' }}\n        run: sh core/prejob.sh",
        "        run: sh core/prejob.sh",
        "self-hosted runners only",
    ),
    "hygiene step running something else": (
        ATTEMPT,
        "        run: sh core/prejob.sh",
        "        run: echo skipped",
        "must run sh core/prejob.sh",
    ),
    "hygiene step after the first dotnet step": (
        ATTEMPT,
        "      - name: Hygiene\n        if: ${{ runner.environment == 'self-hosted' }}\n        run: sh core/prejob.sh\n      - name: Set up .NET\n        uses: actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0\n        with:\n          global-json-file: sim/dotnet/global.json\n",
        "      - name: Set up .NET\n        uses: actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0\n        with:\n          global-json-file: sim/dotnet/global.json\n      - name: Hygiene\n        if: ${{ runner.environment == 'self-hosted' }}\n        run: sh core/prejob.sh\n",
        "must come before",
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
        'sh tools/sim_gate/cache_build.sh build "$RUNNER_TEMP/build.txt" "$RUNNER_TEMP/built.txt"',
        "echo build",
        "Build step must go through cache_build.sh build",
    ),
    "the cache tripwire step removed": (
        ATTEMPT,
        '      - name: Cache tripwire\n        env:\n          SIM_TAILNET_JOIN: ${{ steps.join.outcome }}\n          SIM_CACHE_ENDPOINT_LAB: ${{ secrets.SIM_CACHE_ENDPOINT_LAB }}\n          SIM_CACHE_ENDPOINT_TAILNET: ${{ secrets.SIM_CACHE_ENDPOINT_TAILNET }}\n          SIM_CACHE_READ_KEY_ID: ${{ secrets.SIM_CACHE_READ_KEY_ID }}\n          SIM_CACHE_READ_SECRET: ${{ secrets.SIM_CACHE_READ_SECRET }}\n        run: sh tools/sim_gate/cache_build.sh tripwire "$MARKERS"\n',
        "",
        "tripwire step must come before the Build step",
    ),
    "the cache tripwire step running something else": (
        ATTEMPT,
        'run: sh tools/sim_gate/cache_build.sh tripwire "$MARKERS"',
        "run: echo clear",
        "must run cache_build.sh tripwire",
    ),
    "the cache tripwire step before the plan": (
        ATTEMPT,
        "      - name: Plan\n",
        "      - name: Cache tripwire\n        run: sh tools/sim_gate/cache_build.sh tripwire \"$MARKERS\"\n      - name: Plan\n",
        "tripwire step must come after Plan",
    ),
    "the save step running something else": (
        ATTEMPT,
        'run: sh tools/sim_gate/cache_build.sh save "$RUNNER_TEMP/built.txt"',
        "run: echo save",
        "must only run cache_build.sh save",
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
    assert {".githooks/pre-push", "core/*.sh", "tools/sim_gate/*.sh"} <= set(shellcheck[0][1:])
    assert {path.name for path in (WORKFLOWS.parents[1] / "core").glob("*.sh")} >= {"kill_own_runner.sh", "prejob.sh", "resolve_refs.sh"}
    assert {path.name for path in (WORKFLOWS.parents[1] / "tools" / "sim_gate").glob("*.sh")} >= {"cache_build.sh"}
