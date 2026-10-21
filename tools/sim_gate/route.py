from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 10
PER_PAGE = 100
MAX_PAGES = 5
MAX_BODY_BYTES = 1 << 20
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
REPOSITORY = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9._-]+/(?!\.{1,2}$)[A-Za-z0-9._-]+$")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_fork_or_unreadable(env, repository):
    event = env.get("GITHUB_EVENT_NAME", "")
    if not event.startswith("pull_request"):
        return False
    try:
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
            head = json.load(handle)["pull_request"]["head"]["repo"]["full_name"]
    except (KeyError, TypeError, OSError, ValueError):
        return True
    return head != repository


def allowed_url(url):
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)


def api_base(env):
    base = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    return base if allowed_url(base) else None


def fetch_page(base, repository, token, page):
    request = urllib.request.Request(
        f"{base}/repos/{repository}/actions/runners?per_page={PER_PAGE}&page={page}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sim-gate-route",
        },
    )
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError("status")
        document = json.loads(response.read(MAX_BODY_BYTES))
    runners = document["runners"]
    if not isinstance(runners, list) or not all(isinstance(r, dict) for r in runners):
        raise ValueError("shape")
    return runners, document.get("total_count", len(runners))


def is_idle_with_label(runner, label):
    if runner.get("status") != "online" or runner.get("busy") is not False:
        return False
    names = {str(item.get("name", "")).casefold() for item in runner.get("labels", []) if isinstance(item, dict)}
    return label.casefold() in names


def decide(env, label):
    token = env.get("SIM_ROUTE_TOKEN", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    base = api_base(env)
    if not token or not label or base is None or not REPOSITORY.match(repository):
        return "cloud"
    if is_fork_or_unreadable(env, repository):
        return "cloud"
    try:
        seen = 0
        for page in range(1, MAX_PAGES + 1):
            runners, total = fetch_page(base, repository, token, page)
            if any(is_idle_with_label(r, label) for r in runners):
                return "onprem"
            seen += len(runners)
            if not runners or seen >= total:
                break
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return "cloud"
    return "cloud"


def main(argv):
    label = argv[0] if argv else ""
    try:
        verdict = decide(os.environ, label)
    except Exception:
        verdict = "cloud"
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
