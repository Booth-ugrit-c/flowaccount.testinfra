#!/usr/bin/env bash
# Usage: down.sh <owner/repo> <ssh-config> <ssh-host>=<base> [...]
set -euo pipefail

repo=${1:?repo}; ssh_config=${2:?ssh config}; shift 2
status=0
for pair in "$@"; do
    host=${pair%%=*}; base=${pair#*=}
    ssh -F "$ssh_config" "$host" 'for d in /opt/actions-runner-*; do [ -f "$d/.service" ] && (cd "$d" && sudo ./svc.sh stop && sudo ./svc.sh uninstall); done; sudo systemctl disable --now ti-sampler.service' || status=1
    for id in $(gh api "repos/$repo/actions/runners" -q ".runners[] | select(.name | startswith(\"$base-\")) | .id"); do
        gh api -X DELETE "repos/$repo/actions/runners/$id" && echo "down: deregistered runner $id of $base"
    done
done
left=$(gh api "repos/$repo/actions/runners" -q '.total_count')
echo "down: runners still registered: $left"
exit $status
