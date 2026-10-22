from __future__ import annotations

import re
from pathlib import Path

TOKEN_NAME = "SIM_ROUTE_TOKEN"
WRITER_PREFIX = "SIM_CACHE_WRITE"
READER_PREFIX = "SIM_CACHE_READ"
FORBIDDEN_TRIGGERS = {"pull_request_target", "workflow_run", "issue_comment"}
SECRETISH_VARIABLE = re.compile(r"\bvars\.[A-Za-z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|ENDPOINT)", re.I)
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
BUDGET_MINUTES = 80
MAX_RUN_MINUTES = 30
MAX_JOIN_MINUTES = 5


class Unsupported(ValueError):
    pass


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _strip_comment(text):
    quote = None
    for index, char in enumerate(text):
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] == " "):
            return text[:index].rstrip()
    return text.rstrip()


def _scalar(text):
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


KEY = re.compile(r"""^("[^"]+"|'[^']+'|[^\s:'"\-\[{#][^:]*?|-[^\s:][^:]*?):(?:\s+(.*))?$""")


class Parser:
    def __init__(self, text):
        if "\t" in text:
            raise Unsupported("tab")
        self.lines = text.replace("\r\n", "\n").split("\n")
        self.i = 0

    def peek(self):
        while self.i < len(self.lines):
            stripped = self.lines[self.i].strip()
            if stripped and not stripped.startswith("#"):
                return self.lines[self.i]
            self.i += 1
        return None

    def block(self, indent):
        line = self.peek()
        if line is None:
            return ""
        if line.lstrip().startswith("- ") or line.strip() == "-":
            return self.sequence(_indent(line))
        return self.mapping(_indent(line))

    def mapping(self, indent):
        result = {}
        while True:
            line = self.peek()
            if line is None or _indent(line) < indent:
                return result
            if _indent(line) > indent or line.lstrip().startswith("- "):
                raise Unsupported(f"unexpected structure at line {self.i + 1}")
            match = KEY.match(_strip_comment(line.strip()))
            if not match:
                raise Unsupported(f"not a mapping line: {line!r}")
            key, rest = _scalar(match.group(1)), match.group(2) or ""
            if key in result:
                raise Unsupported(f"duplicate key {key}")
            self.i += 1
            result[key] = self.value(rest, indent)

    def value(self, rest, indent):
        rest = _strip_comment(rest)
        if rest == "":
            following = self.peek()
            return self.block(_indent(following)) if following and _indent(following) > indent else ""
        if rest[0] in "|>":
            return self.block_scalar(indent, rest[0] == ">")
        if rest[0] == "[":
            if rest[-1] != "]":
                raise Unsupported("multi-line flow sequence")
            return [_scalar(part.strip()) for part in rest[1:-1].split(",") if part.strip()]
        if rest[0] in "{&*!%@`":
            raise Unsupported(f"unsupported value {rest!r}")
        return _scalar(rest)

    def block_scalar(self, indent, folded):
        collected = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() and _indent(line) <= indent:
                break
            collected.append(line)
            self.i += 1
        while collected and not collected[-1].strip():
            collected.pop()
        base = min((_indent(line) for line in collected if line.strip()), default=0)
        body = [line[base:] for line in collected]
        return (" ".join(body) if folded else "\n".join(body)) + "\n"

    def sequence(self, indent):
        items = []
        while True:
            line = self.peek()
            if line is None or _indent(line) < indent:
                return items
            content = line.strip()
            if _indent(line) != indent or not (content.startswith("- ") or content == "-"):
                raise Unsupported(f"unexpected structure at line {self.i + 1}")
            inner = content[1:].lstrip()
            if not inner:
                self.i += 1
                items.append(self.block(indent))
            elif KEY.match(_strip_comment(inner)) and not inner.startswith(("'", '"')):
                self.lines[self.i] = " " * (indent + 2) + inner
                items.append(self.mapping(indent + 2))
            else:
                self.i += 1
                items.append(_scalar(_strip_comment(inner)))


def parse(text):
    parser = Parser(text)
    document = parser.block(0)
    if parser.peek() is not None:
        raise Unsupported(f"trailing content at line {parser.i + 1}")
    return document


TOKEN = re.compile(r"""\s*(?:(\|\||&&|==|!=)|('(?:[^']|'')*')|(!?[A-Za-z_]\w*\(\))|(format\()|([,)])|([A-Za-z_][\w.-]*)|(.))""")


def truthy(value):
    return value not in (None, "", False, 0)


def _loose_equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if left in (None, "") and right in (None, ""):
        return True
    return left == right


class Expression:
    def __init__(self, text, context):
        self.tokens = []
        position = 0
        text = text.strip()
        while position < len(text):
            match = TOKEN.match(text, position)
            if match.group(7):
                raise Unsupported(f"unsupported character {match.group(7)!r} in {text!r}")
            position = match.end()
            op, string, function, call, punct, path, _ = match.groups()
            if call:
                self.tokens.append(("fn", "format"))
            elif punct:
                self.tokens.append(("punct", punct))
            elif op:
                self.tokens.append(("op", op))
            elif string:
                self.tokens.append(("val", string[1:-1].replace("''", "'")))
            elif function:
                if function not in ("!cancelled()", "always()"):
                    raise Unsupported(f"unsupported function {function}")
                self.tokens.append(("val", True))
            elif path in ("true", "false"):
                self.tokens.append(("val", path == "true"))
            else:
                self.tokens.append(("val", context.get(path)))
        self.position = 0

    def take(self, *ops):
        if self.position < len(self.tokens) and self.tokens[self.position][0] == "op" and self.tokens[self.position][1] in ops:
            self.position += 1
            return self.tokens[self.position - 1][1]
        return None

    def atom(self):
        kind, value = self.tokens[self.position]
        if kind == "fn":
            self.position += 1
            arguments = [self.disjunction()]
            while self.tokens[self.position] == ("punct", ","):
                self.position += 1
                arguments.append(self.disjunction())
            if self.tokens[self.position] != ("punct", ")"):
                raise Unsupported("unterminated format call")
            self.position += 1
            template = str(arguments[0])
            for index, argument in enumerate(arguments[1:]):
                template = template.replace("{" + str(index) + "}", str(argument))
            return template
        if kind != "val":
            raise Unsupported("operand expected")
        self.position += 1
        return value

    def comparison(self):
        left = self.atom()
        op = self.take("==", "!=")
        if op:
            equal = _loose_equal(left, self.atom())
            return equal if op == "==" else not equal
        return left

    def conjunction(self):
        value = self.comparison()
        while self.take("&&"):
            right = self.comparison()
            value = right if truthy(value) else value
        return value

    def disjunction(self):
        value = self.conjunction()
        while self.take("||"):
            right = self.conjunction()
            value = value if truthy(value) else right
        return value

    def evaluate(self):
        value = self.disjunction()
        if self.position != len(self.tokens):
            raise Unsupported("trailing tokens")
        return value


EXPRESSION = re.compile(r"^\$\{\{(.*)\}\}$", re.S)


def condition(text):
    return text if "${{" in text else "${{ " + text + " }}"


def evaluate(text, context):
    match = EXPRESSION.match(text.strip())
    return Expression(match.group(1) if match else f"'{text}'", context).evaluate()


def walk(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, path + (index,))
    else:
        yield path, node



def load(directory):
    paths = [*Path(directory).glob("sim-gate*.yml"), *Path(directory).glob("sim-retest.yml"), *Path(directory).glob("tpl-*.yml")]
    return {p.name: parse(p.read_text(encoding="utf-8")) for p in sorted(paths)}


def triggers(doc):
    on = doc.get("on", {})
    return set(on) if isinstance(on, (dict, list)) else {on}


def minutes(docs, workflow, job):
    return int(docs[workflow]["jobs"][job]["timeout-minutes"])


ENTRY = "sim-gate.yml"
RETEST = "sim-retest.yml"
ROUTE = "tpl-route.yml"
BUILD = "tpl-dotnet-build.yml"
LANE = "tpl-dotnet-lane.yml"
SIM_WORKFLOWS = {ENTRY, RETEST, ROUTE, BUILD, LANE}
OTHER_WORKFLOWS = {"ci.yml", "leak-guard.yml", "leak-guard-scheduled.yml", "trial-gate.yml"}
DISPATCH_ONLY = {ENTRY, RETEST}
CALLED = {ROUTE, BUILD, LANE}
WORK = (BUILD, LANE)
ROUTE_USES = "./.github/workflows/tpl-route.yml"
BUILD_USES = "./.github/workflows/tpl-dotnet-build.yml"
LANE_USES = "./.github/workflows/tpl-dotnet-lane.yml"
HOSTED = "ubuntu-24.04"
RUNNER = "${{ needs.route.outputs.target == 'onprem' && inputs.runner_label || 'ubuntu-24.04' }}"
SOLUTION_GLOBAL_JSON = "${{ inputs.solution_root }}/global.json"
CACHE_BUILD = "adapters/dotnet/cache_build.sh"
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
FAULTS = ("none", "fail_test", "kill_onprem", "kill_both", "fail_health_once", "fail_health_twice", "fail_unit_health_once")
SIM_FAULTS = ("", "fail_test", "kill_onprem", "kill_both", "fail_health_once", "fail_health_twice", "fail_unit_health_once", "other")
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
UPLOAD = "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"
DOWNLOAD = "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"

