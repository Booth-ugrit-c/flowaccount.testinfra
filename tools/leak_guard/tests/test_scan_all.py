import re

import pytest

from helpers import TOKEN, combined, commit, git, init_repo, run_cli, write


@pytest.fixture
def repo(tmp_path):
    repo = init_repo(tmp_path / "repo")
    commit(repo, {"a.txt": "clean\n"})
    return repo


def scan_all(repo, denylist=TOKEN, *extra):
    return run_cli(repo, "scan-all", *extra, denylist=denylist)


def assert_found_without_echo(proc):
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert TOKEN not in combined(proc).lower()


def test_clean_repository_exits_zero_and_prints_counts(repo):
    proc = scan_all(repo)
    assert proc.returncode == 0
    assert re.search(r"leak-guard: scan-all 1 commits, 1 refs", proc.stdout)
    assert "0 findings" in proc.stdout


def test_blob_on_a_branch_that_is_not_checked_out_is_found(repo):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"notes.txt": f"{TOKEN}\n"})
    git(repo, "checkout", "-q", "main")
    assert not (repo / "notes.txt").exists()
    proc = scan_all(repo)
    assert_found_without_echo(proc)
    assert re.search(r"blob [0-9a-f]{12} notes.txt:1 deny-list entry #1", proc.stdout)


def test_blob_on_an_orphan_branch_is_found(repo):
    git(repo, "checkout", "-q", "--orphan", "orphan")
    git(repo, "rm", "-rq", "--cached", ".")
    commit(repo, {"lone.txt": f"{TOKEN}\n"})
    git(repo, "checkout", "-q", "-f", "main")
    assert_found_without_echo(scan_all(repo))


def test_commit_message_on_an_orphan_branch_is_found(repo):
    git(repo, "checkout", "-q", "--orphan", "orphan")
    git(repo, "rm", "-rq", "--cached", ".")
    commit(repo, {"lone.txt": "clean\n"}, message=f"test: note {TOKEN}")
    git(repo, "checkout", "-q", "-f", "main")
    proc = scan_all(repo)
    assert_found_without_echo(proc)
    assert re.search(r"commit [0-9a-f]{12} message:1 deny-list entry #1", proc.stdout)


def test_token_only_in_a_tag_message_is_found(repo):
    git(repo, "tag", "-a", "-m", f"release {TOKEN}", "v1", "HEAD")
    proc = scan_all(repo)
    assert_found_without_echo(proc)
    assert re.search(r"tag [0-9a-f]{12} message:1 deny-list entry #1", proc.stdout)


def test_token_in_the_inner_tag_of_a_nested_tag_is_found(repo):
    git(repo, "tag", "-a", "-m", f"inner {TOKEN}", "inner", "HEAD")
    git(repo, "tag", "-a", "-m", "clean outer", "outer", "inner")
    git(repo, "tag", "-d", "inner")
    assert git(repo, "cat-file", "-p", "outer").stdout.split("\n")[1] == "type tag"
    assert_found_without_echo(scan_all(repo))


def test_tree_entry_name_that_exists_only_on_a_side_branch_is_found(repo):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"a2.txt": "", f"{TOKEN}.conf": ""})
    git(repo, "rm", "-q", "--", f"{TOKEN}.conf")
    git(repo, "commit", "-q", "-m", "test: remove")
    git(repo, "checkout", "-q", "main")
    proc = scan_all(repo)
    assert_found_without_echo(proc)
    assert re.search(r"tree [0-9a-f]{12} name deny-list entry #1", proc.stdout)


def test_utf16_blob_on_a_side_branch_is_found(repo):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"wide.txt": f"first\n{TOKEN}\n".encode("utf-16-le")})
    git(repo, "checkout", "-q", "main")
    assert_found_without_echo(scan_all(repo))


@pytest.mark.parametrize("kind", ["branch", "tag"])
def test_ref_name_containing_the_token_is_found(repo, kind):
    git(repo, kind, f"x-{TOKEN}")
    proc = scan_all(repo)
    assert_found_without_echo(proc)
    assert re.search(r"ref #\d+:1 deny-list entry #1", proc.stdout)


def test_pull_request_style_remote_ref_is_covered(repo):
    git(repo, "checkout", "-q", "-b", "side")
    sha = commit(repo, {"notes.txt": f"{TOKEN}\n"})
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "side")
    git(repo, "update-ref", "refs/remotes/pull/7/merge", sha)
    assert_found_without_echo(scan_all(repo))


def test_working_tree_is_not_scanned(repo):
    write(repo, "untracked.txt", f"{TOKEN}\n")
    write(repo, "a.txt", f"{TOKEN}\n")
    proc = scan_all(repo)
    assert proc.returncode == 0
    assert "0 findings" in proc.stdout


def test_require_denylist_without_a_list_is_exit_two(repo):
    assert scan_all(repo, None, "--require-denylist").returncode == 2
