from __future__ import annotations

import re
from pathlib import Path

TOKEN_NAME = "SIM_ROUTE_TOKEN"
WRITER_PREFIX = "SIM_CACHE_WRITE"
FORBIDDEN_TRIGGERS = {"pull_request_target", "workflow_run", "issue_comment"}
SECRETISH_VARIABLE = re.compile(r"\bvars\.[A-Za-z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|ENDPOINT)", re.I)
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
BUDGET_MINUTES = 80
MAX_ATTEMPT_MINUTES = 30
DOTNET_GLOBAL_JSON = "sim/dotnet/global.json"
CACHE_BUILD = "adapters/dotnet/cache_build.sh"


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
    return {
        p.name: parse(p.read_text(encoding="utf-8"))
        for p in sorted(Path(directory).glob("sim-gate*.yml"))
    }


def triggers(doc):
    on = doc.get("on", {})
    return set(on) if isinstance(on, (dict, list)) else {on}


def minutes(docs, workflow, job):
    return int(docs[workflow]["jobs"][job]["timeout-minutes"])


SECRET_REF = re.compile(r"""\bsecrets\s*[.\[]\s*['"]?([A-Za-z0-9_]+)""")
READER_PREFIX = "SIM_CACHE_READ"


ENDPOINT_NAMES = ("SIM_CACHE_ENDPOINT_LAB", "SIM_CACHE_ENDPOINT_TAILNET")
TAILNET_INPUTS = {"SIM_TAILNET_CLIENT_ID": "oauth-client-id", "SIM_TAILNET_AUDIENCE": "audience"}
ATTEMPT = "sim-gate-attempt.yml"
ID_TOKEN_PLACES = {
    ("sim-gate.yml", ("jobs", "unit", "permissions")),
    ("sim-gate.yml", ("jobs", "integration", "permissions")),
    ("sim-gate-lane.yml", ("jobs", "attempt", "permissions")),
    ("sim-gate-lane.yml", ("jobs", "retry", "permissions")),
    (ATTEMPT, ("jobs", "run", "permissions")),
}
WRITE_ENVIRONMENT = "sim-cache-write"
READ_ENVIRONMENT = "sim-cache-read"
LAB_ENVIRONMENT = "sim-cache-lab"
JOIN_STEP = "Join the tailnet"
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


def in_attempt_run_step(name, path, section):
    return name == ATTEMPT and path[:2] == ("jobs", "run") and len(path) >= 6 and path[2] == "steps" and path[4] == section


def secret_allowed(name, path, secret, value):
    key = str(path[-1])
    exact = value.strip() == "${{ secrets." + secret + " }}" and key == secret
    if secret == TOKEN_NAME:
        return (
            exact
            and name == "sim-gate-lane.yml"
            and path[:2] == ("jobs", "route")
            and path[2] == "steps"
            and path[4:5] == ("env",)
        )
    if secret in TAILNET_INPUTS:
        return value.strip() == "${{ secrets." + secret + " }}" and key == TAILNET_INPUTS[secret] and in_attempt_run_step(name, path, "with")
    if secret in ENDPOINT_NAMES or secret.startswith((READER_PREFIX, WRITER_PREFIX)):
        return exact and in_attempt_run_step(name, path, "env")
    return False


def secret_violations(docs):
    found = []
    for name, doc in docs.items():
        for path, value in walk(doc):
            where = ".".join(map(str, path))
            for match in SECRET_REF.finditer(str(value)):
                if not secret_allowed(name, path, match.group(1), str(value)):
                    found.append(f"{name}: secret {match.group(1)} is read at a place that is not allowed: {where}")
            if "secrets" in path:
                found.append(f"{name}: no secrets mapping or declaration is allowed, secrets are read in the attempt step env only: {where}")
    return found


