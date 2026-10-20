#!/bin/sh
# Runs as root on a pool node from /tmp/ti-kube; an agent reads the join token from stdin.
set -eu

role='' server=''
while [ $# -gt 0 ]; do
    case "$1" in
        --role) role=$2; shift 2 ;;
        --server) server=$2; shift 2 ;;
        *) echo "node-setup: unknown argument $1" >&2; exit 2 ;;
    esac
done
case "$role" in server) ;; agent) [ -n "$server" ] || { echo "node-setup: an agent needs --server" >&2; exit 2; } ;; *) echo "node-setup: --role server|agent" >&2; exit 2 ;; esac
here=/tmp/ti-kube
. "$here/versions.env"
docker info >/dev/null 2>&1 || { echo "node-setup: docker is needed to build the runner image" >&2; exit 3; }

docker build -q -t "$RUNNER_IMAGE" --build-arg RUNNER_BASE="$RUNNER_BASE" -f "$here/runner.Dockerfile" "$here" >/dev/null

reserved="--kubelet-arg=kube-reserved=cpu=200m,memory=512Mi --kubelet-arg=system-reserved=cpu=100m,memory=256Mi"
curl -fsSL "https://raw.githubusercontent.com/k3s-io/k3s/$(echo "$K3S_VERSION" | sed 's/+/%2B/')/install.sh" -o "$here/k3s-install.sh"
if [ "$role" = server ]; then
    INSTALL_K3S_VERSION=$K3S_VERSION sh "$here/k3s-install.sh" server --disable traefik --disable servicelb --write-kubeconfig-mode 0600 $reserved
else
    IFS= read -r token || true
    [ -n "$token" ] || { echo "node-setup: no join token on stdin" >&2; exit 2; }
    K3S_URL="https://$server:6443" K3S_TOKEN=$token INSTALL_K3S_VERSION=$K3S_VERSION sh "$here/k3s-install.sh" agent $reserved
fi

for _ in $(seq 1 60); do k3s crictl info >/dev/null 2>&1 && break; sleep 2; done
docker save "$RUNNER_IMAGE" | k3s ctr -n k8s.io images import - >/dev/null
k3s crictl pull "$DIND_IMAGE" >/dev/null
k3s crictl pull "$MIRROR_IMAGE" >/dev/null

install -d -m 0755 /opt/ti-sampler /var/lib/ti-sampler /var/lib/ti-mirror
install -m 0644 "$here/sample.py" /opt/ti-sampler/sample.py
cat >/etc/systemd/system/ti-sampler.service <<'UNIT'
[Unit]
Description=testinfra 1 s CPU and memory sampler
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /opt/ti-sampler/sample.py --out /var/lib/ti-sampler/samples.csv --interval 1
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ti-sampler.service
echo "node-setup: $role ready; k3s $(k3s --version | head -1 | cut -d' ' -f3); images: $(k3s crictl images -q | wc -l); sampler $(systemctl is-active ti-sampler.service)"
