#!/usr/bin/env bash
# Usage: down.sh <ssh-config> <server-host>
# Removes the scale sets (and their GitHub registrations), the controller and the ARC token; k3s stays.
set -euo pipefail

ssh_config=${1:?ssh config}; server=${2:?server host}
ssh -F "$ssh_config" "$server" 'sudo sh -s' <<'REMOTE'
set -u
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
status=0
for name in ti-kube-unit ti-kube-unit-sched ti-kube-integration ti-kube-integration-sched; do
    helm -n ti-runners uninstall "$name" --wait >/dev/null 2>&1 && echo "down: removed $name" || echo "down: $name not installed"
done
helm -n ti-arc-system uninstall arc --wait >/dev/null 2>&1 && echo "down: removed controller" || echo "down: controller not installed"
kubectl -n ti-runners delete secret ti-arc-github --ignore-not-found >/dev/null || status=1
left=$(kubectl get secrets -A --no-headers 2>/dev/null | grep -c 'ti-arc-github\|listener-config' || true)
echo "down: token secrets left: $left"
[ "$left" -eq 0 ] || status=1
exit $status
REMOTE