SAVE_BASE = {"github.event.repository.default_branch": "main", "inputs.fail_test": False, "runner.environment": "github-hosted"}
SAVE_OK = dict(SAVE_BASE, **{"github.ref": "refs/heads/main", "github.ref_name": "main", "inputs.head": FULL_SHA, "inputs.default_tip": True})
SAVE_REFUSED = [
    dict(SAVE_OK, **{"github.ref": "refs/heads/topic", "github.ref_name": "topic"}),
    dict(SAVE_OK, **{"github.ref": "refs/tags/main", "github.ref_name": "main"}),
    dict(SAVE_OK, **{"inputs.default_tip": False}),
    dict(SAVE_OK, **{"inputs.head": "main", "inputs.default_tip": False}),
    dict(SAVE_OK, **{"inputs.head": "MAIN", "inputs.default_tip": False}),
    dict(SAVE_OK, **{"inputs.head": "topic", "inputs.default_tip": False}),
    dict(SAVE_OK, **{"runner.environment": "self-hosted"}),
]
NAME_COMPARE = "inputs.head"


def default_branch_only(guard):
    return truthy(evaluate(condition(guard), SAVE_OK)) and not any(
        truthy(evaluate(condition(guard), context)) for context in SAVE_REFUSED
    )


def hosted_only(guard):
    return truthy(evaluate(condition(guard), {"runner.environment": "github-hosted"})) and not truthy(
        evaluate(condition(guard), {"runner.environment": "self-hosted"})
    )


def environment_violations(docs):
    found = []
    run = docs[ATTEMPT]["jobs"]["run"]
    expression = run.get("environment")
    if not isinstance(expression, str):
        return ["the attempt run job must choose its environment with an expression"]
    if NAME_COMPARE in expression:
        found.append("the attempt environment must not compare inputs.head by name; names compare case-insensitively, use inputs.default_tip")
    base = {"github.event.repository.default_branch": "main"}
    for ref in ("refs/heads/main", "refs/heads/topic", "refs/tags/main", "refs/heads/main2"):
        for head in ("main", "MAIN", "topic", FULL_SHA):
            for default_tip in (False, True):
                for fail_test in (False, True):
                    for hosted in (True, False):
                        context = dict(base, **{"github.ref": ref, "github.ref_name": ref.rsplit("/", 1)[-1], "inputs.head": head, "inputs.default_tip": default_tip, "inputs.fail_test": fail_test, "inputs.hosted": hosted})
                        chosen = evaluate(expression, context)
                        if not hosted:
                            wanted = LAB_ENVIRONMENT
                        elif (ref, default_tip, fail_test) == ("refs/heads/main", True, False):
                            wanted = WRITE_ENVIRONMENT
                        else:
                            wanted = READ_ENVIRONMENT
                        if chosen != wanted:
                            found.append(
                                f"the attempt environment is {chosen!r} for ref {ref}, head {head}, default_tip {default_tip}, fail_test {fail_test}, hosted {hosted}; wanted {wanted!r}"
                            )
    if run.get("permissions") != {"contents": "read", "id-token": "write"}:
        found.append("the attempt run job must have exactly contents: read and id-token: write")
    classify = docs[ATTEMPT]["jobs"]["classify"]
    if "id-token" in classify.get("permissions", {}) or "environment" in classify:
        found.append("the classify job must have neither id-token nor an environment")
    lane = docs["sim-gate-lane.yml"]["jobs"]
    for target in ("", "cloud", "onprem"):
        for label in ("", "lab"):
            context = {"needs.route.outputs.target": target, "inputs.lab_label": label}
            runner = evaluate(lane["attempt"]["with"]["runner"], context)
            if truthy(evaluate(lane["attempt"]["with"]["hosted"], context)) != (runner == "ubuntu-24.04"):
                found.append(f"the hosted input disagrees with the runner for target {target!r} and label {label!r}")
    if str(lane["retry"]["with"]["hosted"]) != "true" or str(lane["retry"]["with"]["runner"]) != "ubuntu-24.04":
        found.append("the retry must be hosted")
    for name, doc in docs.items():
        for job_id, job in doc["jobs"].items():
            if any(SECRET_REF.search(str(value)) for _, value in walk(job)) and "environment" not in job:
                found.append(f"{name}: job {job_id} reads secrets without an environment, so they would be empty in a called workflow")
    return found


