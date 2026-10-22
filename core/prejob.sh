#!/bin/sh
set -eu

fail() {
    echo "prejob: $*" >&2
    exit 1
}

DOCKER=${SIM_DOCKER:-docker}
PS=${SIM_PS:-ps}
SURVIVOR_COMMS=${SIM_SURVIVORS:-}
case $SURVIVOR_COMMS in
    *[!\ ]*) ;;
    *) fail "SIM_SURVIVORS is empty; the adapter must name the process names an earlier job can leave behind" ;;
esac
case $SURVIVOR_COMMS in
    *[!A-Za-z0-9._\ -]*) fail "SIM_SURVIVORS must be process names of A-Za-z0-9._- separated by spaces" ;;
esac

runner=${RUNNER_NAME:-}
case $runner in
    '' | *[!A-Za-z0-9._-]*) fail "RUNNER_NAME is missing or outside A-Za-z0-9._-" ;;
esac
[ "${#runner}" -le 64 ] || fail "RUNNER_NAME is longer than 64 characters"

workspace=${GITHUB_WORKSPACE:-}
case $workspace in
    /* | [A-Za-z]:/*) ;;
    *) fail "GITHUB_WORKSPACE must be an absolute path" ;;
esac
workspace=${workspace%/}
case $workspace in
    */.. | */../* | */./* | */.) fail "GITHUB_WORKSPACE must not contain dot segments" ;;
esac
depth=$(printf '%s\n' "$workspace" | awk -F/ '{ n = 0; for (i = 1; i <= NF; i++) if ($i != "") n++; print n }')
[ "$depth" -ge 4 ] || fail "GITHUB_WORKSPACE is too shallow to wipe safely"
[ "$workspace" != "${HOME:-}" ] || fail "GITHUB_WORKSPACE is the home directory"

if command -v "$DOCKER" >/dev/null 2>&1; then
    label="label=sim.runner=$runner"
    ids=$("$DOCKER" ps -aq --filter "$label") || fail "docker could not list containers"
    if [ -n "$ids" ]; then
        printf '%s\n' "$ids" | xargs "$DOCKER" rm -f >/dev/null || fail "docker could not remove containers"
    fi
    ids=$("$DOCKER" network ls -q --filter "$label") || fail "docker could not list networks"
    if [ -n "$ids" ]; then
        printf '%s\n' "$ids" | xargs "$DOCKER" network rm >/dev/null || fail "docker could not remove networks"
    fi
    ids=$("$DOCKER" volume ls -q --filter "$label") || fail "docker could not list volumes"
    if [ -n "$ids" ]; then
        printf '%s\n' "$ids" | xargs "$DOCKER" volume rm -f >/dev/null || fail "docker could not remove volumes"
    fi
fi

dirs=$(find "$workspace" \( -name .git -o -name node_modules \) -prune -o -type d \( -name bin -o -name obj \) -print)
printf '%s\n' "$dirs" | while IFS= read -r dir; do
    if [ -z "$dir" ] || [ ! -d "$dir" ] || [ -L "$dir" ]; then
        continue
    fi
    case $dir in "$workspace"/*) ;; *) continue ;; esac
    parent=$(dirname "$dir")
    owned=
    for project in "$parent"/*.csproj "$parent"/*.fsproj "$parent"/*.vbproj; do
        if [ -f "$project" ]; then
            owned=1
            break
        fi
    done
    [ -n "$owned" ] && rm -rf -- "$dir"
done

table=$("$PS" -eo pid=,ppid=,uid=,comm=) || fail "process listing failed"
chain=$(printf '%s\n' "$table" | awk -v self="$$" '
    { parent[$1] = $2 }
    END { p = self; while (p != "" && p != 0 && !(p in seen)) { seen[p] = 1; printf "%s ", p; p = parent[p] } }')
survivors=$(printf '%s\n' "$table" | awk -v uid="$(id -u)" -v chain="$chain" -v comms="$SURVIVOR_COMMS" '
    BEGIN {
        n = split(chain, c, " "); for (i = 1; i <= n; i++) skip[c[i]] = 1
        m = split(comms, w, " "); for (i = 1; i <= m; i++) want[w[i]] = 1
    }
    { rows[++count] = $1; parent[$1] = $2; owner[$1] = $3; comm[$1] = $4 }
    function under_a_worker(pid,    p) {
        for (p = parent[pid]; p != "" && p != 0 && seen[p] != pid; p = parent[p]) {
            seen[p] = pid
            if (comm[p] == "Runner.Worker") return 1
        }
        return 0
    }
    END {
        for (i = 1; i <= count; i++) {
            pid = rows[i]
            if (owner[pid] == uid && (comm[pid] in want) && !(pid in skip) && !under_a_worker(pid)) print pid, comm[pid]
        }
    }')
if [ -n "$survivors" ]; then
    printf '%s\n' "$survivors" | while read -r pid comm; do
        echo "prejob: process from an earlier job: pid $pid ($comm), killed" >&2
        kill -KILL "$pid" 2>/dev/null || true
    done
    fail "a process from an earlier job survived; it was killed and this job is failed so the next one starts clean"
fi
echo "prejob: clean"
