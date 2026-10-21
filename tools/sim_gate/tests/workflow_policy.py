from __future__ import annotations

import re
from pathlib import Path

TOKEN_NAME = "SIM_ROUTE_TOKEN"
WRITER_PREFIX = "SIM_CACHE_WRITE"
FORBIDDEN_TRIGGERS = {"pull_request_target", "workflow_run", "issue_comment"}
SECRETISH_VARIABLE = re.compile(r"\bvars\.[A-Za-z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD)", re.I)
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
BUDGET_MINUTES = 80
MAX_ATTEMPT_MINUTES = 30
DOTNET_GLOBAL_JSON = "sim/dotnet/global.json"


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


TOKEN = re.compile(r"""\s*(?:(\|\||&&|==|!=)|('(?:[^']|'')*')|(!?[A-Za-z_]\w*\(\))|([A-Za-z_][\w.-]*)|(.))""")


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
            if match.group(5):
                raise Unsupported(f"unsupported character {match.group(5)!r} in {text!r}")
            position = match.end()
            op, string, function, path, _ = match.groups()
            if op:
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
            if "permissions" in path and value not in ("read", "none"):
                found.append(f"{name}: permission {'.'.join(map(str, path))} is {value}")
            if path[-1:] == ("continue-on-error",):
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

    default_branch = {"github.ref_name": "main", "github.event.repository.default_branch": "main"}
    other_branch = {"github.ref_name": "topic", "github.event.repository.default_branch": "main"}
    for name, doc in docs.items():
        writer_refs = {
            path[:2]
            for path, value in walk(doc)
            if WRITER_PREFIX in str(value) or WRITER_PREFIX in str(path[-1])
        }
        for where in sorted(writer_refs, key=str):
            guard = doc["jobs"][where[1]].get("if") if where[0] == "jobs" else None
            if where[0] != "jobs" or guard is None:
                found.append(f"{name}: {WRITER_PREFIX}* is referenced outside a job gated on the default branch: {where}")
            elif not truthy(evaluate(condition(guard), default_branch)) or truthy(evaluate(condition(guard), other_branch)):
                found.append(f"{name}: job {where[1]} reads {WRITER_PREFIX}* but its guard does not mean the default branch only")

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
            context = {"needs.attempt.outputs.verdict": verdict, "needs.route.outputs.target": target}
            runs = truthy(evaluate(condition(retry_if), context))
            if runs != (verdict == "infra" and target == "onprem"):
                found.append(f"retry runs={runs} for verdict {verdict!r} on target {target!r}")

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
    if "environment" in attempt["run"]:
        found.append("the attempt job must not use an environment")
    return found