def cache_wiring_violations(docs):
    found = []
    steps = docs[ATTEMPT]["jobs"]["run"]["steps"]
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
        if WRITER_PREFIX in str(step):
            guard = step.get("if")
            if NAME_COMPARE in str(guard):
                found.append("the cache save guard must not compare inputs.head by name; names compare case-insensitively, use inputs.default_tip")
            if guard is None or not default_branch_only(str(guard)):
                found.append("a step reading the cache writer key must itself be gated on the default branch")
            elif truthy(evaluate(condition(str(guard)), dict(SAVE_OK, **{"inputs.fail_test": True}))):
                found.append("the cache save step must not run when fail_test is on")
            elif not truthy(evaluate(condition(str(guard)), SAVE_OK)):
                found.append("the cache save step never runs on the default branch with fail_test off")
            if step.get("env", {}).get("SIM_DEFAULT_BRANCH") != "${{ github.event.repository.default_branch }}":
                found.append("the save step must pass the default branch name from github.event.repository.default_branch")
            if f"sh {CACHE_BUILD} save" not in str(step.get("run", "")):
                found.append(f"the step holding the cache writer key must only run sh {CACHE_BUILD} save")
    if "Cache tripwire" not in names or "Build" not in names or names.index("Cache tripwire") > names.index("Build"):
        found.append("the cache tripwire step must come before the Build step")
    elif f"sh {CACHE_BUILD} tripwire" not in str(steps[names.index("Cache tripwire")].get("run", "")):
        found.append(f"the cache tripwire step must run sh {CACHE_BUILD} tripwire")
    for early in ("Health", "Services", "Affected", "Plan"):
        if early in names and "Cache tripwire" in names and names.index(early) > names.index("Cache tripwire"):
            found.append(f"the cache tripwire step must come after {early}")
    if "Join the tailnet" in names and "Cache tripwire" in names and names.index("Join the tailnet") > names.index("Cache tripwire"):
        found.append("the tailnet must be joined before the cache tripwire step")
    if JOIN_STEP not in names:
        found.append("the attempt job has no step that joins the tailnet on hosted runners")
    else:
        join = steps[names.index(JOIN_STEP)]
        if join.get("continue-on-error") != "true":
            found.append("the tailnet join must be continue-on-error: true; a failed join costs a cold build, never a red lane")
        if join.get("id") != "join":
            found.append("the tailnet join step must have id: join")
    if "Cache tripwire" in names and steps[names.index("Cache tripwire")].get("env", {}).get("SIM_TAILNET_JOIN") != "${{ steps.join.outcome }}":
        found.append("the cache tripwire step must pass SIM_TAILNET_JOIN: ${{ steps.join.outcome }}")
    found += full_plan_violations(steps, names)
    build = steps[names.index("Build")] if "Build" in names else {}
    if f"sh {CACHE_BUILD} build" not in str(build.get("run", "")):
        found.append(f"the Build step must go through sh {CACHE_BUILD} build")
    for name, doc in docs.items():
        for job_id, job in doc["jobs"].items():
            for step in job.get("steps", []):
                for line in str(step.get("run", "")).split("\n"):
                    if re.search(r"\bdotnet\s+test\b", line) and "--no-build" not in line:
                        found.append(f"{name}: dotnet test without --no-build would rebuild restored projects: {line.strip()}")
                    if re.search(r"\bdotnet\s+build\b", line):
                        found.append(f"{name}: dotnet build outside cache_build.sh bypasses the cache: {line.strip()}")
    return found


def full_plan_violations(steps, names):
    if "Plan" not in names:
        return ["the attempt job has no Plan step"]
    plan = steps[names.index("Plan")]
    save = next((st for st in steps if WRITER_PREFIX in str(st)), None)
    full = plan.get("env", {}).get("FULL")
    if save is None or full is None:
        return ["the Plan step must take FULL from the same condition as the cache save step"]
    found = []
    for context in (SAVE_OK, *SAVE_REFUSED, dict(SAVE_OK, **{"inputs.fail_test": True})):
        if (evaluate(full, context) == "1") != truthy(evaluate(condition(str(save.get("if"))), context)):
            found.append(f"the Plan step must plan the whole lane exactly when the cache saves; they differ for {context}")
            break
    run = str(plan.get("run", ""))
    if '[ "$FULL" = 1 ]' not in run or "set -- --all" not in run:
        found.append("the Plan step must pass --all when FULL is 1")
    return found


