import re
from pathlib import Path

KUBE = Path(__file__).resolve().parents[1]
UNITS = {"m": 0.001, "": 1, "Mi": 1, "Gi": 1024}
REQUESTS_RE = re.compile(r"requests:\n\s+cpu: (\w+)\n\s+memory: (\w+)")
QUOTA_RE = re.compile(r'requests\.cpu: "?(\w+)"?\n\s+requests\.memory: (\w+)\n\s+pods: "?(\d+)"?')


def quantity(text: str, memory: bool) -> float:
    number, unit = re.fullmatch(r"(\d+)([A-Za-z]*)", text).groups()
    if memory:
        return int(number) * {"Mi": 1, "Gi": 1024}[unit]
    return int(number) * UNITS[unit]


def container_requests(text: str) -> list[tuple[float, float]]:
    return [(quantity(cpu, False), quantity(mem, True)) for cpu, mem in REQUESTS_RE.findall(text)]


def scale_sets_per_lane() -> dict[str, int]:
    script = (KUBE / "cluster-up.sh").read_text(encoding="utf-8")
    pairs = re.search(r"^for set in (.+?); do$", script, re.M).group(1).split()
    lanes: dict[str, int] = {}
    for pair in pairs:
        lane = pair.split(":")[0]
        lanes[lane] = lanes.get(lane, 0) + 1
    return lanes


def full_pool() -> tuple[float, float, int]:
    cpu = memory = pods = 0
    for lane, sets in scale_sets_per_lane().items():
        text = (KUBE / "values" / f"{lane}.yaml").read_text(encoding="utf-8")
        runners = int(re.search(r"^maxRunners: (\d+)$", text, re.M).group(1)) * sets
        per_pod = container_requests(text)
        cpu += runners * sum(c for c, _ in per_pod)
        memory += runners * sum(m for _, m in per_pod)
        pods += runners
    return cpu, memory, pods


def test_full_pool_is_read_from_the_manifests():
    assert scale_sets_per_lane() == {"unit": 2, "integration": 2}
    assert full_pool() == (26.0, 36 * 1024, 16)


def test_namespace_quota_admits_a_full_pool_and_the_mirror():
    pool = (KUBE / "manifests" / "pool.yaml").read_text(encoding="utf-8")
    quota = QUOTA_RE.search(pool.split("kind: ResourceQuota", 1)[1])
    mirror = container_requests(pool.split("kind: DaemonSet", 1)[1])
    cpu, memory, pods = full_pool()
    cpu += sum(c for c, _ in mirror)
    memory += sum(m for _, m in mirror)
    assert quantity(quota.group(1), False) >= cpu
    assert quantity(quota.group(2), True) >= memory
    assert int(quota.group(3)) >= pods + 1
