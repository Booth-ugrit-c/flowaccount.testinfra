#!/usr/bin/env bash
# Usage (root on the Proxmox host): switch-design.sh <design|none> <design>=<vmid>,<vmid> [...]
set -euo pipefail

want=${1:?design or none}; shift
margin_mib=${MARGIN_MIB:-1024}
declare -A members=()
for pair in "$@"; do
    name=${pair%%=*}; ids=${pair#*=}
    [[ $name =~ ^[a-z0-9-]+$ && $ids =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "switch: bad pair $pair" >&2; exit 2; }
    members[$name]=${ids//,/ }
done
[[ $want == none || -n ${members[$want]:-} ]] || { echo "switch: unknown design $want" >&2; exit 2; }

running() { qm status "$1" | grep -q 'status: running'; }

for name in "${!members[@]}"; do
    [[ $name == "$want" ]] && continue
    for id in ${members[$name]}; do
        if running "$id"; then
            qm shutdown "$id" --timeout 120 || qm stop "$id"
            echo "switch: stopped $id ($name)"
        fi
    done
done
[[ $want == none ]] && { echo "switch: no design running"; exit 0; }

need=0
for id in ${members[$want]}; do
    running "$id" || need=$(( need + $(qm config "$id" | sed -n 's/^memory: //p') ))
done
avail=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
if (( avail < need + margin_mib )); then
    echo "switch: refusing to start $want: ${avail} MiB available, $(( need + margin_mib )) MiB needed" >&2
    exit 3
fi
for id in ${members[$want]}; do
    running "$id" || { qm start "$id"; echo "switch: started $id ($want)"; }
done
for id in ${members[$want]}; do echo "switch: $id $(qm status "$id")"; done
