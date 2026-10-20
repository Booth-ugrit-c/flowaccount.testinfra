#!/usr/bin/env bash
# Usage: up.sh <owner/repo> <ssh-config> <services|none> <ssh-host>=<base> [...]
set -euo pipefail

repo=${1:?repo}; ssh_config=${2:?ssh config}; services=${3:?services or none}; shift 3
[ $# -gt 0 ] || { echo "up: give at least one host=base pair" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
runners=${RUNNERS_PER_VM:-2}

images=''
if [ "$services" != none ]; then
    images=$(cd "$root" && python3 -c 'import sys; from workload.services import SERVICES; print(" ".join(SERVICES[k].image for k in sys.argv[1].split(",")))' "$services")
fi

expected=''
for pair in "$@"; do
    host=${pair%%=*}; base=${pair#*=}
    [ -n "$host" ] && [ -n "$base" ] && [ "$host" != "$pair" ] || { echo "up: bad pair $pair" >&2; exit 2; }
    scp -q -F "$ssh_config" "$here/setup-vm.sh" "$host:/tmp/ti-setup-vm.sh"
    scp -q -F "$ssh_config" "$root/trials/sampler/sample.py" "$host:/tmp/ti-sample.py"
    gh api -X POST "repos/$repo/actions/runners/registration-token" -q .token \
        | ssh -F "$ssh_config" "$host" sudo sh /tmp/ti-setup-vm.sh --url "https://github.com/$repo" --base "$base" --runners "$runners" --images "'$images'"
    for i in $(seq 1 "$runners"); do expected="$expected $base-$i"; done
done

for _ in $(seq 1 30); do
    online=$(gh api "repos/$repo/actions/runners" -q '[.runners[] | select(.status == "online") | .name] | join(" ")')
    missing=0
    for name in $expected; do
        case " $online " in *" $name "*) ;; *) missing=1 ;; esac
    done
    [ $missing -eq 0 ] && { echo "up: online: $online"; exit 0; }
    sleep 5
done
echo "up: runners not online after 150 s; online: ${online:-none}" >&2
exit 1
