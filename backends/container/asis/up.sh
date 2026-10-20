#!/usr/bin/env bash
# Usage: up.sh <owner/repo> <ssh-config> <ssh-host>=<label> [...]
set -euo pipefail

repo=${1:?repo}; ssh_config=${2:?ssh config}; shift 2
[ $# -gt 0 ] || { echo "up: give at least one host=label pair" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)

for pair in "$@"; do
    host=${pair%%=*}; label=${pair#*=}
    [ -n "$host" ] && [ -n "$label" ] && [ "$host" != "$pair" ] || { echo "up: bad pair $pair" >&2; exit 2; }
    scp -q -F "$ssh_config" "$here/setup-vm.sh" "$host:/tmp/ti-setup-vm.sh"
    scp -q -F "$ssh_config" "$root/trials/sampler/sample.py" "$host:/tmp/ti-sample.py"
    gh api -X POST "repos/$repo/actions/runners/registration-token" -q .token \
        | ssh -F "$ssh_config" "$host" sudo sh /tmp/ti-setup-vm.sh --url "https://github.com/$repo" --name "$label" --labels "$label"
done

for _ in $(seq 1 30); do
    online=$(gh api "repos/$repo/actions/runners" -q '[.runners[] | select(.status == "online") | .name] | join(" ")')
    missing=0
    for pair in "$@"; do
        case " $online " in *" ${pair#*=} "*) ;; *) missing=1 ;; esac
    done
    [ $missing -eq 0 ] && { echo "up: online: $online"; exit 0; }
    sleep 5
done
echo "up: runners not online after 150 s; online: ${online:-none}" >&2
exit 1
