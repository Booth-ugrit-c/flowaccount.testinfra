import pytest

from tools.leak_guard import denylist, rules
from tools.leak_guard.scan import check_line

from helpers import TOKEN, j


def ip(*octets):
    return ".".join(str(o) for o in octets)


HITS = {
    "twelve-digit-id": [j("1234", "5678", "9012"), "id=" + j("0000", "0000", "0001") + ";"],
    "ipv4-address": [ip(10, 20, 30, 40), ip(172, 16, 5, 9), ip(192, 168, 1, 10), ip(8, 8, 8, 8), ip(255, 255, 255, 255)],
    "private-key-header": [
        j("-----BEGIN ", "RSA PRIVATE KEY-----"),
        j("-----BEGIN ", "PRIVATE KEY-----"),
        j("-----BEGIN ", "OPENSSH PRIVATE KEY-----"),
    ],
    "github-token": [j(p, "_", "A" * 36) for p in ("ghp", "gho", "ghu", "ghs", "ghr")] + [j("github", "_pat_", "B" * 30)],
    "aws-access-key-id": [j("AKIA", "Q" * 16), j("ASIA", "Q" * 16)],
    "instance-size-runner-label": [j("ubuntu", "-4vcpu", "-16ram"), j("acme", "-8vcpu", "-32ram")],
    "email-address": [
        j("dev", "@", "corp-domain", ".io"),
        j("Dev", "@", "Corp-Domain", ".IO"),
        j("dev", "@", "notexample", ".com"),
    ],
}

NEAR_MISSES = {
    "twelve-digit-id": ["1" * 11, "1" * 13, "no digits here"],
    "ipv4-address": [
        ip(127, 0, 0, 1), ip(127, 255, 255, 254), ip(0, 0, 0, 0), ip(192, 0, 2, 5), ip(198, 51, 100, 7),
        ip(203, 0, 113, 9), ip(256, 1, 1, 1), ip(1, 2, 3), ip(1, 2, 3, 4, 5), ip(999, 999, 999, 999),
    ],
    "private-key-header": [j("-----BEGIN ", "CERTIFICATE-----"), j("-----BEGIN ", "PUBLIC KEY-----")],
    "github-token": ["ghp_short", j("ghp", "_", "A" * 10), j("ghx", "_", "A" * 36)],
    "aws-access-key-id": [j("AKIA", "Q" * 15), j("AKIA", "q" * 16), j("AKIB", "Q" * 16), j("AKIA", "Q" * 17)],
    "instance-size-runner-label": ["ubuntu-latest", "4vcpu-16ram", j("ubuntu-", "4vcpu"), j("ubuntu-4", "vcpu-x", "ram")],
    "email-address": [
        "x@users.noreply.github.com", "noreply@anthropic.com", "dev@example.com", "dev@example.org",
        "dev@host.test", "dev@sub.example.com", "git@github.com", "@main", "pkg@1.2.3", "@scope/pkg", "a@b",
    ],
}


def as_params(table):
    return [pytest.param(rule, text, id=f"{rule}-{n}") for rule, texts in table.items() for n, text in enumerate(texts)]


NO_DENY = denylist.DenyList((), "none")


def test_every_rule_has_hit_and_near_miss_cases():
    for rule in rules.RULES:
        assert rule.name in HITS and rule.name in NEAR_MISSES


@pytest.mark.parametrize("rule,sample", as_params(HITS))
def test_rule_hit(rule, sample):
    names = [n for n, _ in rules.find_generic(f"prefix {sample} suffix")]
    assert rule in names


@pytest.mark.parametrize("rule,sample", as_params(NEAR_MISSES))
def test_rule_near_miss(rule, sample):
    names = [n for n, _ in rules.find_generic(f"prefix {sample} suffix")]
    assert rule not in names


@pytest.mark.parametrize("rule,sample", as_params(HITS))
def test_rule_finding_masks_match(rule, sample):
    labels = check_line(NO_DENY, f"prefix {sample} suffix")
    shown = "\n".join(labels)
    assert any(label.startswith(rule + ": ") for label in labels)
    assert sample not in shown
    assert "prefix " in shown and "***" in shown and " suffix" in shown


@pytest.mark.parametrize("rule,sample", as_params(HITS))
def test_rule_finding_line_also_masks_deny_list_match(rule, sample):
    deny = denylist.parse(TOKEN, "env")
    labels = check_line(deny, f"{TOKEN} {sample}")
    shown = "\n".join(labels)
    assert "deny-list entry #1" in labels
    assert TOKEN not in shown
    assert sample not in shown
