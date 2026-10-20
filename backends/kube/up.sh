#!/usr/bin/env bash
# Usage: up.sh <owner/repo> <ssh-config> <services|none> <server-host> [<agent-host>...]
set -euo pipefail

repo=${1:?repo}; ssh_config=${2:?ssh config}; services=${3:?services or none}; server=${4:?server host}; shift 4
agents=("$@")
[ -n "${GH_TOKEN:-}" ] || { echo "up: GH_TOKEN must hold the token ARC registers runners with" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
run() { ssh -F "$ssh_config" "$1" "${@:2}"; }

images=''
if [ "$services" != none ]; then
    images=$(cd "$root" && python3 -c 'import sys; from workload.services import SERVICES; print(" ".join(SERVICES[k].image for k in sys.argv[1].split(",")))' "$services")
fi

for host in "$server" "${agents[@]}"; do
    run "$host" 'rm -rf /tmp/ti-kube && mkdir -p /tmp/ti-kube/manifests /tmp/ti-kube/values'
    scp -q -F "$ssh_config" "$here"/versions.env "$here"/runner.Dockerfile "$here"/node-setup.sh "$here"/cluster-up.sh "$here"/warm-mirror.sh "$root/trials/sampler/sample.py" "$host:/tmp/ti-kube/"
    scp -q -F "$ssh_config" "$here"/manifests/*.yaml "$host:/tmp/ti-kube/manifests/"
    scp -q -F "$ssh_config" "$here"/values/*.yaml "$host:/tmp/ti-kube/values/"
done

run "$server" sudo sh /tmp/ti-kube/node-setup.sh --role server
if [ ${#agents[@]} -gt 0 ]; then
    server_ip=$(run "$server" "hostname -I | cut -d' ' -f1")
    for agent in "${agents[@]}"; do
        run "$server" sudo cat /var/lib/rancher/k3s/server/node-token | run "$agent" sudo sh /tmp/ti-kube/node-setup.sh --role agent --server "$server_ip"
    done
fi
printf '%s\n' "$GH_TOKEN" | run "$server" sudo sh /tmp/ti-kube/cluster-up.sh "$repo" $(( 1 + ${#agents[@]} ))
for host in "$server" "${agents[@]}"; do
    run "$host" sudo sh /tmp/ti-kube/warm-mirror.sh $images
done
