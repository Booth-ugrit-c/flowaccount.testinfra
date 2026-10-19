import re

import pytest

from helpers import TOKEN, ZERO_SHA, combined, commit, git, init_repo, run_cli


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo")


def push_range(repo, base, head, denylist=TOKEN, *extra):
    return run_cli(repo, "push-range", base, head, *extra, denylist=denylist)


def test_tree_count_equals_git_ls_tree(repo):
    base = commit(repo, {"a.txt": "x\n"})
    head = commit(repo, {".hidden/b.txt": "x\n", "sp ace/c.txt": "x\n", "bin.dat": b"\x00\x01"})
    proc = push_range(repo, base, head)
    listed = git(repo, "ls-tree", "-r", "--name-only", head).stdout.splitlines()
    assert proc.returncode == 0
    assert len(listed) == 4
    assert int(re.search(r"scanned (\d+) files", proc.stdout).group(1)) == len(listed)


def test_token_added_in_one_commit_and_removed_in_the_next_is_found(repo):
    base = commit(repo, {"a.txt": "x\n"})
    commit(repo, {"notes.txt": f"1\n2\n{TOKEN}\n"})
    head = commit(repo, {"notes.txt": None})
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert re.search(r"blob [0-9a-f]{12} notes.txt:3 deny-list entry #1", proc.stdout)
    assert TOKEN not in combined(proc)


def test_binary_added_then_removed_is_found(repo):
    base = commit(repo, {"a.txt": "x\n"})
    commit(repo, {"blob.dat": b"\x00\xff" + TOKEN.encode() + b"\x00"})
    head = commit(repo, {"blob.dat": None})
    assert push_range(repo, base, head).returncode == 1


def test_token_in_commit_message_is_found(repo):
    base = commit(repo, {"a.txt": "x\n"})
    head = commit(repo, {"b.txt": "y\n"}, message=f"test: subject\n\nbody mentions {TOKEN}\n")
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert f"commit {head[:12]} message:3 deny-list entry #1" in proc.stdout
    assert TOKEN not in combined(proc)


def test_denied_author_name_is_found_without_printing_it(repo):
    base = commit(repo, {"a.txt": "x\n"})
    head = commit(repo, {"b.txt": "y\n"}, author=f"{TOKEN} <author@example.com>")
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert f"commit {head[:12]} author deny-list entry #1" in proc.stdout
    assert TOKEN not in combined(proc)


@pytest.mark.parametrize("rule", ["email-address"])
def test_author_email_not_allowed_is_found_without_printing_it(repo, rule):
    base = commit(repo, {"a.txt": "x\n"})
    address = "dev" + "@" + "corp-domain" + ".io"
    head = commit(repo, {"b.txt": "y\n"}, author=f"Someone <{address}>")
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert f"commit {head[:12]} author {rule}" in proc.stdout
    assert address not in combined(proc)


@pytest.mark.parametrize("address", [
    "1+name@users.noreply.github.com", "noreply@anthropic.com", "a@example.com", "a@example.org", "a@host.test",
])
def test_allowed_author_emails_are_clean(repo, address):
    base = commit(repo, {"a.txt": "x\n"})
    head = commit(repo, {"b.txt": "y\n"}, author=f"Team <{address}>")
    assert push_range(repo, base, head).returncode == 0


def test_only_commits_after_base_are_inspected_for_added_lines(repo):
    commit(repo, {"old.txt": f"{TOKEN}\n"})
    base = commit(repo, {"old.txt": "scrubbed\n"})
    head = commit(repo, {"new.txt": "fine\n"})
    assert push_range(repo, base, head).returncode == 0


def test_new_branch_zero_base_walks_whole_history_without_remotes(repo):
    commit(repo, {"a.txt": "x\n"})
    commit(repo, {"notes.txt": f"{TOKEN}\n"})
    head = commit(repo, {"notes.txt": None})
    assert push_range(repo, ZERO_SHA, head).returncode == 1