def validate_violations(docs):
    jobs = docs["sim-gate.yml"]["jobs"]
    validate = jobs["validate"]
    found = []
    steps = validate.get("steps", [])
    checkouts = [i for i, st in enumerate(steps) if str(st.get("uses", "")).startswith("actions/checkout@")]
    resolves = [i for i, st in enumerate(steps) if st.get("id") == "resolve"]
    options = steps[checkouts[0]].get("with", {}) if checkouts else {}
    if not checkouts or "ref" in options or str(options.get("fetch-depth")) != "0" or str(options.get("persist-credentials")) != "false":
        found.append("validate must check out the dispatch commit itself with fetch-depth: 0 and persist-credentials: false")
    wanted_env = {
        "HEAD_REF": "${{ inputs.head }}",
        "BASE_REF": "${{ inputs.base }}",
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
    }
    resolve = steps[resolves[0]] if resolves else {}
    if str(resolve.get("run", "")).strip() != "sh core/resolve_refs.sh" or resolve.get("env") != wanted_env:
        found.append("validate must resolve head and base with sh core/resolve_refs.sh")
    elif not checkouts or resolves[0] < checkouts[0]:
        found.append("validate must check out before it resolves")
    outputs = {name: f"${{{{ steps.resolve.outputs.{name} }}}}" for name in ("head_sha", "base_sha", "default_tip")}
    if validate.get("outputs") != outputs:
        found.append("validate must output head_sha, base_sha and default_tip from the resolve step")
    wanted_with = {
        "head": "${{ needs.validate.outputs.head_sha }}",
        "base": "${{ needs.validate.outputs.base_sha }}",
        "default_tip": "${{ needs.validate.outputs.default_tip == 'true' }}",
    }
    for lane in ("unit", "integration"):
        given = {key: jobs[lane].get("with", {}).get(key) for key in wanted_with}
        if given != wanted_with:
            found.append(f"lane {lane} must take head, base and default_tip from validate only, has {given}")
    for job_id, job in jobs.items():
        if job_id == "validate":
            continue
        for path, value in walk(job):
            if re.search(r"\binputs\.(head|base)\b", str(value)):
                found.append(f"sim-gate.yml: job {job_id} reads the unresolved ref at {'.'.join(map(str, path))}; use needs.validate.outputs")
    lane_jobs = docs["sim-gate-lane.yml"]["jobs"]
    for caller in ("attempt", "retry"):
        given = lane_jobs[caller].get("with", {})
        if given.get("head") != "${{ inputs.head }}" or given.get("default_tip") != "${{ inputs.default_tip }}":
            found.append(f"the lane {caller} must pass head and default_tip through unchanged")
    return found


def hygiene_violations(docs):
    steps = docs["sim-gate-attempt.yml"]["jobs"]["run"]["steps"]
    hygiene = next((i for i, st in enumerate(steps) if st.get("name") == "Hygiene"), None)
    if hygiene is None:
        return ["the attempt run job has no Hygiene step"]
    found = []
    step = steps[hygiene]
    if step.get("run", "").strip() != "sh core/prejob.sh":
        found.append("the Hygiene step must run sh core/prejob.sh")
    guard = condition(str(step["if"])) if "if" in step else "${{ true }}"
    if not truthy(evaluate(guard, {"runner.environment": "self-hosted"})) or truthy(evaluate(guard, {"runner.environment": "github-hosted"})):
        found.append("the Hygiene step must run on self-hosted runners only")
    first_work = min(i for i, st in enumerate(steps) if st.get("name") in ("Set up .NET", "Health", "Services", "Affected", "Build", "Test"))
    if hygiene > first_work:
        found.append("the Hygiene step must come before the first dotnet or service step")
    return found


def lane_label_violations(docs):
    jobs = docs["sim-gate.yml"]["jobs"]
    labels = {lane: jobs[lane]["with"]["lab_label"] for lane in ("unit", "integration")}
    found = []
    if labels["unit"] == labels["integration"]:
        found.append("the unit and integration lanes share one lab label expression")
    for lane, label in labels.items():
        if label != "${{ vars.SIM_LAB_LABEL_" + lane.upper() + " }}":
            found.append(f"lane {lane} must use its own variable SIM_LAB_LABEL_{lane.upper()}, has {label}")
    return found


