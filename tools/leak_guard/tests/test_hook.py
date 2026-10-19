import shutil
import subprocess

import pytest

from helpers import REPO_ROOT, TOKEN, b64, clean_env, combined, commit, git, init_repo, install_guard


@pytest.fixture
def pair(tmp_path):
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    work = init_repo(tmp_path / "work")
    git(work, "remote", "add", "origin", str(bare))
    install_guard(work)
    commit(work, {"README.md": "clean\n"}, message="chore: guard files")
    return bare, work


def push(work, *refspec, denylist=TOKEN):
    env = clean_env(LEAK_GUARD_DENYLIST=b64(denylist)) if denylist is not None else clean_env()
    return git(work, "push", "origin", *refspec, env=env, check=False)


def remote_head(bare, ref="refs/heads/main"):
    proc = git(bare, "rev-parse", "--verify", "-q", ref, check=False)
    return proc.stdout.strip() or None


def test_clean_push_to_a_new_branch_is_accepted(pair):
    bare, work = pair
    proc = push(work, "main")
    assert proc.returncode == 0, combined(proc)
    assert remote_head(bare) == git(work, "rev-parse", "HEAD").stdout.strip()


def test_denied_commit_is_refused_and_the_remote_is_untouched(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    before = remote_head(bare)
    commit(work, {"notes.txt": f"{TOKEN}\n"})
    proc = push(work, "main")
    assert proc.returncode != 0
    assert "refused" in combined(proc)
    assert TOKEN not in combined(proc)
    assert remote_head(bare) == before


def test_clean_follow_up_push_is_accepted(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    head = commit(work, {"more.txt": "fine\n"})
    assert push(work, "main").returncode == 0
    assert remote_head(bare) == head


def test_add_then_remove_pair_is_refused(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    before = remote_head(bare)
    commit(work, {"notes.txt": f"{TOKEN}\n"})
    commit(work, {"notes.txt": None})
    proc = push(work, "main")
    assert proc.returncode != 0
    assert remote_head(bare) == before


def test_new_branch_carrying_a_denied_commit_is_refused(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    git(work, "checkout", "-q", "-b", "feature")
    commit(work, {"notes.txt": f"{TOKEN}\n"})
    assert push(work, "feature").returncode != 0
    assert remote_head(bare, "refs/heads/feature") is None


def test_deletion_of_a_cleanly_named_branch_is_accepted(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    git(work, "branch", "doomed")
    assert push(work, "doomed").returncode == 0
    proc = push(work, ":doomed")
    assert proc.returncode == 0, combined(proc)
    assert remote_head(bare, "refs/heads/doomed") is None


def test_push_without_a_deny_list_is_refused(pair):
    bare, work = pair
    proc = push(work, "main", denylist=None)
    assert proc.returncode != 0
    assert remote_head(bare) is None


def test_hook_refuses_when_no_python_is_usable(pair, tmp_path):
    _, work = pair
    empty = tmp_path / "empty-path"
    empty.mkdir()
    stdin = "refs/heads/main " + "1" * 40 + " refs/heads/main " + "0" * 40 + "\n"
    proc = subprocess.run(
        [shutil.which("sh"), str(work / ".githooks" / "pre-push")], input=stdin, capture_output=True,
        text=True, cwd=work, env={**clean_env(), "PATH": str(empty)},
    )
    assert proc.returncode == 1
    assert "no working python" in proc.stderr


@pytest.mark.skipif(not (REPO_ROOT / ".git").exists(), reason="needs the real repository")
def test_hook_executable_bit_is_recorded_in_git():
    staged = git(REPO_ROOT, "ls-files", "-s", ".githooks/pre-push").stdout
    assert staged.startswith("100755 ")


def test_push_to_a_denied_branch_name_is_refused(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    proc = push(work, f"main:refs/heads/feature-{TOKEN}")
    assert proc.returncode != 0
    assert remote_head(bare, f"refs/heads/feature-{TOKEN}") is None


def test_push_of_a_denied_tag_name_is_refused(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    git(work, "tag", f"v1-{TOKEN}")
    proc = push(work, f"refs/tags/v1-{TOKEN}")
    assert proc.returncode != 0
    assert remote_head(bare, f"refs/tags/v1-{TOKEN}") is None


def test_deleting_a_denied_branch_name_is_refused(pair):
    bare, work = pair
    assert push(work, "main").returncode == 0
    name = f"refs/heads/feature-{TOKEN}"
    git(work, "push", "--no-verify", "origin", f"main:{name}")
    assert remote_head(bare, name) is not None
    proc = push(work, f":{name}")
    assert proc.returncode != 0
    assert remote_head(bare, name) is not None


def test_a_second_remote_does_not_hide_history_from_the_scan(pair, tmp_path):
    bare, work = pair
    other = tmp_path / "other.git"
    other.mkdir()
    git(other, "init", "-q", "--bare", "-b", "main")
    git(work, "remote", "add", "other", str(other))
    assert push(work, "main").returncode == 0
    git(work, "checkout", "-q", "-b", "leaky")
    commit(work, {"s.txt": f"{TOKEN}\n"})
    commit(work, {"s.txt": None})
    git(work, "push", "--no-verify", "other", "leaky:refs/heads/x")
    proc = push(work, "leaky")
    assert proc.returncode != 0
    assert remote_head(bare, "refs/heads/leaky") is None
