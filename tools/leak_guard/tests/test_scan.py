import re

import pytest

from helpers import TOKEN, combined, commit, git, init_repo, run_cli, write


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo")


def count_line(proc):
    return int(re.search(r"scanned (\d+) files, (\d+) findings", proc.stdout).group(1))


def test_token_in_dot_directory_is_found(repo):
    commit(repo, {".hidden/notes.txt": f"line one\nsecret {TOKEN} here\n", "readme.md": "clean\n"})
    proc = run_cli(repo, "scan", denylist=TOKEN)
    assert proc.returncode == 1
    assert ".hidden/notes.txt:2 deny-list entry #1" in proc.stdout
    assert "scanned 2 files, 1 findings" in proc.stdout


def test_clean_repo_exits_zero(repo):
    commit(repo, {"a.txt": "nothing to see\n"})
    proc = run_cli(repo, "scan", denylist=TOKEN)
    assert proc.returncode == 0
    assert "scanned 1 files, 0 findings" in proc.stdout


def test_scanned_count_equals_git_ls_files(repo):
    commit(repo, {
        ".github/workflows/ci.yml": "x\n", ".hidden/.deeper/a": "x\n", "sp ace/file name.txt": "x\n",
        "unicode/café.txt": "x\n", "bin/blob.dat": b"\x00\x01\x02\xff\xfe", "empty.txt": "",
    })
    write(repo, "untracked.txt", f"{TOKEN}\n")
    proc = run_cli(repo, "scan", denylist=TOKEN)
    listed = git(repo, "ls-files").stdout.splitlines()
    assert proc.returncode == 0
    assert count_line(proc) == len(listed) == 6


def test_binary_file_is_scanned_with_replacement_decoding(repo):
    commit(repo, {"bin/blob.dat": b"\x00\xff\xfe" + TOKEN.encode() + b"\x00\x80"})
    proc = run_cli(repo, "scan", denylist=TOKEN)
    assert proc.returncode == 1
    assert "bin/blob.dat:1 deny-list entry #1" in proc.stdout


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_file_is_found(repo, encoding):
    commit(repo, {"wide.txt": f"first\n{TOKEN}\n".encode(encoding)})
    proc = run_cli(repo, "scan", denylist=TOKEN)
    assert proc.returncode == 1
    assert "wide.txt:2 deny-list entry #1" in proc.stdout
    assert "3 findings" not in proc.stdout


def test_working_tree_content_is_scanned_not_the_index(repo):
    commit(repo, {"a.txt": "clean\n"})
    write(repo, "a.txt", f"{TOKEN}\n")
    assert run_cli(repo, "scan", denylist=TOKEN).returncode == 1


def test_indexed_path_missing_on_disk_is_exit_two(repo):
    commit(repo, {"a.txt": "clean\n", "b.txt": "clean\n"})
    (repo / "b.txt").unlink()
    proc = run_cli(repo, "scan", denylist=TOKEN)
    assert proc.returncode == 2
    assert "b.txt" not in combined(proc)


def test_outside_a_git_repo_is_exit_two(tmp_path):
    assert run_cli(tmp_path, "scan", denylist=TOKEN).returncode == 2


@pytest.mark.parametrize("entries", [TOKEN, "re:acme-[a-z]+-host", f"{TOKEN.upper()}"])
def test_output_never_contains_the_entry_or_its_match(repo, entries):
    commit(repo, {"a.txt": f"before {TOKEN} after\n", "b.txt": f"{TOKEN}{TOKEN}\n"})
    proc = run_cli(repo, "scan", denylist=entries)
    assert proc.returncode == 1
    out = combined(proc).lower()
    assert TOKEN not in out
    assert entries.lower().removeprefix("re:") not in out
    assert "deny-list entry #1" in proc.stdout


def test_a_path_containing_the_token_is_not_printed(repo):
    commit(repo, {f"{TOKEN}/notes.txt": f"{TOKEN}\n"})
    proc = run_cli(repo, "scan", denylist=TOKEN)
    assert proc.returncode == 1
    assert TOKEN not in combined(proc)
    assert "path matched a rule" in proc.stdout


def test_findings_count_is_per_entry_per_line(repo):
    commit(repo, {"a.txt": "alpha beta\ngamma\n"})
    proc = run_cli(repo, "scan", denylist="alpha\nbeta\nre:gam+a")
    assert "a.txt:1 deny-list entry #1" in proc.stdout
    assert "a.txt:1 deny-list entry #2" in proc.stdout
    assert "a.txt:2 deny-list entry #3" in proc.stdout
    assert "3 findings" in proc.stdout


def test_crlf_files_match_like_lf_files(repo):
    commit(repo, {"a.txt": f"one\r\n{TOKEN}\r\n"})
    assert "a.txt:2 deny-list entry #1" in run_cli(repo, "scan", denylist=TOKEN).stdout