def checkout_violations(docs):
    steps = docs["sim-gate-attempt.yml"]["jobs"]["run"]["steps"]
    checkout = next(st for st in steps if st.get("name") == "Checkout")
    affected = next(st for st in steps if st.get("name") == "Affected")
    found = []
    ref = checkout.get("with", {}).get("ref")
    head = affected.get("env", {}).get("HEAD_REF")
    if ref is None or ref != head or "--head \"$HEAD_REF\"" not in affected.get("run", ""):
        found.append(f"the attempt checkout ref {ref!r} must equal the head given to affected {head!r}")
    return found


def violations(directory):
    docs = load(directory)
    found = []
    expected = {"sim-gate.yml", "sim-gate-lane.yml", "sim-gate-attempt.yml"}
    if set(docs) != expected:
        return [f"workflow set is {sorted(docs)}, expected {sorted(expected)}"]

    for name, doc in docs.items():
        for trigger in sorted(triggers(doc) & FORBIDDEN_TRIGGERS):
            found.append(f"{name}: forbidden trigger {trigger}")
        if name == "sim-gate.yml" and triggers(doc) != {"workflow_dispatch"}:
            found.append(f"{name}: must be workflow_dispatch only, has {sorted(triggers(doc))}")
        if doc.get("permissions") != {"contents": "read"}:
            found.append(f"{name}: workflow permissions must be contents: read only")
        for path, value in walk(doc):
            text = str(value)
            if "permissions" in path and value not in ("read", "none") and not (
                value == "write" and path[-1] == "id-token" and (name, path[:-1]) in ID_TOKEN_PLACES
            ):
                found.append(f"{name}: permission {'.'.join(map(str, path))} is {value}")
            if "github.ref_name" in text:
                found.append(f"{name}: github.ref_name is refused at {'.'.join(map(str, path))}; a tag can carry the branch name, compare github.ref")
            if path[-1:] == ("continue-on-error",) and not (
                name == ATTEMPT and path[:3] == ("jobs", "run", "steps") and len(path) == 5
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

    refs = [
        (name, path)
        for name, doc in docs.items()
        for path, value in walk(doc)
        if TOKEN_NAME in str(value) or TOKEN_NAME == str(path[-1])
    ]
    outside = [(n, p) for n, p in refs if n != "sim-gate-lane.yml" or p[:2] != ("jobs", "route")]
    if outside:
        found.append(f"{TOKEN_NAME} is referenced outside the route job: {outside}")
    misplaced = [p for n, p in refs if (n, p[:2]) == ("sim-gate-lane.yml", ("jobs", "route")) and (p[2] != "steps" or p[4] != "env")]
    if misplaced:
        found.append(f"{TOKEN_NAME} is used outside a step env: {misplaced}")
    if len({p[:4] for n, p in refs}) != 1:
        found.append(f"{TOKEN_NAME} must appear in exactly one step")

    found += lane_label_violations(docs)
    found += validate_violations(docs)
    found += checkout_violations(docs)
    found += secret_violations(docs)
    found += hygiene_violations(docs)
    label_input = docs["sim-gate-lane.yml"]["on"]["workflow_call"]["inputs"]["lab_label"]
    if "opaque" not in str(label_input.get("description", "")).lower():
        found.append("lab_label input must say the label is public in logs and has to be opaque")

    found += cache_wiring_violations(docs)
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

    route = docs["sim-gate-lane.yml"]["jobs"]["route"]
    runs_on = str(route.get("runs-on", ""))
    if "${{" in runs_on or not runs_on.startswith("ubuntu-"):
        found.append(f"route job must run on a GitHub-hosted ubuntu label, has {runs_on!r}")
    if route.get("environment") != "sim-route":
        found.append("route job must use environment sim-route")
    guard = route.get("if")
    fork = {"github.event_name": "pull_request", "github.repository": "owner/repo",
            "github.event.pull_request.head.repo.full_name": "stranger/repo"}
    same = dict(fork, **{"github.event.pull_request.head.repo.full_name": "owner/repo"})
    dispatch = {"github.event_name": "workflow_dispatch", "github.repository": "owner/repo"}
    if guard is None:
        found.append("route job has no fork guard")
    else:
        if truthy(evaluate(condition(guard), fork)):
            found.append("route job runs for a fork pull request")
        if not truthy(evaluate(condition(guard), same)) or not truthy(evaluate(condition(guard), dispatch)):
            found.append("route job is skipped for a same-repository run")

    lane_jobs = docs["sim-gate-lane.yml"]["jobs"]
    runner_expression = lane_jobs["attempt"]["with"]["runner"]
    for target, label, want in (("", "lab", "ubuntu-24.04"), ("cloud", "lab", "ubuntu-24.04"), ("onprem", "lab", "lab")):
        context = {"needs.route.outputs.target": target, "inputs.lab_label": label}
        if evaluate(runner_expression, context) != want:
            found.append(f"attempt runner for target {target!r} is not {want!r}")
    if not str(lane_jobs["retry"]["with"]["runner"]).startswith("ubuntu-"):
        found.append("retry must run on a hosted label")

    retry_if = lane_jobs["retry"]["if"]
    for verdict in ("pass", "test", "infra", "", "unknown"):
        for target in ("onprem", "cloud", ""):
            for reason in ("", "cache tripwire", "other cause"):
                context = {
                    "needs.attempt.outputs.verdict": verdict,
                    "needs.attempt.outputs.reason": reason,
                    "needs.route.outputs.target": target,
                }
                runs = truthy(evaluate(condition(retry_if), context))
                if runs != (verdict == "infra" and target == "onprem" and reason != "cache tripwire"):
                    found.append(f"retry runs={runs} for verdict {verdict!r} on target {target!r} with reason {reason!r}")

    attempt = docs["sim-gate-attempt.yml"]["jobs"]
    total = (
        minutes(docs, "sim-gate.yml", "validate")
        + minutes(docs, "sim-gate-lane.yml", "route")
        + 2 * (minutes(docs, "sim-gate-attempt.yml", "run") + minutes(docs, "sim-gate-attempt.yml", "classify"))
        + minutes(docs, "sim-gate.yml", "gate")
    )
    if total > BUDGET_MINUTES:
        found.append(f"worst-case critical path is {total} minutes, budget {BUDGET_MINUTES}")
    if minutes(docs, "sim-gate-attempt.yml", "run") > MAX_ATTEMPT_MINUTES:
        found.append("one attempt may take longer than the per-attempt cap")
    for name, doc in docs.items():
        for job_id, job in doc["jobs"].items():
            steps = job.get("steps", [])
            first_dotnet = next((i for i, st in enumerate(steps) if re.search(r"\bdotnet\b", str(st.get("run", "")))), None)
            if first_dotnet is None:
                continue
            setup = next(
                (i for i, st in enumerate(steps) if str(st.get("uses", "")).startswith("actions/setup-dotnet@")), None
            )
            if setup is None or setup > first_dotnet:
                found.append(f"{name}: job {job_id} runs dotnet without a prior actions/setup-dotnet step")
            elif steps[setup].get("with", {}).get("global-json-file") != DOTNET_GLOBAL_JSON:
                found.append(f"{name}: job {job_id} setup-dotnet must use global-json-file {DOTNET_GLOBAL_JSON}")
    classify_step = next(st for st in attempt["classify"]["steps"] if st.get("id") == "verdict")
    classify_run = classify_step["run"]
    if "--reason-file" not in classify_run:
        found.append("classify step does not capture the infra reason")
    if classify_step.get("env", {}).get("RUN_RESULT") != "${{ needs.run.result }}" or '--run-result "$RUN_RESULT"' not in classify_run:
        found.append("classify step does not take the run job result from needs.run.result")
    gate_run = next(st["run"] for st in docs["sim-gate.yml"]["jobs"]["gate"]["steps"] if st["name"] == "Decide")
    for lane in ("unit", "integration"):
        if f'--reason "{lane}=' not in gate_run:
            found.append(f"gate does not pass the infra reason of lane {lane}")
    for output in ("attempt_reason", "retry_reason"):
        if output not in docs["sim-gate-lane.yml"]["on"]["workflow_call"]["outputs"]:
            found.append(f"lane workflow does not output {output}")
    found += environment_violations(docs)
    return found
