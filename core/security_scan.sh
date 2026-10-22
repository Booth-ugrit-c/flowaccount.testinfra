#!/bin/sh
set -eu

lane=security_scan

finish() {
    result=$1
    shift
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        printf 'result=%s\n' "$result" >> "$GITHUB_OUTPUT"
    fi
    echo "$lane: $*"
    if [ "$result" = pass ]; then
        exit 0
    fi
    exit 1
}

error() {
    finish scanner-error "scanner error: $*"
}

PARSE='
import json, sys

lane, rc, report, staged = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
SCANNED_SUFFIXES = (".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs")


def out(status, lines=()):
    print(status)
    for line in lines:
        print(line)
    sys.exit(0)


def norm(path):
    path = str(path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


if rc not in (0, 1):
    out(f"error exit code {rc}")
try:
    with open(report, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError) as exc:
    out(f"error report unreadable ({type(exc).__name__})")

if lane == "secret-scan":
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        out("error report is not a gitleaks finding list")
    found = [(item.get("RuleID"), item.get("File"), item.get("StartLine")) for item in data]
else:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list) or not isinstance(data.get("errors"), list):
        out("error report is not a semgrep result")
    if data["errors"]:
        lines = []
        for item in data["errors"]:
            kind = item.get("type") if isinstance(item, dict) else None
            if isinstance(kind, list) and kind:
                kind = kind[0]
            path = norm(item.get("path", "")) if isinstance(item, dict) else ""
            lines.append(f"semgrep error {kind} {path}".rstrip())
        out(f"error semgrep reported {len(lines)} errors", lines)
    paths = data.get("paths")
    scanned = paths.get("scanned") if isinstance(paths, dict) else None
    if not isinstance(scanned, list):
        out("error report has no scanned paths")
    scanned = {norm(path) for path in scanned}
    with open(staged, encoding="utf-8") as handle:
        expected = [path for path in handle.read().splitlines() if path.endswith(SCANNED_SUFFIXES)]
    missing = [path for path in expected if path not in scanned]
    if missing:
        out(f"error {len(missing)} staged files not scanned", [f"not scanned {path}" for path in missing])
    if not all(isinstance(item, dict) for item in data["results"]):
        out("error report is not a semgrep result")
    found = [(str(item.get("check_id")).rsplit(".", 1)[-1], item.get("path"), (item.get("start") or {}).get("line")) for item in data["results"]]

if rc == 0 and found:
    out(f"error exit 0 with {len(found)} findings")
if rc == 1 and not found:
    out("error exit 1 with no findings")
if found:
    out(f"findings {len(found)}", [f"finding {rule} {norm(path)}:{line}" for rule, path, line in found])
out("pass")
'

case ${1:-} in
    secret-scan | security-scan) lane=$1 ;;
    *) error "usage: security_scan.sh secret-scan|security-scan BASE" ;;
esac
base=${2:-}
[ -n "$base" ] || error "BASE is missing"

GITLEAKS=${SIM_GITLEAKS:-gitleaks}
SEMGREP=${SIM_SEMGREP:-semgrep}
PY=${SIM_PYTHON:-python3}

here=$(cd "$(dirname "$0")" && pwd) || error "cannot resolve the script directory"
config_dir=${SIM_SECURITY_CONFIG:-$here/security}
config_dir=$(cd "$config_dir" && pwd) || error "config directory not found"
for name in gitleaks.toml semgrep.yml; do
    [ -f "$config_dir/$name" ] || error "config $name not found"
done

top=$(git rev-parse --show-toplevel) || error "not inside a git work tree"
cd "$top" || error "cannot enter the work tree"
git rev-parse --verify --quiet "$base^{commit}" >/dev/null || error "base is not a commit: $base"

tmp=$(mktemp -d) || error "mktemp failed"
trap 'rm -rf -- "$tmp"' EXIT
stage=$tmp/scan
mkdir "$stage" || error "cannot create the staging directory"
: > "$tmp/staged"

git -c core.quotePath=false diff --name-only --no-ext-diff -M --diff-filter=ACMRT "$base" -- > "$tmp/changed" || error "git diff failed"
git -c core.quotePath=false ls-files --others --exclude-standard > "$tmp/untracked" || error "git ls-files failed"
LC_ALL=C sort -u "$tmp/changed" "$tmp/untracked" > "$tmp/paths" || error "sort failed"

count=0
while IFS= read -r path; do
    case ${path##*/} in
        .gitleaksignore | .semgrepignore)
            echo "$lane: skipped scanner ignore file $path"
            continue
            ;;
    esac
    if [ -L "$path" ]; then
        echo "$lane: skipped symlink $path"
        continue
    fi
    [ -f "$path" ] || error "changed path is not a regular file: $path"
    mkdir -p -- "$stage/$(dirname -- "$path")" || error "cannot stage $path"
    cp -- "$path" "$stage/$path" || error "cannot stage $path"
    printf '%s\n' "$path" >> "$tmp/staged"
    count=$((count + 1))
done < "$tmp/paths"
echo "$lane: $count changed files vs $base"

report=$tmp/report.json
rc=0
if [ "$lane" = secret-scan ]; then
    (cd "$stage" && exec "$GITLEAKS" dir --no-banner --redact --ignore-gitleaks-allow --log-level warn \
        --config "$config_dir/gitleaks.toml" --report-format json --report-path "$report" --exit-code 1 .) || rc=$?
else
    : > "$stage/.semgrepignore"
    (cd "$stage" && exec "$SEMGREP" scan --config "$config_dir/semgrep.yml" --json --output "$report" --error \
        --metrics=off --disable-version-check --disable-nosem --no-git-ignore --max-target-bytes=0 .) || rc=$?
fi

parsed=$("$PY" -I -c "$PARSE" "$lane" "$rc" "$report" "$tmp/staged") || error "report parser failed"
status=$(printf '%s\n' "$parsed" | sed -n 1p)
printf '%s\n' "$parsed" | sed 1d > "$tmp/lines"
while IFS= read -r line; do
    echo "$lane: $line"
done < "$tmp/lines"

case $status in
    pass) finish pass "pass" ;;
    findings\ *) finish findings "$status" ;;
    error\ *) error "${status#error }" ;;
    *) error "unexpected parser output" ;;
esac
