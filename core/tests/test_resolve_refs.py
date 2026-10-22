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
    def run(head, base="origin/main", ref="refs/heads/main", default="main", **inputs):
        output = tmp_path / "output.txt"
        output.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "PR_", "BASE_REF", "DEFAULT_BRANCH", "RETEST_OF", "LANES", "KNOWN_LANES"))}
        env.update(PR_BRANCH=head, BASE_REF=base, DEFAULT_BRANCH=default, GITHUB_REF=ref, GITHUB_OUTPUT=output.as_posix(), KNOWN_LANES="unit integration")
        dispatched = ref.removeprefix("refs/heads/")
        if ref.startswith("refs/heads/") and dispatched in repo:
            env.update(GITHUB_SHA=repo[dispatched])
        env.update(inputs)
        proc = subprocess.run(
            [request.param, SCRIPT.as_posix()], cwd=repo["dir"], env=env, capture_output=True, text=True, timeout=60
        )
        values = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return proc, values

    return run


def test_a_branch_resolves_to_its_tip_and_is_not_the_default_tip(repo, resolve):
    proc, out = resolve("topic")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out == {
        "pr_sha": repo["topic"], "base_sha": repo["main"], "default_tip": "false", "pr_number": "", "pr_branch": "topic",
        "pr_author": "", "retest_of": "", "lane_unit": "true", "lane_integration": "true",
    }


def test_the_four_pr_inputs_are_carried_into_the_outputs(repo, resolve):
    proc, out = resolve("topic", PR_SHA=repo["topic"], PR_NUMBER="17", PR_AUTHOR="dependabot[bot]", RETEST_OF="39789348836")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out["pr_sha"], out["pr_branch"], out["pr_number"], out["pr_author"], out["retest_of"]) == (repo["topic"], "topic", "17", "dependabot[bot]", "39789348836")


