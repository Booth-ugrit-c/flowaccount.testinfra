#!/bin/sh
# Runs as root on a trial VM; reads the runner registration token from stdin.
set -eu

url='' base='' runners=2 reserve_mib=1536 images=''
while [ $# -gt 0 ]; do
    case "$1" in
        --url) url=$2; shift 2 ;;
        --base) base=$2; shift 2 ;;
        --runners) runners=$2; shift 2 ;;
        --reserve-mib) reserve_mib=$2; shift 2 ;;
        --images) images=$2; shift 2 ;;
        *) echo "setup-vm: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$url" ] && [ -n "$base" ] || { echo "setup-vm: --url and --base are required" >&2; exit 2; }
case "$runners" in 1|2|3|4) ;; *) echo "setup-vm: --runners must be 1-4" >&2; exit 2 ;; esac
IFS= read -r token || true
[ -n "$token" ] || { echo "setup-vm: no registration token on stdin" >&2; exit 2; }

pristine=/opt/actions-runner
[ -x "$pristine/config.sh" ] || { echo "setup-vm: $pristine/config.sh missing" >&2; exit 3; }
[ ! -f "$pristine/.runner" ] || { echo "setup-vm: $pristine is already configured; use a fresh VM" >&2; exit 3; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "setup-vm: python3 >= 3.12 required" >&2; exit 3; }
docker info >/dev/null 2>&1 || { echo "setup-vm: docker daemon not reachable" >&2; exit 3; }

total_mib=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
memory_max=$(( (total_mib - reserve_mib) / runners ))
[ "$memory_max" -ge 1024 ] || { echo "setup-vm: ${memory_max} MiB per runner is below 1024" >&2; exit 3; }

for image in $images; do
    docker pull -q "$image" >/dev/null
done

i=1
while [ "$i" -le "$runners" ]; do
    name="$base-$i"
    labels="$base"
    [ "$i" -eq "$runners" ] && labels="$base,$base-sched"
    dir="/opt/actions-runner-$i"
    if [ -f "$dir/.service" ]; then
        (cd "$dir" && ./svc.sh stop && ./svc.sh uninstall) || true
    fi
    rm -rf "$dir"
    cp -a "$pristine" "$dir"
    (cd "$dir" && sudo -u runner ./config.sh --unattended --replace --url "$url" --token "$token" --name "$name" --labels "$labels" --work _work)
    (cd "$dir" && ./svc.sh install runner)
    unit=$(cat "$dir/.service")
    install -d "/etc/systemd/system/$unit.d"
    printf '[Service]\nMemoryMax=%sM\nMemorySwapMax=0\n' "$memory_max" >"/etc/systemd/system/$unit.d/ti-limits.conf"
    systemctl daemon-reload
    (cd "$dir" && ./svc.sh start)
    echo "setup-vm: runner $name labels $labels MemoryMax ${memory_max}M"
    i=$((i + 1))
done

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
echo "setup-vm: images pulled: $(echo $images | wc -w); sampler active: $(systemctl is-active ti-sampler.service)"
