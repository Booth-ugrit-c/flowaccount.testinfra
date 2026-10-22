#!/bin/sh
set -eu

ROOT=${SIM_ROOT:-sim/workspace}
HERE=$(cd "$(dirname "$0")" && pwd)
SELF=$HERE/$(basename "$0")
REPO=$(cd "$HERE/../.." && pwd)
PYTHON=${SIM_PYTHON:-python3}
NX_DAEMON=false
NX_NO_CLOUD=true
NX_TUI=false
export NX_DAEMON NX_NO_CLOUD NX_TUI

usage() {
    echo "usage: test_lane.sh LIST COUNTS_DIR PARALLEL" >&2
    exit 2
}

nx_cmd() {
    if [ -n "${SIM_NX:-}" ]; then
        "$SIM_NX" "$@"
    else
        node "$SIM_NX_ENTRY" "$@"
    fi
}

key_of() {
    printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_'
}

if [ "${1:-}" = --one ]; then
    counts=$2
    project=$3
    key=$(key_of "$project")
    rc=0
    (cd "$ROOT" && nx_cmd run "$project:test" --skip-nx-cache --outputStyle=static -- --ci --json "--outputFile=$counts/$key.json") \
        > "$counts/$key.log" 2>&1 || rc=1
    if "$PYTHON" -I "$HERE/jest_counts.py" "$counts/$key.json" > "$counts/$key.count.tmp"; then
        mv "$counts/$key.count.tmp" "$counts/$key.count"
    else
        rm -f "$counts/$key.count.tmp"
    fi
    if [ "$rc" -eq 0 ]; then
        echo "test_lane: pass $project"
    else
        echo "test_lane: fail $project"
    fi
    exit "$rc"
fi

[ $# -eq 3 ] || usage
list=$1
parallel=$3
case $parallel in
    '' | *[!0-9]*) usage ;;
esac
if [ "$parallel" -eq 0 ]; then
    parallel=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
fi
[ -r "$list" ] || {
    echo "test_lane: cannot read $list" >&2
    exit 2
}
mkdir -p "$2"
counts=$(cd "$2" && pwd)

projects=$(awk '{ sub(/\r$/, ""); gsub(/^[ \t]+|[ \t]+$/, "") } $0 != "" && !seen[$0]++' "$list")
if [ -z "$projects" ]; then
    echo "test_lane: 0 projects selected"
    exit 0
fi

if [ -z "${SIM_NX:-}" ]; then
    workspace=$(cd "$ROOT" && pwd)
    SIM_NX_ENTRY=$("$PYTHON" -I -c 'import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from adapters.workspace.affected import GraphError, nx_entry
try:
    print(nx_entry(Path(sys.argv[2])))
except GraphError as exc:
    sys.exit(f"test_lane: {exc}")' "$REPO" "$workspace") || exit 2
    export SIM_NX_ENTRY
fi

printf '%s\n' "$projects" | while IFS= read -r project; do
    key=$(key_of "$project")
    rm -f "$counts/$key.json" "$counts/$key.count" "$counts/$key.count.tmp" "$counts/$key.log"
done

status=0
printf '%s\n' "$projects" | tr '\n' '\0' |
    xargs -0 -n 1 -P "$parallel" sh "$SELF" --one "$counts" || status=1

printf '%s\n' "$projects" | while IFS= read -r project; do
    key=$(key_of "$project")
    echo "::group::$project"
    cat "$counts/$key.log" 2>/dev/null || echo "test_lane: no log for $project"
    echo "::endgroup::"
done
exit "$status"