SECRET_REF = re.compile(r"""\bsecrets\s*[.\[]\s*['"]?([A-Za-z0-9_]+)""")
ENDPOINT_NAMES = ("SIM_CACHE_ENDPOINT_LAB", "SIM_CACHE_ENDPOINT_TAILNET")
TAILNET_INPUTS = {"SIM_TAILNET_CLIENT_ID": "oauth-client-id", "SIM_TAILNET_AUDIENCE": "audience"}
JOIN_STEP = "Join the tailnet"
TRIPWIRE_STEP = "Cache tripwire"
RESTORE_STEP = "Restore cache"
SAVE_STEP = "Save cache"
SECRET_STEPS = {"endpoint": {TRIPWIRE_STEP, RESTORE_STEP, SAVE_STEP}, "reader": {TRIPWIRE_STEP, RESTORE_STEP}, "writer": {SAVE_STEP}}
WRITE_ENVIRONMENT = "sim-cache-write"
READ_ENVIRONMENT = "sim-cache-read"
LAB_ENVIRONMENT = "sim-cache-lab"
ENVIRONMENT_PLACES = {(ROUTE, "route"), (BUILD, "run")}
BUILD_CALLER = {"contents": "read", "actions": "read", "checks": "read", "id-token": "write"}
LANE_CALLER = {"contents": "read", "actions": "read", "checks": "read"}
CLASSIFY_PERMISSIONS = {"contents": "read", "actions": "read", "checks": "read"}
PERMISSION_MAP = {
    (ENTRY, "build"): BUILD_CALLER,
    (ENTRY, "unit"): LANE_CALLER,
    (ENTRY, "integration"): LANE_CALLER,
    (BUILD, "run"): {"contents": "read", "id-token": "write"},
    (BUILD, "classify"): CLASSIFY_PERMISSIONS,
    (LANE, "classify"): CLASSIFY_PERMISSIONS,
    (ENTRY, "validate"): {"contents": "read", "actions": "read"},
    (ENTRY, "comment"): {"contents": "read", "actions": "read", "pull-requests": "write"},
    (ENTRY, "requeue"): {"contents": "read", "actions": "write"},
    (RETEST, "requeue"): {"contents": "read", "actions": "write"},
}
ID_TOKEN_PLACES = {(name, ("jobs", job, "permissions")) for (name, job), wanted in PERMISSION_MAP.items() if "id-token" in wanted}
WRITE_PLACES = {(name, ("jobs", job, "permissions", scope)) for (name, job), wanted in PERMISSION_MAP.items() for scope, level in wanted.items() if level == "write"}

