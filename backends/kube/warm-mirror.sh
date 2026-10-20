#!/bin/sh
# Runs as root on a pool node: pulls Docker Hub images through the node-local mirror, then stops host Docker.
set -eu

for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:5000/v2/ >/dev/null 2>&1 && break; sleep 2; done
for image in "$@"; do
    case "$image" in
        */*.*/*|*.*/*) echo "warm-mirror: skipping $image, not on Docker Hub"; continue ;;
        */*) path=$image ;;
        *) path="library/$image" ;;
    esac
    docker pull -q "127.0.0.1:5000/$path" >/dev/null
    echo "warm-mirror: cached $path"
done
systemctl disable --now docker.socket docker.service containerd.service >/dev/null 2>&1 || true
echo "warm-mirror: host docker $(systemctl is-active docker.service || true)"
