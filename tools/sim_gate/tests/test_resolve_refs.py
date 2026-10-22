from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "resolve_refs.sh"
SHELLS = [shutil.which(name) for name in ("bash", "sh") if shutil.which(name)]
GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]

pytestmark = pytest.mark.skipif(not SHELLS, reason="a POSIX shell is required")


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd, name):
    (Path(cwd) / f"{name}.txt").write_text(name)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("resolve")
    upstream = root / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    first = commit(upstream, "first")
    main_tip = commit(upstream, "second")
    git(upstream, "checkout", "-q", "-b", "topic")
    topic = commit(upstream, "topic")
    git(upstream, "checkout", "-q", "--detach", first)
    stray = commit(upstream, "stray")
    git(upstream, "tag", "MAIN", stray)
    git(upstream, "checkout", "-q", "--detach", first)
    pull = commit(upstream, "pull")
    git(upstream, "update-ref", "refs/pull/7/head", pull)
    git(upstream, "branch", "refs/x", main_tip)
    git(upstream, "checkout", "-q", "main")
    clone = root / "clone"
    subprocess.run([*GIT, "clone", "-q", upstream.as_posix(), clone.as_posix()], check=True, capture_output=True)
    git(clone, "fetch", "-q", "origin", "+refs/pull/7/head:refs/remotes/pull/7/head")
    return {"dir": clone, "first": first, "main": main_tip, "topic": topic, "stray": stray, "pull": pull}


@pytest.fixture(params=SHELLS)
def resolve(repo, tmp_path, request):
    def run(head, base="origin/main", ref="refs/heads/main", default="main"):
        output = tmp_path / "output.txt"
        output.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "HEAD_REF", "BASE_REF", "DEFAULT_BRANCH"))}
        env.update(HEAD_REF=head, BASE_REF=base, DEFAULT_BRANCH=default, GITHUB_REF=ref, GITHUB_OUTPUT=output.as_posix())
        proc = subprocess.run(
            [request.param, SCRIPT.as_posix()], cwd=repo["dir"], env=env, capture_output=True, text=True, timeout=60
        )
        values = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return proc, values

    return run


def test_a_branch_resolves_to_its_tip_and_is_not_the_default_tip(repo, resolve):
    proc, out = resolve("topic")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out == {"head_sha": repo["topic"], "base_sha": repo["main"], "default_tip": "false"}


def test_the_default_branch_dispatched_from_itself_is_the_default_tip(repo, resolve):
    proc, out = resolve("main")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["head_sha"] == repo["main"] and out["default_tip"] == "true"


def test_the_full_sha_of_the_default_tip_is_the_default_tip_too(repo, resolve):
    proc, out = resolve(repo["main"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["head_sha"] == repo["main"] and out["default_tip"] == "true"


@pytest.mark.parametrize("ref", ["refs/heads/topic", "refs/tags/main", "refs/heads/MAIN", "refs/heads/Main", ""])
def test_the_default_tip_needs_a_dispatch_from_the_default_branch_itself(resolve, ref):
    proc, out = resolve("main", ref=ref)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["default_tip"] == "false"


def test_an_older_commit_of_the_default_branch_runs_but_is_not_the_tip(repo, resolve):
    proc, out = resolve(repo["first"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["head_sha"] == repo["first"] and out["default_tip"] == "false"


@pytest.mark.parametrize(
    "head",
    [
        "refs/pull/7/head", "pull/7/head", "refs/heads/topic", "refs/remotes/origin/topic", "MAIN", "Main",
        "origin/main", "topic~1", "topic^", "main..topic", "-main", "", "to pic", "absent",
    ],
)
def test_anything_but_a_branch_or_a_full_sha_is_refused(resolve, head):
    proc, out = resolve(head)
    assert proc.returncode != 0
    assert out == {}
    assert "::error::" in proc.stdout


@pytest.mark.parametrize("which", ["stray", "pull"])
def test_a_commit_on_no_branch_is_refused_even_when_it_is_in_the_clone(repo, resolve, which):
    proc, out = resolve(repo[which])
    assert proc.returncode != 0 and out == {}
    assert "not on a branch" in proc.stdout


def test_a_short_or_unknown_sha_is_refused(repo, resolve):
    assert resolve(repo["topic"][:12])[0].returncode != 0
    assert resolve("0" * 40)[0].returncode != 0


@pytest.mark.parametrize("base, wanted", [("origin/main", "main"), ("origin/main~1", "first"), ("origin/topic", "topic")])
def test_a_base_on_a_branch_resolves_to_its_sha(repo, resolve, base, wanted):
    proc, out = resolve("topic", base=base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["base_sha"] == repo[wanted]


@pytest.mark.parametrize("base", ["refs/pull/7/head", "MAIN", "-x", "", "a..b", "origin/absent"])
def test_a_base_outside_the_branches_is_refused(repo, resolve, base):
    proc, out = resolve("topic", base=base)
    assert proc.returncode != 0 and out == {}


def test_a_branch_named_like_a_ref_is_refused_as_head(resolve):
    proc, out = resolve("refs/x")
    assert proc.returncode != 0 and out == {}
    assert "::error::head must be a branch" in proc.stdout


@pytest.mark.parametrize("base", ["refs/x", "refs/remotes/origin/refs/x"])
def test_a_branch_named_like_a_ref_is_refused_as_base(resolve, base):
    proc, out = resolve("topic", base=base)
    assert proc.returncode != 0 and out == {}
    assert "::error::base must name a commit" in proc.stdout


def test_a_base_on_no_branch_is_refused(repo, resolve):
    for which in ("stray", "pull"):
        proc, out = resolve("topic", base=repo[which])
        assert proc.returncode != 0 and out == {}


@pytest.mark.parametrize("default", ["", "-main", "ma in", "../main"])
def test_an_unusable_default_branch_name_is_refused(resolve, default):
    assert resolve("main", default=default)[0].returncode != 0
