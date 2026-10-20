#!/usr/bin/env bash
# Usage: down.sh <owner/repo> <ssh-config> <ssh-host>=<label> [...]
set -euo pipefail

repo=${1:?repo}; ssh_config=${2:?ssh config}; shift 2
status=0
for pair in "$@"; do
    host=${pair%%=*}; label=${pair#*=}
    ssh -F "$ssh_config" "$host" 'cd /opt/actions-runner && sudo ./svc.sh stop && sudo ./svc.sh uninstall; sudo systemctl disable --now ti-sampler.service' || status=1
    for id in $(gh api "repos/$repo/actions/runners" -q ".runners[] | select(.name == \"$label\") | .id"); do
        gh api -X DELETE "repos/$repo/actions/runners/$id" && echo "down: deregistered $label ($id)"
    done
done
left=$(gh api "repos/$repo/actions/runners" -q '.total_count')
echo "down: runners still registered: $left"
exit $status