def test_new_branch_zero_base_skips_commits_already_on_a_remote(repo, tmp_path):
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    git(repo, "remote", "add", "origin", str(bare))
    commit(repo, {"a.txt": "x\n"})
    pushed = commit(repo, {"notes.txt": f"{TOKEN}\n"})
    git(repo, "push", "-q", "origin", "main", env=None)
    head = commit(repo, {"notes.txt": None})
    proc = push_range(repo, ZERO_SHA, head)
    assert proc.returncode == 0
    assert pushed[:12] not in proc.stdout


def test_unknown_base_falls_back_to_the_remote_exclusion_walk(repo):
    commit(repo, {"notes.txt": f"{TOKEN}\n"})
    head = commit(repo, {"notes.txt": None})
    assert push_range(repo, "1" * 40, head).returncode == 1


def test_unknown_head_is_exit_two(repo):
    base = commit(repo, {"a.txt": "x\n"})
    assert push_range(repo, base, "2" * 40).returncode == 2


def test_tree_at_head_is_scanned_even_with_no_new_commits(repo):
    head = commit(repo, {"notes.txt": f"{TOKEN}\n"})
    assert push_range(repo, head, head).returncode == 1


def test_path_containing_the_token_is_not_printed(repo):
    base = commit(repo, {"a.txt": "x\n"})
    commit(repo, {f"{TOKEN}.txt": "y\n"})
    head = commit(repo, {f"{TOKEN}.txt": None})
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert TOKEN not in combined(proc)


def test_added_line_numbers_follow_hunks_in_a_modified_file(repo):
    base = commit(repo, {"f.txt": "a\nb\nc\nd\n"})
    head = commit(repo, {"f.txt": f"a\nX\nb\nc\nd\n{TOKEN}\n"})
    proc = push_range(repo, base, head)
    assert "f.txt:6 deny-list entry #1" in proc.stdout


def test_a_removed_line_is_not_reported_as_added(repo):
    base = commit(repo, {"f.txt": f"keep\n{TOKEN}\n"})
    head = commit(repo, {"f.txt": "keep\n"})
    assert push_range(repo, base, head).returncode == 0


def test_line_starting_with_plus_plus_plus_in_added_content_does_not_confuse_the_parser(repo):
    base = commit(repo, {"a.txt": "x\n"})
    head = commit(repo, {"f.txt": f"++ one\n{TOKEN}\n"})
    assert f"f.txt:2 deny-list entry #1" in push_range(repo, base, head).stdout


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_wide_encoded_token_added_then_removed_is_found(repo, encoding):
    base = commit(repo, {"a.txt": "x\n"})
    commit(repo, {"wide.txt": f"first\n{TOKEN}\n".encode(encoding)})
    head = commit(repo, {"wide.txt": None})
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert TOKEN not in combined(proc)


def test_token_in_the_inner_tag_of_a_tag_of_a_tag_is_found(repo):
    base = commit(repo, {"a.txt": "x\n"})
    git(repo, "tag", "-a", "-m", f"note {TOKEN}", "inner", "HEAD")
    git(repo, "tag", "-a", "-m", "clean outer message", "outer", "inner")
    assert git(repo, "cat-file", "-p", "outer").stdout.split("\n")[1] == "type tag"
    head = git(repo, "rev-parse", "outer").stdout.strip()
    proc = push_range(repo, ZERO_SHA, head)
    assert proc.returncode == 1
    assert TOKEN not in combined(proc)


def test_token_in_an_extra_commit_header_is_found(repo):
    base = commit(repo, {"a.txt": "x\n"})
    tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    stamp = "1700000000 +0000"
    raw = (
        f"tree {tree}\nparent {base}\nauthor Test Author <author@example.com> {stamp}\n"
        f"committer Test Author <author@example.com> {stamp}\nx-note {TOKEN}\n\ntest: crafted\n"
    )
    path = repo / ".git" / "crafted-commit.txt"
    path.write_text(raw, encoding="utf-8", newline="\n")
    head = git(repo, "hash-object", "-t", "commit", "-w", "--literally", str(path)).stdout.strip()
    proc = push_range(repo, base, head)
    assert proc.returncode == 1
    assert TOKEN not in combined(proc)