DOTNET_ENV = {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_USE_MSBUILD_SERVER": "0"}
FAIL_TEST_ENV = "${{ inputs.sim_fault == 'fail_test' && '1' || '0' }}"
ENDPOINT_ENV = {name: "${{ secrets." + name + " }}" for name in ENDPOINT_NAMES}
READER_ENV = {**ENDPOINT_ENV, **{name: "${{ secrets." + name + " }}" for name in ("SIM_CACHE_READ_KEY_ID", "SIM_CACHE_READ_SECRET")}}
WRITER_ENV = {**ENDPOINT_ENV, **{name: "${{ secrets." + name + " }}" for name in ("SIM_CACHE_WRITE_KEY_ID", "SIM_CACHE_WRITE_SECRET")}}
TRIPWIRE_RUN = f'sh {CACHE_BUILD} tripwire "$MARKERS"'
RESTORE_RUN = f"""set -eu
if [ "$FAIL_TEST" = 1 ]; then
    SIM_CACHE_MODE=off
    export SIM_CACHE_MODE
fi
sh {CACHE_BUILD} restore "$RUNNER_TEMP/build.txt" "$RUNNER_TEMP/hits.txt"
"""
BUILD_RUN = r"""set -eu
if [ "$FAIL_TEST" = 1 ]; then
    project=$(head -n 1 "$RUNNER_TEMP/out/unit.txt")
    if [ -n "$project" ]; then
        printf '%s\n' 'using Xunit;' 'public class ForcedFailure' '{' '    [Fact]' \
            '    public void Asserts_the_wrong_value() => Assert.Equal(2, 1);' '}' > "$(dirname "$GITHUB_WORKSPACE/$project")/ForcedFailure.cs"
    fi
fi
sh adapters/dotnet/cache_build.sh build "$RUNNER_TEMP/build.txt" "$RUNNER_TEMP/hits.txt" "$RUNNER_TEMP/built.txt"
"""
SAVE_RUN = f'sh {CACHE_BUILD} save "$RUNNER_TEMP/built.txt"'
ARTIFACT = "dotnet-build-${{ github.run_attempt }}"
SAVE_ENV = {"SIM_DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}", "SIM_CACHE_MODE": "${{ inputs.cache }}", **WRITER_ENV}
FAIL_HEALTH_ENV = "${{ inputs.sim_fault == 'fail_health_twice' || inputs.sim_fault == 'fail_health_once' && inputs.retest != true }}"
HEALTH_RUN = """set -eu
mkdir -p "$RUNNER_TEMP/markers"
echo "MARKERS=$RUNNER_TEMP/markers" >> "$GITHUB_ENV"
if [ "$FAIL_HEALTH" = true ]; then
    echo "::error::injected health failure"
    exit 1
fi
python3 --version
(cd "$SIM_ROOT" && dotnet --version)
touch "$RUNNER_TEMP/markers/health"
"""
LANE_FAIL_HEALTH_ENV = "${{ inputs.sim_fault == 'fail_unit_health_once' && inputs.lane == 'unit' && inputs.retest != true }}"
BUILD_PINS = {
    "Health": {"name": "Health", "env": {"FAIL_HEALTH": FAIL_HEALTH_ENV}, "run": HEALTH_RUN},
    TRIPWIRE_STEP: {"name": TRIPWIRE_STEP, "env": {"SIM_TAILNET_JOIN": "${{ steps.join.outcome }}", **READER_ENV}, "run": TRIPWIRE_RUN},
    RESTORE_STEP: {"name": RESTORE_STEP, "env": {"FAIL_TEST": FAIL_TEST_ENV, "SIM_CACHE_MODE": "${{ inputs.cache }}", **READER_ENV}, "run": RESTORE_RUN},
    "Build": {"name": "Build", "env": {"FAIL_TEST": FAIL_TEST_ENV, **DOTNET_ENV}, "run": BUILD_RUN},
    "Pack": {"name": "Pack", "run": 'sh adapters/dotnet/build_artifact.sh pack "$RUNNER_TEMP/out/build.tar"'},
    "Upload build": {
        "name": "Upload build",
        "uses": UPLOAD,
        "with": {"name": ARTIFACT, "path": "${{ runner.temp }}/out", "if-no-files-found": "error", "retention-days": "1"},
    },
    "Built": {"name": "Built", "run": 'touch "$MARKERS/build"'},
}
BUILD_STEPS = [
    "Checkout", "Set up Python", "Checkout trusted", "Hygiene", "Set up .NET", "Health", "Affected", "Plan", JOIN_STEP,
    TRIPWIRE_STEP, RESTORE_STEP, "Build", "Pack", "Upload build", SAVE_STEP, "Built", "Upload markers",
]
KILL_RUNNER = "${{ inputs.sim_fault == 'kill_both' || inputs.sim_fault == 'kill_onprem' && needs.route.outputs.target == 'onprem' }}"
UNPACK_RUN = 'sh adapters/dotnet/build_artifact.sh unpack "$RUNNER_TEMP/build/build.tar"'
TEST_RUN = """set -u
touch "$MARKERS/test-start"
if [ "$KILL_RUNNER" = true ]; then
    sh "$GITHUB_WORKSPACE/core/kill_own_runner.sh" || exit 1
    sleep 600
fi
case "$LANE" in
    integration) filter="Category=integration" ;;
    *) filter="Category!=integration" ;;
esac
status=0
sh adapters/dotnet/test_lane.sh "$RUNNER_TEMP/build/$LANE.txt" "$RUNNER_TEMP/counts" "$filter" "$PARALLEL" || status=1
touch "$MARKERS/test-end"
exit "$status"
"""
ZERO_RUN = """set -u
status=0
python3 -m core.zero_tests --list "$RUNNER_TEMP/build/$LANE.txt" --counts "$RUNNER_TEMP/counts" > "$RUNNER_TEMP/zero.txt" || status=1
cat "$RUNNER_TEMP/zero.txt"
[ "$status" = 0 ] || exit 1
awk '/^executed=[0-9]+ / { sub(/^executed=/, ""); n += $1 } END { printf "executed=%d\\n", n }' "$RUNNER_TEMP/zero.txt" >> "$GITHUB_OUTPUT"
"""
LANE_PINS = {
    "Health": {"name": "Health", "env": {"FAIL_HEALTH": LANE_FAIL_HEALTH_ENV}, "run": HEALTH_RUN},
    "Unpack": {"name": "Unpack", "run": UNPACK_RUN},
    "Test": {
        "name": "Test",
        "env": {"LANE": "${{ inputs.lane }}", "PARALLEL": "${{ inputs.parallel }}", "KILL_RUNNER": KILL_RUNNER, **DOTNET_ENV},
        "run": TEST_RUN,
    },
    "Zero tests": {"name": "Zero tests", "id": "zero", "env": {"LANE": "${{ inputs.lane }}"}, "run": ZERO_RUN},
}
LANE_STEPS = [
    "Checkout", "Set up Python", "Checkout trusted", "Hygiene", "Download build", "Unpack", "Set up .NET", "Health",
    "Services", "Test", "Zero tests", "Remove services", "Upload markers",
]
NOT_IN_A_LANE = ("Build", "Plan", "Affected", RESTORE_STEP, TRIPWIRE_STEP, SAVE_STEP, "Pack", "Upload build", JOIN_STEP)
BUILDING_TEXT = re.compile(r"cache_build\.sh|adapters[./]dotnet[./](?:affected|plan)\b|\bmsbuild\b|\bdotnet\s+(?:build|publish|pack|run|msbuild|restore)\b", re.I)
DOTNET_WORD = re.compile(r"(?<![\w/.-])dotnet(?![\w/.-])")
CLASSIFY_TAIL = r"""case "$verdict" in
    pass | test | infra) ;;
    *) verdict="test" ;;
esac
reason=
[ ! -f "$RUNNER_TEMP/reason.txt" ] || reason=$(cat "$RUNNER_TEMP/reason.txt")
case "$reason" in
    *[!A-Za-z0-9\ _-]*) reason= ;;
esac
echo "verdict=${verdict}" >> "$GITHUB_OUTPUT"
echo "reason=${reason}" >> "$GITHUB_OUTPUT"
"""
CLASSIFY_CALLS = {
    BUILD: 'python3 -m core.classify --job-dir "$RUNNER_TEMP/job" --fetch-job "run dotnet-build" --fetch-artifact markers-dotnet-build'
    ' --run-result "$RUN_RESULT" --reason-file "$RUNNER_TEMP/reason.txt" --infra-step "Set up .NET" --infra-step "Upload build" --done-marker build',
    LANE: 'python3 -m core.classify --job-dir "$RUNNER_TEMP/job" --fetch-job "$JOB_SUFFIX" --fetch-artifact "$ARTIFACT_NAME"'
    ' --run-result "$RUN_RESULT" --reason-file "$RUNNER_TEMP/reason.txt" --infra-step "Set up .NET" --infra-step "Download build"',
}
CLASSIFY_ENVS = {
    BUILD: {"GITHUB_TOKEN": "${{ github.token }}", "RUN_RESULT": "${{ needs.run.result }}"},
    LANE: {
        "GITHUB_TOKEN": "${{ github.token }}",
        "JOB_SUFFIX": "run dotnet-${{ inputs.lane }}",
        "ARTIFACT_NAME": "markers-dotnet-${{ inputs.lane }}",
        "RUN_RESULT": "${{ needs.run.result }}",
    },
}
CLASSIFY_NAMES = {BUILD: "classify dotnet-build", LANE: "classify dotnet-${{ inputs.lane }}"}
RUN_NAMES = {BUILD: "run dotnet-build", LANE: "run dotnet-${{ inputs.lane }}"}


def classify_job(name):
    return {
        "name": CLASSIFY_NAMES[name],
        "needs": "run",
        "if": "${{ always() }}",
        "runs-on": HOSTED,
        "timeout-minutes": "5",
        "permissions": CLASSIFY_PERMISSIONS,
        "outputs": {"verdict": "${{ steps.verdict.outputs.verdict }}", "reason": "${{ steps.verdict.outputs.reason }}"},
        "steps": [
            {"name": "Checkout", "uses": CHECKOUT, "with": {"persist-credentials": "false"}},
            {"name": "Set up Python", "uses": SETUP_PYTHON, "with": {"python-version": "3.12"}},
            {
                "name": "Classify",
                "id": "verdict",
                "env": CLASSIFY_ENVS[name],
                "run": f"set -eu\nverdict=$({CLASSIFY_CALLS[name]})\n" + CLASSIFY_TAIL,
            },
        ],
    }


WORKFLOW_OUTPUTS = {
    ROUTE: {"target": "${{ jobs.route.outputs.target }}"},
    BUILD: {
        "verdict": "${{ jobs.classify.outputs.verdict }}",
        "reason": "${{ jobs.classify.outputs.reason }}",
        "artifact": "${{ jobs.run.outputs.artifact }}",
    },
    LANE: {
        "verdict": "${{ jobs.classify.outputs.verdict }}",
        "reason": "${{ jobs.classify.outputs.reason }}",
        "executed": "${{ jobs.run.outputs.executed }}",
    },
}
RUN_OUTPUTS = {BUILD: {"artifact": ARTIFACT}, LANE: {"executed": "${{ steps.zero.outputs.executed }}"}}


def run_steps(docs, name):
    return docs[name]["jobs"]["run"].get("steps", [])


def step_named(docs, name, step):
    return next((st for st in run_steps(docs, name) if st.get("name") == step), {})


def pinned_step_violations(docs):
    found = []
    for name, pins in ((BUILD, BUILD_PINS), (LANE, LANE_PINS)):
        for step, wanted in pins.items():
            if step_named(docs, name, step) != wanted:
                found.append(f"{name}: the {step} step must be exactly the pinned step, every key included")
    for name, wanted in ((BUILD, BUILD_STEPS), (LANE, LANE_STEPS)):
        names = [st.get("name") for st in run_steps(docs, name)]
        if names != wanted:
            found.append(f"{name}: the run job steps are {names}, the pinned order is {wanted}; a step may not be added, dropped or moved")
    for name in WORK:
        job = docs[name]["jobs"]
        if job.get("classify") != classify_job(name):
            found.append(f"{name}: the classify job must be exactly the pinned job: its steps, outputs and the Classify invocation with --run-result from needs.run.result")
        outputs = docs[name].get("on", {}).get("workflow_call", {}).get("outputs", {})
        values = {key: value.get("value") if isinstance(value, dict) else value for key, value in outputs.items()}
        if values != WORKFLOW_OUTPUTS[name]:
            found.append(f"{name}: the workflow outputs must be exactly {WORKFLOW_OUTPUTS[name]}")
        if job["run"].get("outputs", {}) != RUN_OUTPUTS[name]:
            found.append(f"{name}: the run job outputs must be exactly {RUN_OUTPUTS[name]}")
        if job["run"].get("name") != RUN_NAMES[name]:
            found.append(f"{name}: the run job must be named {RUN_NAMES[name]!r}; classify finds it by that name")
    outputs = docs[ROUTE].get("on", {}).get("workflow_call", {}).get("outputs", {})
    if {key: value.get("value") if isinstance(value, dict) else value for key, value in outputs.items()} != WORKFLOW_OUTPUTS[ROUTE]:
        found.append(f"{ROUTE}: the workflow outputs must be exactly {WORKFLOW_OUTPUTS[ROUTE]}")
    for name in SIM_WORKFLOWS:
        for path, _ in walk(docs[name]):
            if path[-1:] in (("shell",), ("working-directory",), ("defaults",)) or "defaults" in path:
                found.append(f"{name}: {'.'.join(map(str, path))} is refused; shell, working-directory and defaults change what a pinned step runs")
                break
    return found


def secret_class(secret):
    if secret in ENDPOINT_NAMES:
        return "endpoint"
    if secret.startswith(READER_PREFIX):
        return "reader"
    if secret.startswith(WRITER_PREFIX):
        return "writer"
    return None


def secret_allowed(docs, name, path, secret, value):
    key = str(path[-1])
    exact = value.strip() == "${{ secrets." + secret + " }}"
    if secret == TOKEN_NAME:
        return exact and key == secret and name == ROUTE and path[:3] == ("jobs", "route", "steps") and path[4:5] == ("env",) and len(path) == 6
    in_run_step = name == BUILD and path[:3] == ("jobs", "run", "steps") and len(path) == 6
    if not in_run_step:
        return False
    step = docs[name]["jobs"]["run"]["steps"][path[3]]
    if secret in TAILNET_INPUTS:
        return exact and key == TAILNET_INPUTS[secret] and path[4] == "with" and step.get("name") == JOIN_STEP
    kind = secret_class(secret)
    return kind is not None and exact and key == secret and path[4] == "env" and step.get("name") in SECRET_STEPS[kind]


def secret_violations(docs):
    found = []
    for name, doc in docs.items():
        for path, value in walk(doc):
            where = ".".join(map(str, path))
            for match in SECRET_REF.finditer(str(value)):
                if not secret_allowed(docs, name, path, match.group(1), str(value)):
                    found.append(f"{name}: secret {match.group(1)} is read at a place that is not allowed: {where}")
            if "secrets" in path:
                found.append(f"{name}: no secrets mapping or declaration is allowed, secrets are read in a named step env only: {where}")
    return found


GUARD_NAME_COMPARE = re.compile(r"\binputs\.pr_(?:sha|branch)\b")
SAVE_OK = {
    "github.event.repository.default_branch": "main",
    "github.ref": "refs/heads/main",
    "inputs.default_tip": True,
    "inputs.sim_fault": "",
    "runner.environment": "github-hosted",
    "inputs.pr_sha": FULL_SHA,
}
SAVE_REFUSED = [
    dict(SAVE_OK, **{"github.ref": "refs/heads/topic"}),
    dict(SAVE_OK, **{"github.ref": "refs/tags/main"}),
    dict(SAVE_OK, **{"inputs.default_tip": False}),
    dict(SAVE_OK, **{"inputs.pr_sha": "main", "inputs.default_tip": False}),
    dict(SAVE_OK, **{"runner.environment": "self-hosted"}),
    *(dict(SAVE_OK, **{"inputs.sim_fault": fault}) for fault in SIM_FAULTS if fault),
]


def default_branch_only(guard):
    return truthy(evaluate(condition(guard), SAVE_OK)) and not any(
        truthy(evaluate(condition(guard), context)) for context in SAVE_REFUSED
    )


def hosted_only(guard):
    return truthy(evaluate(condition(guard), {"runner.environment": "github-hosted"})) and not truthy(
        evaluate(condition(guard), {"runner.environment": "self-hosted"})
    )


def self_hosted_only(step):
    guard = condition(str(step["if"])) if "if" in step else "${{ true }}"
    return truthy(evaluate(guard, {"runner.environment": "self-hosted"})) and not truthy(evaluate(guard, {"runner.environment": "github-hosted"}))


def environment_violations(docs):
    found = []
    run = docs[BUILD]["jobs"]["run"]
    expression = run.get("environment")
    if not isinstance(expression, str):
        return [f"{BUILD}: the run job must choose its environment with an expression"]
    if GUARD_NAME_COMPARE.search(expression):
        found.append(f"{BUILD}: the environment must not compare the PR head by name; names compare case-insensitively, use inputs.default_tip")
    for target in ("", "cloud", "onprem"):
        for label in ("", "lab"):
            for ref in ("refs/heads/main", "refs/heads/topic", "refs/tags/main", "refs/heads/main2"):
                for default_tip in (False, True):
                    for fault in SIM_FAULTS:
                        context = {
                            "github.event.repository.default_branch": "main",
                            "github.ref": ref,
                            "inputs.default_tip": default_tip,
                            "inputs.sim_fault": fault,
                            "needs.route.outputs.target": target,
                            "inputs.runner_label": label,
                            "inputs.pr_sha": "main",
                        }
                        chosen = evaluate(expression, context)
                        hosted = evaluate(RUNNER, context) == HOSTED
                        if not hosted:
                            wanted = LAB_ENVIRONMENT
                        elif (ref, default_tip, fault) == ("refs/heads/main", True, ""):
                            wanted = WRITE_ENVIRONMENT
                        else:
                            wanted = READ_ENVIRONMENT
                        if chosen != wanted:
                            found.append(
                                f"{BUILD}: the run environment is {chosen!r} for target {target!r}, label {label!r}, ref {ref}, default_tip {default_tip}, fault {fault!r}; wanted {wanted!r}"
                            )
    for name in WORK:
        for job_id in ("route", "classify"):
            if "environment" in docs[name]["jobs"].get(job_id, {}):
                found.append(f"{name}: job {job_id} must have no environment")
    if "environment" in docs[LANE]["jobs"]["run"]:
        found.append(f"{LANE}: the lane run job must have no environment; a lane holds no secret")
    for name, doc in docs.items():
        for job_id, job in doc["jobs"].items():
            if any(SECRET_REF.search(str(value)) for _, value in walk(job)) and "environment" not in job:
                found.append(f"{name}: job {job_id} reads secrets without an environment, so they would be empty in a called workflow")
    return found


def save_violations(docs):
    found = []
    steps = run_steps(docs, BUILD)
    for step in steps:
        if WRITER_PREFIX not in str(step):
            continue
        guard = step.get("if")
        if GUARD_NAME_COMPARE.search(str(guard)):
            found.append(f"{BUILD}: the cache save guard must not compare the PR head by name; names compare case-insensitively, use inputs.default_tip")
        if guard is None or not default_branch_only(str(guard)):
            found.append(f"{BUILD}: a step reading the cache writer key must itself be gated on the default branch, never with a fault")
        if set(step) != {"name", "if", "env", "run"} or step.get("name") != SAVE_STEP:
            found.append(f"{BUILD}: the step holding the cache writer key must be named {SAVE_STEP} and have exactly name, if, env and run")
        if step.get("env") != SAVE_ENV:
            found.append(f"{BUILD}: the save step env must be exactly {sorted(SAVE_ENV)}, the default branch from github.event.repository.default_branch")
        if step.get("run") != SAVE_RUN:
            found.append(f"{BUILD}: the step holding the cache writer key must run exactly {SAVE_RUN}")
    if not any(WRITER_PREFIX in str(step) for step in steps):
        found.append(f"{BUILD}: the run job has no {SAVE_STEP} step")
    return found


def cache_wiring_violations(docs):
    found = save_violations(docs)
    steps = run_steps(docs, BUILD)
    names = [st.get("name") for st in steps]
    for step in steps:
        tailscale = str(step.get("uses", "")).startswith("tailscale/github-action@")
        if (tailscale or any(name in str(step) for name in TAILNET_INPUTS)) and not (
            tailscale and "if" in step and hosted_only(str(step["if"]))
        ):
            found.append("the tailnet credentials may be used only by the tailscale action on a github-hosted runner")
        if tailscale:
            inputs = step.get("with", {})
            if inputs.get("tags") != "tag:ci-sim":
                found.append("the tailscale action must join with tags: tag:ci-sim")
            if "authkey" in inputs or "oauth-secret" in inputs:
                found.append("the tailscale action must use workload identity, not a stored auth key or OAuth secret")
            if not inputs.get("oauth-client-id") or not inputs.get("audience"):
                found.append("the tailscale action needs oauth-client-id and audience for workload identity")
            if "--accept-routes=false" not in str(inputs.get("args", "")):
                found.append("the tailscale action must turn route acceptance off with --accept-routes=false")
            if str(inputs.get("use-cache")) != "false":
                found.append("the tailscale action must not use its binary cache")
    if TRIPWIRE_STEP not in names or "Build" not in names or names.index(TRIPWIRE_STEP) > names.index("Build"):
        found.append("the cache tripwire step must come before the Build step")
    elif steps[names.index(TRIPWIRE_STEP)].get("run") != TRIPWIRE_RUN:
        found.append(f"the cache tripwire step must run exactly {TRIPWIRE_RUN}")
    for early in ("Health", "Affected", "Plan"):
        if early in names and TRIPWIRE_STEP in names and names.index(early) > names.index(TRIPWIRE_STEP):
            found.append(f"the cache tripwire step must come after {early}")
    if RESTORE_STEP not in names or TRIPWIRE_STEP not in names or not names.index(TRIPWIRE_STEP) < names.index(RESTORE_STEP) < names.index("Build"):
        found.append("the Restore cache step must come after the tripwire and before the Build step")
    if JOIN_STEP not in names:
        found.append("the build job has no step that joins the tailnet on hosted runners")
    else:
        join = steps[names.index(JOIN_STEP)]
        if TRIPWIRE_STEP in names and names.index(JOIN_STEP) > names.index(TRIPWIRE_STEP):
            found.append("the tailnet must be joined before the cache tripwire step")
        if join.get("continue-on-error") != "true":
            found.append("the tailnet join must be continue-on-error: true; a failed join costs a cold build, never a red lane")
        if join.get("id") != "join":
            found.append("the tailnet join step must have id: join")
        limit = str(join.get("timeout-minutes", ""))
        if not limit.isdigit() or not 0 < int(limit) <= MAX_JOIN_MINUTES:
            found.append(f"the tailnet join step needs timeout-minutes of {MAX_JOIN_MINUTES} or less; a hanging join eats the build budget")
        if set(join) != {"name", "id", "if", "continue-on-error", "timeout-minutes", "uses", "with"}:
            found.append("the tailnet join step must have exactly name, id, if, continue-on-error, timeout-minutes, uses and with")
    if TRIPWIRE_STEP in names and steps[names.index(TRIPWIRE_STEP)].get("env", {}).get("SIM_TAILNET_JOIN") != "${{ steps.join.outcome }}":
        found.append("the cache tripwire step must pass SIM_TAILNET_JOIN: ${{ steps.join.outcome }}")
    build = steps[names.index("Build")] if "Build" in names else {}
    if any(SECRET_REF.search(str(value)) for _, value in walk(build)):
        found.append("the Build step must hold no secret; it runs the build of the PR and its packages")
    if build.get("run") != BUILD_RUN:
        found.append(f"the Build step must run exactly the pinned script that goes through sh {CACHE_BUILD} build")
    found += full_plan_violations(steps, names)
    for name, doc in docs.items():
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                for line in str(step.get("run", "")).split("\n"):
                    if re.search(r"\bdotnet\s+test\b", line) and "--no-build" not in line:
                        found.append(f"{name}: dotnet test without --no-build would rebuild the restored projects: {line.strip()}")
                    if re.search(r"\bdotnet\s+build\b", line):
                        found.append(f"{name}: dotnet build outside cache_build.sh bypasses the cache: {line.strip()}")
    return found


PLAN_LANES = ('--lane all --emit build > "$RUNNER_TEMP/build.txt"', '--lane unit --emit test > "$RUNNER_TEMP/out/unit.txt"', '--lane integration --emit test > "$RUNNER_TEMP/out/integration.txt"')


def full_plan_violations(steps, names):
    if "Plan" not in names:
        return ["the build job has no Plan step"]
    plan = steps[names.index("Plan")]
    save = next((st for st in steps if WRITER_PREFIX in str(st)), None)
    full = plan.get("env", {}).get("FULL")
    if save is None or full is None:
        return ["the Plan step must take FULL from the same condition as the cache save step"]
    found = []
    for context in (SAVE_OK, *SAVE_REFUSED):
        if (evaluate(full, context) == "1") != truthy(evaluate(condition(str(save.get("if"))), context)):
            found.append(f"the Plan step must plan every project exactly when the cache saves; they differ for {context}")
            break
    run = str(plan.get("run", ""))
    if '[ "$FULL" = 1 ]' not in run or "set -- --all" not in run:
        found.append("the Plan step must pass --all when FULL is 1")
    for tail in PLAN_LANES:
        if f'python3 -m adapters.dotnet.plan "$@" --root "$SIM_ROOT" {tail}' not in run:
            found.append(f"the Plan step must plan {tail}; one build serves every lane")
    return found


def lane_does_not_build_violations(docs):
    found = []
    lane = docs[LANE]
    names = [st.get("name") for st in run_steps(docs, LANE)]
    for step in NOT_IN_A_LANE:
        if step in names:
            found.append(f"{LANE}: the lane has a step named {step}; a lane never builds, plans or touches the cache")
    for path, value in walk(lane):
        text = str(value)
        if BUILDING_TEXT.search(text):
            found.append(f"{LANE}: the lane builds or selects at {'.'.join(map(str, path))}; a lane only tests the build artifact")
        if path[-1:] == ("run",):
            for line in text.split("\n"):
                if DOTNET_WORD.search(line) and "dotnet --version" not in line:
                    found.append(f"{LANE}: the lane runs dotnet other than --version: {line.strip()}")
    if "Unpack" not in names or "Download build" not in names or names.index("Unpack") != names.index("Download build") + 1:
        found.append(f"{LANE}: the Unpack step must follow the Download build step directly")
    if "Download build" not in names or "Hygiene" not in names or names.index("Download build") != names.index("Hygiene") + 1:
        found.append(f"{LANE}: the Download build step must follow Hygiene directly; Hygiene wipes bin and obj")
    work = [i for i, st in enumerate(run_steps(docs, LANE)) if "run" in st and st.get("name") != "Hygiene"]
    if not work or names[work[0]] != "Unpack":
        found.append(f"{LANE}: Unpack must be the first step that runs anything from the head tree")
    download = step_named(docs, LANE, "Download build")
    if (
        set(download) != {"name", "uses", "with"}
        or not str(download.get("uses", "")).startswith("actions/download-artifact@")
        or download.get("with") != {"name": "${{ inputs.artifact }}", "path": "${{ runner.temp }}/build"}
    ):
        found.append(f"{LANE}: the Download build step must download inputs.artifact of this run into runner.temp/build, with no run-id, repository or token")
    zero = step_named(docs, LANE, "Zero tests")
    if not zero or "if" in zero or "continue-on-error" in zero:
        found.append(f"{LANE}: the Zero tests step must run with no if and no continue-on-error")
    if "Zero tests" not in names or "Test" not in names or names.index("Zero tests") < names.index("Test"):
        found.append(f"{LANE}: the Zero tests step must come after the Test step")
    jobs = docs[ENTRY]["jobs"]
    callers = [(name, job_id) for name, doc in docs.items() for job_id, job in doc["jobs"].items() if str(job.get("uses", "")).split(" ")[0] == BUILD_USES]
    if callers != [(ENTRY, "build")]:
        found.append(f"exactly one job, {ENTRY} build, may call {BUILD}; found {callers}")
    for lane_id in lane_jobs_of(docs):
        job = jobs[lane_id]
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        if "build" not in needs:
            found.append(f"{ENTRY}: lane {lane_id} must need the build job")
        if job.get("with", {}).get("artifact") != "${{ needs.build.outputs.artifact }}":
            found.append(f"{ENTRY}: lane {lane_id} must take artifact from needs.build.outputs.artifact, the artifact of this run")
        guard = condition(str(job.get("if", "true")))
        selected = f"needs.validate.outputs.lane_{lane_id}"
        for verdict in ("pass", "test", "infra", "", None):
            for value in ("true", "false", ""):
                runs = truthy(evaluate(guard, {selected: value, "needs.build.outputs.verdict": verdict}))
                if runs != (value == "true" and verdict == "pass"):
                    found.append(f"{ENTRY}: lane {lane_id} runs={runs} for selection {value!r} and build verdict {verdict!r}; it must run only when selected and the build passed")
    return found


def checkout_violations(docs):
    found = []
    for name in WORK:
        checkout = step_named(docs, name, "Checkout")
        if checkout.get("with", {}).get("ref") != "${{ inputs.pr_sha }}":
            found.append(f"{name}: the run job must check out inputs.pr_sha, has {checkout.get('with', {}).get('ref')!r}")
    affected = step_named(docs, BUILD, "Affected")
    if affected.get("env", {}).get("HEAD_SHA") != "${{ inputs.pr_sha }}" or '--head "$HEAD_SHA"' not in str(affected.get("run", "")):
        found.append(f"{BUILD}: the affected selection must use the checked-out head inputs.pr_sha")
    if affected.get("env", {}).get("BASE_SHA") != "${{ inputs.base_sha }}" or '--base "$BASE_SHA"' not in str(affected.get("run", "")):
        found.append(f"{BUILD}: the affected selection must use inputs.base_sha")
    return found


TRUSTED_CHECKOUT = "Checkout trusted"
TRUSTED_OPTIONS = {"path": ".sim-trusted", "persist-credentials": "false", "sparse-checkout": "core"}
DOTNET_SURVIVORS = "dotnet testhost vstest.console VBCSCompiler"
HYGIENE_RUN = """set -u
status=0
if [ -L .sim-trusted ] || [ ! -d .sim-trusted/.git ] || [ "$(git -C .sim-trusted rev-parse HEAD)" != "$GITHUB_SHA" ]; then
    echo "::error::the trusted checkout is not the dispatch commit"
    status=1
else
    sh .sim-trusted/core/prejob.sh || status=$?
fi
rm -rf .sim-trusted
exit "$status"
"""
FIRST_WORK = ("Set up .NET", "Health", "Services", "Affected", "Build", "Test", "Download build", "Unpack")


def hygiene_violations(docs):
    found = []
    for name in WORK:
        steps = run_steps(docs, name)
        names = [st.get("name") for st in steps]
        if "Hygiene" not in names:
            found.append(f"{name}: the run job has no Hygiene step")
            continue
        hygiene = names.index("Hygiene")
        step = steps[hygiene]
        if step.get("run") != HYGIENE_RUN:
            found.append(f"{name}: the Hygiene step must run the trusted copy exactly: sh .sim-trusted/core/prejob.sh, then remove it")
        if step.get("env") != {"SIM_SURVIVORS": DOTNET_SURVIVORS}:
            found.append(f"{name}: the Hygiene step must pass exactly SIM_SURVIVORS: {DOTNET_SURVIVORS}")
        if set(step) != {"name", "if", "env", "run"}:
            found.append(f"{name}: the Hygiene step must have exactly name, if, env and run")
        if not self_hosted_only(step):
            found.append(f"{name}: the Hygiene step must run on self-hosted runners only")
        first_work = min((i for i, st in enumerate(steps) if st.get("name") in FIRST_WORK), default=len(steps))
        if hygiene > first_work:
            found.append(f"{name}: the Hygiene step must come before the first dotnet, artifact or service step")
        if TRUSTED_CHECKOUT not in names:
            found.append(f"{name}: the run job has no {TRUSTED_CHECKOUT} step")
            continue
        trusted = steps[names.index(TRUSTED_CHECKOUT)]
        options = trusted.get("with", {})
        if "ref" in options:
            found.append(f"{name}: the trusted checkout must not take a ref; it is the dispatch commit")
        if not str(trusted.get("uses", "")).startswith("actions/checkout@") or options != TRUSTED_OPTIONS or set(trusted) != {"name", "if", "uses", "with"}:
            found.append(f"{name}: the trusted checkout must be actions/checkout with exactly {TRUSTED_OPTIONS}")
        if not self_hosted_only(trusted):
            found.append(f"{name}: the trusted checkout must run on self-hosted runners only")
        head = names.index("Checkout") if "Checkout" in names else len(steps)
        if not head < names.index(TRUSTED_CHECKOUT) < hygiene:
            found.append(f"{name}: the trusted checkout must come after the head checkout and before Hygiene")
        if hygiene != names.index(TRUSTED_CHECKOUT) + 1:
            found.append(f"{name}: Hygiene must follow the trusted checkout directly; a step between them can change the trusted copy")
    return found


ROUTE_LABEL = "${{ inputs.retest != true && inputs.runner_label || '' }}"
LABELS = {"build": "SIM_LAB_LABEL_BUILD", "unit": "SIM_LAB_LABEL_UNIT", "integration": "SIM_LAB_LABEL_INTEGRATION"}


def runner_violations(docs):
    found = []
    labels = {}
    for job_id, variable in LABELS.items():
        label = docs[ENTRY]["jobs"].get(job_id, {}).get("with", {}).get("runner_label")
        labels[job_id] = label
        if label != "${{ vars." + variable + " }}":
            found.append(f"{ENTRY}: job {job_id} must use its own variable {variable}, has {label}")
    if len(set(labels.values())) != len(labels):
        found.append(f"{ENTRY}: two jobs share one lab label expression")
    for name in CALLED:
        label = docs[name].get("on", {}).get("workflow_call", {}).get("inputs", {}).get("runner_label", {})
        if "opaque" not in str(label.get("description", "")).lower():
            found.append(f"{name}: the runner_label input must say the label is public in logs and has to be opaque")
    for name in WORK:
        jobs = docs[name]["jobs"]
        route = jobs.get("route", {})
        if str(route.get("uses", "")).split(" ")[0] != ROUTE_USES or route.get("with") != {"runner_label": ROUTE_LABEL}:
            found.append(f"{name}: the route job must call {ROUTE} with runner_label: {ROUTE_LABEL} and nothing else")
        given = str(route.get("with", {}).get("runner_label", ""))
        for retest in (True, False):
            for label in ("", "lab"):
                got = evaluate(given, {"inputs.retest": retest, "inputs.runner_label": label})
                want = "" if retest else label
                if not _loose_equal(got, want):
                    found.append(f"{name}: the route is asked about {got!r} for retest {retest} and label {label!r}; a retest always runs on the cloud")
        run = jobs["run"]
        if run.get("needs") != "route" or str(run.get("if")) != "${{ !cancelled() }}":
            found.append(f"{name}: the run job must need route and run unless cancelled, so a skipped route falls back to the cloud")
        expression = str(run.get("runs-on", ""))
        for target, label, want in (("", "lab", HOSTED), ("cloud", "lab", HOSTED), ("onprem", "lab", "lab"), ("onprem", "", HOSTED), ("", "", HOSTED)):
            got = evaluate(expression, {"needs.route.outputs.target": target, "inputs.runner_label": label})
            if got != want:
                found.append(f"{name}: the run job runs on {got!r} for target {target!r} and label {label!r}, wanted {want!r}")
    route = docs[ROUTE]["jobs"].get("route", {})
    runs_on = str(route.get("runs-on", ""))
    if "${{" in runs_on or not runs_on.startswith("ubuntu-"):
        found.append(f"{ROUTE}: route job must run on a GitHub-hosted ubuntu label, has {runs_on!r}")
    if route.get("environment") != "sim-route":
        found.append(f"{ROUTE}: route job must use environment sim-route")
    guard = route.get("if")
    fork = {"github.event_name": "pull_request", "github.repository": "owner/repo", "github.event.pull_request.head.repo.full_name": "stranger/repo"}
    same = dict(fork, **{"github.event.pull_request.head.repo.full_name": "owner/repo"})
    dispatch = {"github.event_name": "workflow_dispatch", "github.repository": "owner/repo"}
    if guard is None:
        found.append(f"{ROUTE}: route job has no fork guard")
    else:
        if truthy(evaluate(condition(guard), fork)):
            found.append(f"{ROUTE}: route job runs for a fork pull request")
        if not truthy(evaluate(condition(guard), same)) or not truthy(evaluate(condition(guard), dispatch)):
            found.append(f"{ROUTE}: route job is skipped for a same-repository run")
    return found


def token_violations(docs):
    found = []
    refs = [(name, path) for name, doc in docs.items() for path, value in walk(doc) if TOKEN_NAME in str(value) or TOKEN_NAME == str(path[-1])]
    outside = [(n, p) for n, p in refs if n != ROUTE or p[:2] != ("jobs", "route")]
    if outside:
        found.append(f"{TOKEN_NAME} is referenced outside the route job: {outside}")
    misplaced = [p for n, p in refs if (n, p[:2]) == (ROUTE, ("jobs", "route")) and (len(p) < 5 or p[2] != "steps" or p[4] != "env")]
    if misplaced:
        found.append(f"{TOKEN_NAME} is used outside a step env: {misplaced}")
    if len({p[:4] for n, p in refs}) != 1:
        found.append(f"{TOKEN_NAME} must appear in exactly one step")
    return found


def fault_violations(docs):
    found = []
    entry_inputs = docs[ENTRY].get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
    options = entry_inputs.get("fault", {}).get("options", [])
    texts = {name: str(docs[name]["jobs"]) for name in SIM_WORKFLOWS}
    for option in options:
        if option != "none" and not any(f"'{option}'" in text for text in texts.values()):
            found.append(f"{ENTRY}: fault option {option} is not referenced by any job")
    for job_id in ("build", *lane_jobs_of(docs)):
        given = docs[ENTRY]["jobs"].get(job_id, {}).get("with", {})
        for option in FAULTS:
            got = evaluate(str(given.get("sim_fault", "")), {"inputs.fault": option})
            if got != ("" if option == "none" else option):
                found.append(f"{ENTRY}: job {job_id} maps fault {option} to sim_fault {got!r}; only none may map to empty")
    fail_test = step_named(docs, BUILD, "Build").get("env", {}).get("FAIL_TEST", "")
    for fault in SIM_FAULTS:
        if (evaluate(str(fail_test), {"inputs.sim_fault": fault}) == "1") != (fault == "fail_test"):
            found.append(f"{BUILD}: the Build step injects the failing test for fault {fault!r}; only fail_test may")
    health = step_named(docs, BUILD, "Health").get("env", {}).get("FAIL_HEALTH", "")
    for fault in SIM_FAULTS:
        for retest in (True, False):
            got = truthy(evaluate(str(health), {"inputs.sim_fault": fault, "inputs.retest": retest}))
            if got != (fault == "fail_health_twice" or fault == "fail_health_once" and not retest):
                found.append(f"{BUILD}: the Health step fails={got} for fault {fault!r} with retest {retest}")
    lane_health = step_named(docs, LANE, "Health").get("env", {}).get("FAIL_HEALTH", "")
    for fault in SIM_FAULTS:
        for lane in ("unit", "integration"):
            for retest in (True, False):
                got = truthy(evaluate(str(lane_health), {"inputs.sim_fault": fault, "inputs.lane": lane, "inputs.retest": retest}))
                if got != (fault == "fail_unit_health_once" and lane == "unit" and not retest):
                    found.append(f"{LANE}: the lane Health step fails={got} for fault {fault!r}, lane {lane} and retest {retest}")
    kill = step_named(docs, LANE, "Test").get("env", {}).get("KILL_RUNNER", "")
    for fault in SIM_FAULTS:
        for target in ("onprem", "cloud", ""):
            got = truthy(evaluate(str(kill), {"inputs.sim_fault": fault, "needs.route.outputs.target": target}))
            if got != (fault == "kill_both" or fault == "kill_onprem" and target == "onprem"):
                found.append(f"{LANE}: the Test step kills the runner={got} for fault {fault!r} on target {target!r}")
    return found


ENTRY_INPUTS = {
    "pr_number": {"type": "string", "default": ""},
    "pr_branch": {"type": "string", "default": "main"},
    "pr_sha": {"type": "string", "default": ""},
    "pr_author": {"type": "string", "default": ""},
    "base": {"type": "string", "default": "origin/main"},
    "stack": {"type": "choice", "default": "dotnet", "options": ["dotnet"]},
    "lanes": {"type": "string", "default": ""},
    "cache": {"type": "choice", "default": "use", "options": ["use", "off"]},
    "fault": {"type": "choice", "default": "none"},
    "retest_of": {"type": "string", "default": ""},
}
UNRESOLVED_INPUTS = re.compile(r"\binputs\.(head|base|pr_branch|pr_sha|pr_number|pr_author|lanes|retest_of)\b")
RESOLVED = ("pr_sha", "base_sha", "default_tip", "pr_number", "pr_branch", "pr_author", "retest_of")
RETEST_FLAG = "${{ needs.validate.outputs.retest_of != '' || github.run_attempt != '1' }}"
SIM_FAULT = "${{ inputs.fault != 'none' && inputs.fault || '' }}"
CALLER_WITH = {
    "pr_number": "${{ needs.validate.outputs.pr_number }}",
    "pr_branch": "${{ needs.validate.outputs.pr_branch }}",
    "pr_author": "${{ needs.validate.outputs.pr_author }}",
    "pr_sha": "${{ needs.validate.outputs.pr_sha }}",
    "base_sha": "${{ needs.validate.outputs.base_sha }}",
    "default_tip": "${{ needs.validate.outputs.default_tip == 'true' }}",
    "cache": "${{ inputs.cache }}",
    "retest": RETEST_FLAG,
    "sim_fault": SIM_FAULT,
}


def lane_jobs_of(docs):
    jobs = docs[ENTRY]["jobs"]
    return [job_id for job_id, job in jobs.items() if str(job.get("uses", "")).split(" ")[0] == LANE_USES]


def known_lanes(docs):
    resolve = next((st for st in docs[ENTRY]["jobs"]["validate"].get("steps", []) if st.get("id") == "resolve"), {})
    return str(resolve.get("env", {}).get("KNOWN_LANES", "")).split()


def entry_contract_violations(docs):
    found = []
    inputs = docs[ENTRY].get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
    if set(inputs) != set(ENTRY_INPUTS):
        return [f"{ENTRY}: dispatch inputs are {sorted(inputs)}, the entry contract is {sorted(ENTRY_INPUTS)}"]
    for name, wanted in ENTRY_INPUTS.items():
        given = inputs[name]
        if str(given.get("required")) != "false":
            found.append(f"{ENTRY}: input {name} must be required: false")
        for key, value in wanted.items():
            if given.get(key) != value:
                found.append(f"{ENTRY}: input {name} has {key} {given.get(key)!r}, the entry contract says {value!r}")
    options = inputs["fault"].get("options", [])
    if options[:1] != ["none"] or set(options) != set(FAULTS) or len(options) != len(set(options)):
        found.append(f"{ENTRY}: fault options are {options}, wanted none first and exactly {sorted(FAULTS)}")
    return found


def run_name_violations(docs):
    found = []
    text = str(docs[ENTRY].get("run-name", ""))
    base = {"inputs.stack": "dotnet", "inputs.pr_branch": "topic", "inputs.base": "origin/main"}
    for retest in (None, "", "1", "42"):
        context = dict(base, **({} if retest is None else {"inputs.retest_of": retest}))
        title = str(evaluate(text, context))
        if retest:
            if not title.endswith("(Retest)") or f"retest of {retest}" not in title:
                found.append(f"{ENTRY}: run-name {title!r} for retest_of {retest} must contain 'retest of {retest}' and end with (Retest)")
        elif "(Retest)" in title or "retest of" in title:
            found.append(f"{ENTRY}: run-name {title!r} without retest_of must not say retest")
    for lanes in (None, "", "unit", "integration,unit"):
        for retest in ("", "7"):
            context = dict(base, **{"inputs.retest_of": retest}, **({} if lanes is None else {"inputs.lanes": lanes}))
            title = str(evaluate(text, context))
            if lanes and f" lanes {lanes}" not in title:
                found.append(f"{ENTRY}: run-name {title!r} must name the selected lanes {lanes}")
            if not lanes and " lanes " in title:
                found.append(f"{ENTRY}: run-name {title!r} must not name lanes when every lane runs")
            if retest and not title.endswith(f" retest of {retest} (Retest)"):
                found.append(f"{ENTRY}: run-name {title!r} must end with ' retest of {retest} (Retest)'")
    return found


GATE_INPUTS = ("pr_number", "pr_branch", "pr_sha", "pr_author", "base_sha", "cache", "fault")


def decide_run(lanes):
    lines = ["set -eu", 'set -- --lane "build=${BUILD_VERDICT}" --reason "build=${BUILD_REASON}"']
    for lane in lanes:
        upper = lane.upper()
        lines += [
            f'case "${upper}_SELECTED" in',
            f'    true) set -- "$@" --lane "{lane}=${{{upper}_VERDICT}}" --reason "{lane}=${{{upper}_REASON}}" --executed "{lane}=${{{upper}_EXECUTED}}" ;;',
            f'    false) set -- "$@" --prior-lane "{lane}=${{{upper}_PRIOR}}" ;;',
            '    *) echo "gate: red [test]: validate did not select lanes"; exit 1 ;;',
            "esac",
        ]
    lines += [
        'case "$RETEST" in',
        '    true) set -- "$@" --retest ;;',
        "    false) ;;",
        '    *) echo "gate: red [test]: the retest flag is neither true nor false"; exit 1 ;;',
        "esac",
        'python3 -m core.gate "$@" --stack "$STACK" --retest-of "$RETEST_OF" \\',
        '    --input "pr_number=${PR_NUMBER}" --input "pr_branch=${PR_BRANCH}" --input "pr_sha=${PR_SHA}" --input "pr_author=${PR_AUTHOR}" \\',
        '    --input "base_sha=${BASE_SHA}" --input "cache=${CACHE}" --input "fault=${FAULT}" --verdict-file "$RUNNER_TEMP/verdict/verdict.json"',
    ]
    return "\n".join(lines) + "\n"


def decide_env(lanes):
    env = {
        "STACK": "${{ inputs.stack }}",
        "CACHE": "${{ inputs.cache }}",
        "FAULT": "${{ inputs.fault }}",
        "RETEST": RETEST_FLAG,
        "RETEST_OF": "${{ needs.validate.outputs.retest_of }}",
        "PR_NUMBER": "${{ needs.validate.outputs.pr_number }}",
        "PR_BRANCH": "${{ needs.validate.outputs.pr_branch }}",
        "PR_SHA": "${{ needs.validate.outputs.pr_sha }}",
        "PR_AUTHOR": "${{ needs.validate.outputs.pr_author }}",
        "BASE_SHA": "${{ needs.validate.outputs.base_sha }}",
        "BUILD_VERDICT": "${{ needs.build.outputs.verdict }}",
        "BUILD_REASON": "${{ needs.build.outputs.reason }}",
    }
    for lane in lanes:
        upper = lane.upper()
        env[f"{upper}_SELECTED"] = f"${{{{ needs.validate.outputs.lane_{lane} }}}}"
        for key in ("verdict", "reason", "executed"):
            env[f"{upper}_{key.upper()}"] = f"${{{{ needs.{lane}.outputs.{key} }}}}"
        env[f"{upper}_PRIOR"] = f"${{{{ needs.validate.outputs.prior_{lane} }}}}"
    return env


VERDICT_ARTIFACT = "sim-gate-verdict-${{ github.run_attempt }}"
VERDICT_UPLOAD = {
    "name": "Upload verdict",
    "if": "${{ always() }}",
    "uses": UPLOAD,
    "with": {"name": VERDICT_ARTIFACT, "path": "${{ runner.temp }}/verdict/verdict.json", "if-no-files-found": "ignore", "retention-days": "1"},
}


def lane_set_violations(docs):
    jobs = docs[ENTRY]["jobs"]
    lanes = lane_jobs_of(docs)
    found = []
    outputs = sorted(key[len("lane_"):] for key in jobs["validate"].get("outputs", {}) if key.startswith("lane_"))
    gate = jobs.get("gate", {})
    steps = gate.get("steps", [])
    decide = next((st for st in steps if st.get("name") == "Decide"), {})
    selected = sorted(key[: -len("_SELECTED")].lower() for key in decide.get("env", {}) if key.endswith("_SELECTED"))
    sets = {"lane jobs": sorted(lanes), "KNOWN_LANES": sorted(known_lanes(docs)), "validate lane_ outputs": outputs, "Decide _SELECTED env": selected}
    if len({tuple(v) for v in sets.values()}) != 1 or not lanes:
        found.append(f"{ENTRY}: the lane names differ between {sets}")
    for lane in lanes:
        if jobs[lane].get("with", {}).get("lane") != lane:
            found.append(f"{ENTRY}: job {lane} must pass lane: {lane}")
    needs = gate.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    if sorted(needs) != sorted(["validate", "build", *lanes]):
        found.append(f"{ENTRY}: the gate must need validate, build and every lane, has {needs}")
    ordered = sorted(lanes, key=list(jobs).index)
    if decide.get("run") != decide_run(ordered) or decide.get("env") != decide_env(ordered) or set(decide) != {"name", "id", "env", "run"} or decide.get("id") != "decide":
        found.append(f"{ENTRY}: the Decide step must be exactly the pinned gate invocation and env for build and the selected lanes")
    names = [st.get("name") for st in steps]
    if names != ["Checkout", "Set up Python", "Decide", "Upload verdict"] or steps[-1] != VERDICT_UPLOAD:
        found.append(f"{ENTRY}: the gate job must be Checkout, Set up Python, Decide, then the pinned Upload verdict of sim-gate-verdict")
    if str(gate.get("if")) != "${{ always() }}":
        found.append(f"{ENTRY}: the gate must run always")
    if gate.get("outputs") != {"retest_lanes": "${{ steps.decide.outputs.retest_lanes }}"}:
        found.append(f"{ENTRY}: the gate job outputs must be exactly retest_lanes from the Decide step")
    return found + report_violations(docs)


COMMENT_JOB = {
    "name": "comment",
    "needs": ["validate", "gate"],
    "if": "${{ always() && needs.validate.outputs.pr_number != '' }}",
    "runs-on": HOSTED,
    "timeout-minutes": "5",
    "permissions": PERMISSION_MAP[(ENTRY, "comment")],
    "steps": [
        {"name": "Checkout", "uses": CHECKOUT, "with": {"persist-credentials": "false"}},
        {"name": "Set up Python", "uses": SETUP_PYTHON, "with": {"python-version": "3.12"}},
        {"name": "Download verdict", "uses": DOWNLOAD, "with": {"name": VERDICT_ARTIFACT, "path": "${{ runner.temp }}/verdict"}},
        {
            "name": "Upsert",
            "env": {
                "GITHUB_TOKEN": "${{ github.token }}",
                "PR_NUMBER": "${{ needs.validate.outputs.pr_number }}",
                "STACK": "${{ inputs.stack }}",
                "GATE_RESULT": "${{ needs.gate.result }}",
                "RUN_URL": "${{ format('{0}/{1}/actions/runs/{2}/attempts/{3}', github.server_url, github.repository, github.run_id, github.run_attempt) }}",
            },
            "run": 'python3 -m core.comment upsert --pr "$PR_NUMBER" --marker "sim-gate:${STACK}" --state "$RUNNER_TEMP/verdict/verdict.json" --run-url "$RUN_URL" --gate-result "$GATE_RESULT"',
        },
    ],
}
REQUEUE_RUN = """set -eu
[ "$GITHUB_REF" = "refs/heads/${GITHUB_REF_NAME}" ]
gh workflow run sim-retest.yml --repo "$GITHUB_REPOSITORY" --ref "$GITHUB_REF_NAME" -f run_id="$GITHUB_RUN_ID"
"""
REQUEUE_JOB = {
    "name": "requeue",
    "needs": ["validate", "gate"],
    "if": "${{ always() && needs.validate.outputs.retest_of == '' && github.run_attempt == '1' && needs.gate.outputs.retest_lanes != '' }}",
    "runs-on": HOSTED,
    "timeout-minutes": "5",
    "permissions": PERMISSION_MAP[(ENTRY, "requeue")],
    "steps": [{"name": "Dispatch the retest stand-in", "env": {"GH_TOKEN": "${{ github.token }}"}, "run": REQUEUE_RUN}],
}
RETEST_JOB = {
    "name": "requeue",
    "runs-on": HOSTED,
    "timeout-minutes": "20",
    "permissions": PERMISSION_MAP[(RETEST, "requeue")],
    "steps": [
        {"name": "Checkout", "uses": CHECKOUT, "with": {"persist-credentials": "false"}},
        {"name": "Set up Python", "uses": SETUP_PYTHON, "with": {"python-version": "3.12"}},
        {
            "name": "Retest",
            "env": {"GITHUB_TOKEN": "${{ github.token }}", "RUN_ID": "${{ inputs.run_id }}"},
            "run": 'python3 -m core.retest_sim run --run-id "$RUN_ID" --workflow sim-gate.yml',
        },
    ],
}
RETEST_INPUTS = {"run_id": {"description": "The sim-gate run to consider for a retest", "required": "true", "type": "string"}}
RETEST_CONCURRENCY = {"group": "${{ format('sim-retest-{0}', inputs.run_id) }}", "cancel-in-progress": "false"}
ENTRY_JOBS = ["validate", "build", "unit", "integration", "gate", "comment", "requeue"]


def report_violations(docs):
    found = []
    jobs = docs[ENTRY]["jobs"]
    if list(jobs) != ENTRY_JOBS:
        found.append(f"{ENTRY}: the jobs are {list(jobs)}, the pinned set and order is {ENTRY_JOBS}")
    if jobs.get("comment") != COMMENT_JOB:
        found.append(f"{ENTRY}: the comment job must be exactly the pinned job: after the gate, only with a PR number, one upsert of this run's verdict")
    if jobs.get("requeue") != REQUEUE_JOB:
        found.append(f"{ENTRY}: the requeue job must be exactly the pinned job: first attempt of a run that is not a retest, with retest lanes, one stand-in dispatch on this branch")
    retest = docs[RETEST]
    if list(retest.get("jobs", {})) != ["requeue"] or retest["jobs"].get("requeue") != RETEST_JOB:
        found.append(f"{RETEST}: the stand-in must be exactly the one pinned requeue job running core.retest_sim against sim-gate.yml")
    if retest.get("concurrency") != RETEST_CONCURRENCY:
        found.append(f"{RETEST}: the stand-in concurrency must be exactly {RETEST_CONCURRENCY}, keyed on run_id")
    if retest.get("on", {}).get("workflow_dispatch", {}).get("inputs") != RETEST_INPUTS:
        found.append(f"{RETEST}: the stand-in takes exactly one required input, run_id")
    return found


PRIOR_CHECK = "Check the previous run"


def prior_step(lanes):
    env = {
        "GITHUB_TOKEN": "${{ github.token }}",
        "RETEST_OF": "${{ steps.resolve.outputs.retest_of }}",
        "STACK": "${{ inputs.stack }}",
        **{f"{lane.upper()}_SELECTED": f"${{{{ steps.resolve.outputs.lane_{lane} }}}}" for lane in lanes},
    }
    selected = " ".join(f'--selected "{lane}=${{{lane.upper()}_SELECTED}}"' for lane in lanes)
    return {
        "name": PRIOR_CHECK,
        "id": "prior",
        "if": "${{ steps.resolve.outputs.retest_of != '' }}",
        "env": env,
        "run": f'python3 -m core.prior check --run-id "$RETEST_OF" --workflow sim-gate.yml --stack "$STACK" {selected}',
    }


def validate_violations(docs):
    jobs = docs[ENTRY]["jobs"]
    validate = jobs["validate"]
    found = []
    steps = validate.get("steps", [])
    checkouts = [i for i, st in enumerate(steps) if str(st.get("uses", "")).startswith("actions/checkout@")]
    resolves = [i for i, st in enumerate(steps) if st.get("id") == "resolve"]
    options = steps[checkouts[0]].get("with", {}) if checkouts else {}
    if not checkouts or "ref" in options or str(options.get("fetch-depth")) != "0" or str(options.get("persist-credentials")) != "false":
        found.append("validate must check out the dispatch commit itself with fetch-depth: 0 and persist-credentials: false")
    wanted_env = {
        "PR_BRANCH": "${{ inputs.pr_branch }}",
        "PR_SHA": "${{ inputs.pr_sha }}",
        "PR_NUMBER": "${{ inputs.pr_number }}",
        "PR_AUTHOR": "${{ inputs.pr_author }}",
        "BASE_REF": "${{ inputs.base }}",
        "RETEST_OF": "${{ inputs.retest_of }}",
        "LANES": "${{ inputs.lanes }}",
        "KNOWN_LANES": " ".join(known_lanes(docs)),
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
    }
    resolve = steps[resolves[0]] if resolves else {}
    if str(resolve.get("run", "")).strip() != "sh core/resolve_refs.sh" or resolve.get("env") != wanted_env:
        found.append("validate must resolve every PR input with sh core/resolve_refs.sh")
    elif not checkouts or resolves[0] < checkouts[0]:
        found.append("validate must check out before it resolves")
    names = [*RESOLVED, *(f"lane_{lane}" for lane in known_lanes(docs))]
    outputs = {name: f"${{{{ steps.resolve.outputs.{name} }}}}" for name in names}
    outputs.update({f"prior_{lane}": f"${{{{ steps.prior.outputs.prior_{lane} }}}}" for lane in known_lanes(docs)})
    if validate.get("outputs") != outputs:
        found.append(f"validate must output {', '.join(names)} from the resolve step and the prior lanes from the prior check")
    if [st.get("name") for st in steps] != ["Checkout", "Resolve refs", "Set up Python", PRIOR_CHECK] or steps[-1] != prior_step(known_lanes(docs)):
        found.append(f"validate must be Checkout, Resolve refs, Set up Python, then the pinned {PRIOR_CHECK} step, only on a retest")
    for job_id in ("build", *lane_jobs_of(docs)):
        given = jobs.get(job_id, {}).get("with", {})
        for key, wanted in CALLER_WITH.items():
            if given.get(key) != wanted:
                found.append(f"{ENTRY}: job {job_id} must pass {key}: {wanted}, has {given.get(key)!r}")
    build_needs = jobs.get("build", {}).get("needs")
    if build_needs != "validate":
        found.append(f"{ENTRY}: the build job must need validate")
    for job_id, job in jobs.items():
        if job_id == "validate":
            continue
        for path, value in walk(job):
            if UNRESOLVED_INPUTS.search(str(value)):
                found.append(f"{ENTRY}: job {job_id} reads an unresolved PR input at {'.'.join(map(str, path))}; use needs.validate.outputs")
    return found + entry_contract_violations(docs) + run_name_violations(docs) + lane_set_violations(docs)


def permission_violations(docs):
    found = []
    for name, doc in docs.items():
        for job_id, job in doc.get("jobs", {}).items():
            wanted = PERMISSION_MAP.get((name, job_id))
            if job.get("permissions") != wanted:
                found.append(f"{name}: job {job_id} permissions are {job.get('permissions')}, the permission map says {wanted or 'inherit contents: read'}")
    for name, job_id in sorted(PERMISSION_MAP):
        if job_id not in docs.get(name, {}).get("jobs", {}):
            found.append(f"{name}: the permission map names job {job_id}, which does not exist")
    return found


WRITER_TEXT = re.compile(r"sim[-_]cache[-_]write", re.I)
YAML_ESCAPE = re.compile(r"\\[xuUN]")
SECRETS_WHOLE = re.compile(r"toJSON\s*\(\s*secrets\s*\)|\bsecrets\s*\[", re.I)
THIRD_PARTY_MODULES = {"pytest", "pip"}
EXPECTED_MODULES = {"core.route", "core.classify", "core.gate", "core.zero_tests", "core.comment", "core.retest_sim", "core.prior", "adapters.dotnet.affected", "adapters.dotnet.plan"}
EXPECTED_SCRIPTS = {
    "core/resolve_refs.sh",
    "core/prejob.sh",
    "core/kill_own_runner.sh",
    "adapters/dotnet/cache_build.sh",
    "adapters/dotnet/build_artifact.sh",
    "adapters/dotnet/test_lane.sh",
}
PYTHON_MODULE = re.compile(r"\bpython3?(?:\s+-[A-Za-z]+)*\s+-m\s+([A-Za-z_][\w.]*)")
PYTHON_FILE = re.compile(r"\bpython3?\s+([^\s\-][^\s;|&)]*\.py)\b")
SHELL_SCRIPT = re.compile(r"""(?:^|[\s;&|(])(?:sh|bash|\.)\s+(?:-[A-Za-z]+\s+)*["']?([^\s"';|&)]+\.sh)\b""")
DIRECT_SCRIPT = re.compile(r"""(?:^|[\s;&|(])["']?(\./[^\s"';|&)]+\.sh)\b""")
SCRIPT_PREFIXES = ("$GITHUB_WORKSPACE/", "${GITHUB_WORKSPACE}/", ".sim-trusted/", "./")


def workflow_files(directory):
    return sorted(p for p in Path(directory).iterdir() if p.suffix in (".yml", ".yaml"))


def all_files_violations(directory):
    found = []
    files = workflow_files(directory)
    names = {p.name for p in files}
    if names != SIM_WORKFLOWS | OTHER_WORKFLOWS:
        found.append(f"workflow directory holds {sorted(names)}, expected {sorted(SIM_WORKFLOWS | OTHER_WORKFLOWS)}; a new workflow needs a policy entry")
    for path in files:
        text = path.read_text(encoding="utf-8")
        if YAML_ESCAPE.search(text):
            found.append(f"{path.name}: YAML escape sequences are refused; they hide names from this policy")
        if SECRETS_WHOLE.search(text):
            found.append(f"{path.name}: the secrets context may only be read by name")
        if path.name not in SIM_WORKFLOWS and WRITER_TEXT.search(text):
            found.append(f"{path.name}: names the cache writer key or environment; only the build save step may")
        try:
            doc = parse(text)
        except Unsupported as exc:
            found.append(f"{path.name}: the policy cannot read this workflow: {exc}")
            continue
        for where, value in walk(doc):
            dotted = ".".join(map(str, where))
            if where[-1:] == ("secrets",) and value == "inherit":
                found.append(f"{path.name}: secrets: inherit at {dotted}")
            if where[-1:] == ("run",) and "${{" in str(value):
                found.append(f"{path.name}: expression inside run at {dotted}")
            if "environment" in where and (where[:1] != ("jobs",) or (path.name, where[1]) not in ENVIRONMENT_PLACES):
                found.append(f"{path.name}: environment at {dotted} is not in the environment allowlist")
            for item in (str(value), *map(str, where)):
                if WRITER_TEXT.search(item) and not writer_place_allowed(path.name, where, doc):
                    found.append(f"{path.name}: the cache writer key or environment is named at {dotted}")
                    break
    return found


def writer_place_allowed(name, where, doc):
    if name != BUILD or where[:2] != ("jobs", "run"):
        return False
    if where[2:] == ("environment",):
        return True
    if len(where) == 6 and where[2] == "steps" and where[4] == "env" and str(where[5]).startswith(WRITER_PREFIX):
        step = doc["jobs"]["run"]["steps"][where[3]]
        return step.get("name") == SAVE_STEP and step.get("run") == SAVE_RUN
    return False


def resolves_module(root, module):
    base = Path(root, *module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__main__.py").is_file()


def script_path(raw):
    for prefix in SCRIPT_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw


def invocation_violations(directory, root):
    found, modules, scripts = [], set(), set()
    for path in workflow_files(directory):
        for where, value in walk(parse(path.read_text(encoding="utf-8"))):
            text = str(value)
            if where[-1:] == ("uses",) and text.startswith("./"):
                local = text.split(" ")[0]
                if not Path(root, local).is_file():
                    found.append(f"{path.name}: uses {local}, which does not exist")
            if where[-1:] != ("run",):
                continue
            text = re.sub(r"\\\n\s*", " ", text)
            for module in PYTHON_MODULE.findall(text):
                modules.add(module)
                if module not in THIRD_PARTY_MODULES and not resolves_module(root, module):
                    found.append(f"{path.name}: python -m {module} does not resolve to a module in this tree")
            for raw in PYTHON_FILE.findall(text) + SHELL_SCRIPT.findall(text) + DIRECT_SCRIPT.findall(text):
                script = script_path(raw)
                scripts.add(script)
                if "$" in script or not Path(root, script).is_file():
                    found.append(f"{path.name}: {raw} does not resolve to a file in this tree")
    for missing in sorted(EXPECTED_MODULES - modules):
        found.append(f"no workflow runs python -m {missing}; the invocation scan lost it")
    for missing in sorted(EXPECTED_SCRIPTS - scripts):
        found.append(f"no workflow runs {missing}; the invocation scan lost it")
    return found


def lane_script_violations(root):
    path = Path(root, "adapters", "dotnet", "test_lane.sh")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ["adapters/dotnet/test_lane.sh is missing; the lane has no test runner"]
    calls = [line for line in text.split("\n") if re.search(r"\bset -- test\b", line)]
    if not calls or any("--no-build" not in line for line in calls):
        return ["adapters/dotnet/test_lane.sh must pass --no-build to every dotnet test it builds a command for"]
    return []


def budget_violations(docs):
    found = []
    total = (
        minutes(docs, ENTRY, "validate")
        + sum(minutes(docs, ROUTE, "route") + minutes(docs, name, "run") + minutes(docs, name, "classify") for name in WORK)
        + minutes(docs, ENTRY, "gate")
    )
    if total > BUDGET_MINUTES:
        found.append(f"worst-case critical path is {total} minutes, budget {BUDGET_MINUTES}")
    for name in WORK:
        if minutes(docs, name, "run") > MAX_RUN_MINUTES:
            found.append(f"{name}: one run may take longer than the per-run cap of {MAX_RUN_MINUTES} minutes")
    return found


def dotnet_setup_violations(docs):
    found = []
    for name, doc in docs.items():
        for job_id, job in doc["jobs"].items():
            steps = job.get("steps", [])
            first_dotnet = next((i for i, st in enumerate(steps) if DOTNET_WORD.search(str(st.get("run", "")))), None)
            if first_dotnet is None:
                continue
            setup = next((i for i, st in enumerate(steps) if str(st.get("uses", "")).startswith("actions/setup-dotnet@")), None)
            if setup is None or setup > first_dotnet:
                found.append(f"{name}: job {job_id} runs dotnet without a prior actions/setup-dotnet step")
            elif steps[setup].get("with", {}).get("global-json-file") != SOLUTION_GLOBAL_JSON:
                found.append(f"{name}: job {job_id} setup-dotnet must use global-json-file {SOLUTION_GLOBAL_JSON}")
    return found


def violations(directory, root=None):
    docs = load(directory)
    found = []
    if set(docs) != SIM_WORKFLOWS:
        return [f"workflow set is {sorted(docs)}, expected {sorted(SIM_WORKFLOWS)}"]
    found += all_files_violations(directory)
    if root is not None:
        found += invocation_violations(directory, root)
        found += lane_script_violations(root)
    found += permission_violations(docs)

    for name, doc in docs.items():
        for trigger in sorted(triggers(doc) & FORBIDDEN_TRIGGERS):
            found.append(f"{name}: forbidden trigger {trigger}")
        if name in DISPATCH_ONLY and triggers(doc) != {"workflow_dispatch"}:
            found.append(f"{name}: must be workflow_dispatch only, has {sorted(triggers(doc))}")
        if name in CALLED and triggers(doc) != {"workflow_call"}:
            found.append(f"{name}: must be workflow_call only, has {sorted(triggers(doc))}")
        if doc.get("permissions") != {"contents": "read"}:
            found.append(f"{name}: workflow permissions must be contents: read only")
        for path, value in walk(doc):
            text = str(value)
            if "permissions" in path and value not in ("read", "none") and not (
                value == "write" and (name, path) in WRITE_PLACES
            ):
                found.append(f"{name}: permission {'.'.join(map(str, path))} is {value}")
            if "github.ref_name" in text:
                found.append(f"{name}: github.ref_name is refused at {'.'.join(map(str, path))}; a tag can carry the branch name, compare github.ref")
            if path[-1:] == ("continue-on-error",) and not (
                name == BUILD and path[:3] == ("jobs", "run", "steps") and len(path) == 5
                and doc["jobs"]["run"]["steps"][path[3]].get("name") == JOIN_STEP
            ):
                found.append(f"{name}: continue-on-error at {'.'.join(map(str, path))}")
            if path[-1:] == ("secrets",) and value == "inherit":
                found.append(f"{name}: secrets: inherit at {'.'.join(map(str, path))}")
            if SECRETISH_VARIABLE.search(text):
                found.append(f"{name}: a secret-looking name is read from vars at {'.'.join(map(str, path))}")
            if path[-1:] == ("run",) and "${{" in text:
                found.append(f"{name}: expression inside run at {'.'.join(map(str, path))}")
            if path[-1:] == ("uses",) and not text.startswith(("./", "$/")) and not PINNED.match(text.split(" ")[0]):
                found.append(f"{name}: action not pinned to a full SHA: {text}")

    found += token_violations(docs)
    found += runner_violations(docs)
    found += validate_violations(docs)
    found += checkout_violations(docs)
    found += secret_violations(docs)
    found += hygiene_violations(docs)
    found += pinned_step_violations(docs)
    found += cache_wiring_violations(docs)
    found += lane_does_not_build_violations(docs)
    found += fault_violations(docs)
    for name, doc in docs.items():
        writer_refs = {
            path[:4] if len(path) > 3 and path[2] == "steps" else path[:2]
            for path, value in walk(doc)
            if WRITER_PREFIX in str(value) or WRITER_PREFIX in str(path[-1])
        }
        for where in sorted(writer_refs, key=str):
            if where[0] != "jobs":
                found.append(f"{name}: {WRITER_PREFIX}* is referenced outside a job gated on the default branch: {where}")
                continue
            job = doc["jobs"][where[1]]
            guards = [job.get("if")]
            if len(where) == 4:
                guards.append(job["steps"][where[3]].get("if"))
            if not any(g is not None and default_branch_only(str(g)) for g in guards):
                found.append(f"{name}: {WRITER_PREFIX}* is referenced outside a job gated on the default branch: {where}")
    found += budget_violations(docs)
    found += dotnet_setup_violations(docs)
    found += environment_violations(docs)
    return found
