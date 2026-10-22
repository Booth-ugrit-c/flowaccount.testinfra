#!/bin/sh
set -eu

ROOT=${SIM_ROOT:-sim/dotnet}
WORKSPACE=${GITHUB_WORKSPACE:-$(pwd)}
HERE=$(cd "$(dirname "$0")" && pwd)
SELF=$HERE/$(basename "$0")
PYTHON=${SIM_PYTHON:-python3}

usage() {
    echo "usage: test_lane.sh LIST COUNTS_DIR FILTER PARALLEL" >&2
    exit 2
}

dotnet_cmd() {
    if [ -n "${SIM_DOTNET:-}" ]; then
        "$SIM_DOTNET" "$@"
    else
        dotnet "$@"
    fi
}

key_of() {
    printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_'
}

if [ "${1:-}" = --one ]; then
    counts=$2
    filter=$3
    project=$4
    key=$(key_of "$project")
    set -- test "$WORKSPACE/$project" -c Release --no-build -v:normal --disable-build-servers \
        --results-directory "$counts" --logger "trx;LogFileName=$key.trx"
    [ -z "$filter" ] || set -- "$@" --filter "$filter"
    rc=0
    (cd "$ROOT" && dotnet_cmd "$@") > "$counts/$key.log" 2>&1 || rc=1
    if "$PYTHON" -I "$HERE/trx_counts.py" "$counts/$key.trx" > "$counts/$key.count.tmp"; then
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

[ $# -eq 4 ] || usage
list=$1
filter=$3
parallel=$4
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

printf '%s\n' "$projects" | while IFS= read -r project; do
    key=$(key_of "$project")
    rm -f "$counts/$key.trx" "$counts/$key.count" "$counts/$key.count.tmp" "$counts/$key.log"
done

# Parallel first runs race in the SDK's first-use setup.
(cd "$ROOT" && dotnet_cmd nuget --version) > /dev/null || {
    echo "test_lane: dotnet first-use setup failed" >&2
    exit 1
}

status=0
printf '%s\n' "$projects" | tr '\n' '\0' |
    xargs -0 -n 1 -P "$parallel" sh "$SELF" --one "$counts" "$filter" || status=1

printf '%s\n' "$projects" | while IFS= read -r project; do
    key=$(key_of "$project")
    echo "::group::$project"
    cat "$counts/$key.log" 2>/dev/null || echo "test_lane: no log for $project"
    echo "::endgroup::"
done
exit "$status"
