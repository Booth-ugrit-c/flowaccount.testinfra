#!/bin/sh
set -eu

PS=${SIM_PS:-ps}

table=$("$PS" -eo pid=,ppid=,comm=) || {
    echo "kill_own_runner: process listing failed" >&2
    exit 1
}
targets=$(printf '%s\n' "$table" | awk -v self="$$" '
    { parent[$1] = $2; comm[$1] = $3 }
    END {
        p = self
        while (p != "" && p != 0 && !(p in seen)) {
            seen[p] = 1
            if (comm[p] == "Runner.Worker" || comm[p] == "Runner.Listener") printf "%s ", p
            p = parent[p]
        }
    }')
if [ -z "$targets" ]; then
    echo "kill_own_runner: no runner process on the parent chain of this step" >&2
    exit 1
fi
for pid in $targets; do
    kill -KILL "$pid"
done