def test_pr_sha_wins_over_the_branch_tip_when_dispatched_from_another_ref(repo, resolve):
    proc, out = resolve("main", ref="refs/heads/topic", PR_SHA=repo["first"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["first"] and out["default_tip"] == "false"


BINDING = "the tested commit is not the dispatched commit"


def test_an_older_pr_sha_dispatched_on_the_pr_branch_is_refused(repo, resolve):
    proc, out = resolve("main", PR_SHA=repo["first"])
    assert proc.returncode != 0 and out == {}
    assert BINDING in proc.stdout


def test_an_older_pr_sha_on_a_feature_branch_dispatched_there_is_refused(repo, resolve):
    proc, out = resolve("topic", ref="refs/heads/topic", PR_SHA=repo["first"])
    assert proc.returncode != 0 and out == {}
    assert BINDING in proc.stdout


def test_the_dispatched_commit_as_pr_sha_on_the_pr_branch_is_accepted(repo, resolve):
    proc, out = resolve("topic", ref="refs/heads/topic", PR_SHA=repo["topic"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["topic"]


def test_a_branch_tip_that_moved_past_the_dispatched_commit_is_refused(repo, resolve):
    proc, out = resolve("topic", ref="refs/heads/topic", GITHUB_SHA=repo["first"])
    assert proc.returncode != 0 and out == {}
    assert BINDING in proc.stdout


def test_a_dispatch_without_the_dispatched_commit_is_refused_on_the_pr_branch(repo, resolve):
    proc, out = resolve("topic", ref="refs/heads/topic", GITHUB_SHA="")
    assert proc.returncode != 0 and out == {}
    assert BINDING in proc.stdout


def test_pr_sha_alone_resolves_when_it_is_on_a_branch(repo, resolve):
    proc, out = resolve("", PR_SHA=repo["topic"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["topic"] and out["pr_branch"] == ""


@pytest.mark.parametrize("branch, which", [("main", "topic"), ("topic", "stray")])
def test_pr_sha_must_be_an_ancestor_of_pr_branch(repo, resolve, branch, which):
    proc, out = resolve(branch, PR_SHA=repo[which])
    assert proc.returncode != 0 and out == {}
    assert "::error::" in proc.stdout


def test_pr_sha_on_pr_branch_is_accepted_below_the_tip(repo, resolve):
    proc, out = resolve("topic", PR_SHA=repo["first"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["first"]


def test_a_pr_branch_that_does_not_exist_is_refused_even_with_a_valid_sha(repo, resolve):
    proc, out = resolve("absent", PR_SHA=repo["first"])
    assert proc.returncode != 0 and out == {}
    assert "pr_branch is not a branch" in proc.stdout


@pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, "0" * 39, "0" * 41, "main"])
def test_pr_sha_must_be_a_full_lowercase_sha(resolve, sha):
    proc, out = resolve("main", PR_SHA=sha)
    assert proc.returncode != 0 and out == {}
    assert "pr_sha must be a full lowercase commit SHA" in proc.stdout


@pytest.mark.parametrize("branch", ["refs/heads/topic", "-x", "a..b", "to pic"])
def test_pr_branch_is_checked_even_when_pr_sha_is_given(repo, resolve, branch):
    proc, out = resolve(branch, PR_SHA=repo["main"])
    assert proc.returncode != 0 and out == {}


@pytest.mark.parametrize("number", ["", "1", "1234567890"])
def test_pr_number_accepts_digits_or_empty(resolve, number):
    proc, out = resolve("topic", PR_NUMBER=number)
    assert proc.returncode == 0 and out["pr_number"] == number


@pytest.mark.parametrize("number", ["12a", "-1", "1 2", "12345678901", "1\n2", "#3"])
def test_pr_number_refuses_anything_but_digits(resolve, number):
    proc, out = resolve("topic", PR_NUMBER=number)
    assert proc.returncode != 0 and out == {}
    assert "pr_number must be digits or empty" in proc.stdout


@pytest.mark.parametrize("author", ["", "octo-cat", "a" * 39, "dependabot[bot]", "A1-b2"])
def test_pr_author_accepts_a_login_or_a_bot(resolve, author):
    proc, out = resolve("topic", PR_AUTHOR=author)
    assert proc.returncode == 0 and out["pr_author"] == author


@pytest.mark.parametrize("author", ["a" * 40, "a b", "x;y", "[bot]", "bot[bot][bot]", "name_1", "a\nb", "$(id)"])
def test_pr_author_refuses_anything_else(resolve, author):
    proc, out = resolve("topic", PR_AUTHOR=author)
    assert proc.returncode != 0 and out == {}
    assert "pr_author must be a GitHub login or empty" in proc.stdout


@pytest.mark.parametrize("retest", ["1a", "-1", "1 2", "1" * 21])
def test_retest_of_must_be_a_run_id(resolve, retest):
    proc, out = resolve("topic", RETEST_OF=retest)
    assert proc.returncode != 0 and out == {}
    assert "retest_of must be a run id or empty" in proc.stdout


@pytest.mark.parametrize(
    "lanes, unit, integration",
    [("", "true", "true"), ("unit", "true", "false"), ("integration", "false", "true"), ("integration,unit", "true", "true")],
)
def test_lanes_select_known_lanes_and_empty_selects_all(resolve, lanes, unit, integration):
    proc, out = resolve("topic", LANES=lanes, RETEST_OF="5")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out["lane_unit"], out["lane_integration"]) == (unit, integration)


@pytest.mark.parametrize("lanes", ["unit", "integration", "unit,integration"])
def test_lanes_on_a_first_run_are_refused(resolve, lanes):
    proc, out = resolve("topic", LANES=lanes)
    assert proc.returncode != 0 and out == {}
    assert "lanes may narrow a retest only" in proc.stdout


@pytest.mark.parametrize("lanes", ["e2e", "unit,e2e", "unit,", ",unit", "Unit", "unit integration", "uni"])
def test_lanes_refuse_unknown_or_malformed_names(resolve, lanes):
    proc, out = resolve("topic", LANES=lanes, RETEST_OF="5")
    assert proc.returncode != 0 and out == {}
    assert "lanes" in proc.stdout


@pytest.mark.parametrize("known", ["", " ", "Unit", "unit,integration"])
def test_a_workflow_that_names_no_usable_lanes_is_refused(resolve, known):
    proc, out = resolve("topic", KNOWN_LANES=known)
    assert proc.returncode != 0 and out == {}
    assert "the workflow must name its lanes" in proc.stdout


def test_the_default_branch_dispatched_from_itself_is_the_default_tip(repo, resolve):
    proc, out = resolve("main")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["main"] and out["default_tip"] == "true"


def test_the_full_sha_of_the_default_tip_is_the_default_tip_too(repo, resolve):
    proc, out = resolve(repo["main"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["main"] and out["default_tip"] == "true"


@pytest.mark.parametrize("ref", ["refs/heads/topic", "refs/tags/main", "refs/heads/MAIN", "refs/heads/Main", ""])
def test_the_default_tip_needs_a_dispatch_from_the_default_branch_itself(resolve, ref):
    proc, out = resolve("main", ref=ref)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["default_tip"] == "false"


def test_an_older_commit_of_the_default_branch_runs_but_is_not_the_tip(repo, resolve):
    proc, out = resolve(repo["first"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["pr_sha"] == repo["first"] and out["default_tip"] == "false"


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


@pytest.mark.parametrize("base", ["origin/topic", "origin/topic~0"])
def test_a_base_off_the_default_branch_is_refused(resolve, base):
    proc, out = resolve("topic", base=base)
    assert proc.returncode != 0 and out == {}
    assert "base must be a commit on the default branch" in proc.stdout


def test_a_base_sha_off_the_default_branch_is_refused(repo, resolve):
    proc, out = resolve("topic", base=repo["topic"])
    assert proc.returncode != 0 and out == {}
    assert "base must be a commit on the default branch" in proc.stdout


@pytest.mark.parametrize("base, wanted", [("origin/main", "main"), ("origin/main~1", "first"), ("first", "first")])
def test_a_base_on_the_default_branch_resolves_to_its_sha(repo, resolve, base, wanted):
    proc, out = resolve("topic", base=repo["first"] if base == "first" else base)
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
