#!/bin/sh
# Runs as root on the server node from /tmp/ti-kube; reads the GitHub token for ARC from stdin.
set -eu

repo=${1:?owner/repo}; nodes=${2:-1}
here=/tmp/ti-kube
. "$here/versions.env"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
IFS= read -r token || true
[ -n "$token" ] || { echo "cluster-up: no token on stdin" >&2; exit 2; }
chart=oci://ghcr.io/actions/actions-runner-controller-charts

if ! helm version --short 2>/dev/null | grep -q "^$HELM_VERSION+"; then
    tarball="helm-$HELM_VERSION-linux-amd64.tar.gz"
    (cd "$here" && curl -fsSLO "https://get.helm.sh/$tarball" && curl -fsSLO "https://get.helm.sh/$tarball.sha256sum" \
        && sha256sum -c "$tarball.sha256sum" && tar -xzf "$tarball" linux-amd64/helm && install -m 0755 linux-amd64/helm /usr/local/bin/helm)
fi

for _ in $(seq 1 60); do
    [ "$(kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready ')" -ge "$nodes" ] && break
    sleep 5
done
kubectl wait --for=condition=Ready node --all --timeout=60s

render() { sed -e "s|__RUNNER_IMAGE__|$RUNNER_IMAGE|" -e "s|__DIND_IMAGE__|$DIND_IMAGE|" -e "s|__MIRROR_IMAGE__|$MIRROR_IMAGE|" "$1"; }
render "$here/manifests/pool.yaml" | kubectl apply -f -
kubectl -n ti-mirror rollout status daemonset/mirror --timeout=180s

kubectl -n ti-runners delete secret ti-arc-github --ignore-not-found >/dev/null
printf %s "$token" | kubectl -n ti-runners create secret generic ti-arc-github --from-file=github_token=/dev/stdin >/dev/null

helm upgrade --install arc "$chart/gha-runner-scale-set-controller" --version "$ARC_CHART_VERSION" \
    -n ti-arc-system --create-namespace --wait >/dev/null

for set in unit:pr unit:schedule integration:pr integration:schedule; do
    lane=${set%%:*}; kind=${set#*:}
    name="ti-kube-$lane"; class=ti-pr
    [ "$kind" = schedule ] && { name="$name-sched"; class=ti-sched; }
    render "$here/values/$lane.yaml" >"$here/values/$name.rendered.yaml"
    helm upgrade --install "$name" "$chart/gha-runner-scale-set" --version "$ARC_CHART_VERSION" -n ti-runners \
        -f "$here/values/$name.rendered.yaml" \
        --set githubConfigUrl="https://github.com/$repo" \
        --set githubConfigSecret=ti-arc-github \
        --set runnerScaleSetName="$name" \
        --set template.spec.priorityClassName="$class" \
        --set controllerServiceAccount.namespace=ti-arc-system \
        --set controllerServiceAccount.name=arc-gha-rs-controller \
        --wait >/dev/null
    echo "cluster-up: scale set $name priority $class"
done

for _ in $(seq 1 36); do
    listeners=$(kubectl -n ti-arc-system get pods --no-headers 2>/dev/null | grep -c 'listener.* Running' || true)
    [ "$listeners" -ge 4 ] && break
    sleep 5
done
kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory
echo "cluster-up: listeners running: $listeners"
[ "$listeners" -ge 4 ]
