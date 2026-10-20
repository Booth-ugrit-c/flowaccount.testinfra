#!/bin/sh
# Runs as root on a trial VM; reads the runner registration token from stdin.
set -eu

url='' name='' labels=''
while [ $# -gt 0 ]; do
    case "$1" in
        --url) url=$2; shift 2 ;;
        --name) name=$2; shift 2 ;;
        --labels) labels=$2; shift 2 ;;
        *) echo "setup-vm: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$url" ] && [ -n "$name" ] && [ -n "$labels" ] || { echo "setup-vm: --url, --name and --labels are required" >&2; exit 2; }
IFS= read -r token || true
[ -n "$token" ] || { echo "setup-vm: no registration token on stdin" >&2; exit 2; }

dir=/opt/actions-runner
[ -x "$dir/config.sh" ] || { echo "setup-vm: $dir/config.sh missing" >&2; exit 3; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "setup-vm: python3 >= 3.12 required" >&2; exit 3; }
docker info >/dev/null 2>&1 || { echo "setup-vm: docker daemon not reachable" >&2; exit 3; }

if [ -f "$dir/.service" ]; then
    (cd "$dir" && ./svc.sh stop && ./svc.sh uninstall) || true
fi
(cd "$dir" && sudo -u runner ./config.sh --unattended --replace --url "$url" --token "$token" --name "$name" --labels "$labels" --work _work)
(cd "$dir" && ./svc.sh install runner && ./svc.sh start)

install -d -m 0755 /opt/ti-sampler /var/lib/ti-sampler
install -m 0644 /tmp/ti-sample.py /opt/ti-sampler/sample.py
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
echo "setup-vm: runner $name registered with labels $labels; sampler active: $(systemctl is-active ti-sampler.service)"
