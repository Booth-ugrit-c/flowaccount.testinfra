import base64

import pytest

from tools.leak_guard import denylist

from helpers import b64, clean_env, commit, init_repo, run_cli, write


def test_parse_skips_comments_blanks_and_crlf():
    deny = denylist.parse("# comment\r\n\r\n  alpha  \r\n#beta\r\ngamma\r\n", "file")
    assert [e.number for e in deny.entries] == [1, 2]
    assert deny.find("alpha") == [(1, [(0, 5)])]
    assert deny.find("beta") == []
    assert deny.find("xgamma") == [(2, [(1, 6)])]


def test_parse_keeps_hash_inside_an_entry():
    assert denylist.parse("a#b", "env").find("xa#bx") == [(1, [(1, 4)])]


def test_mask_offsets_stay_exact_after_a_character_whose_lowercase_is_longer():
    deny = denylist.parse("alpha", "env")
    line = "İ ALPHA tail"
    spans = [s for _, found in deny.find(line) for s in found]
    assert denylist.mask(line, spans) == "İ *** tail"


def test_substring_entry_is_case_insensitive():
    deny = denylist.parse("Alpha", "env")
    assert deny.find("xx ALPHA yy") == [(1, [(3, 8)])]
    assert deny.find("beta") == []


def test_regex_entry_is_case_insensitive():
    deny = denylist.parse("re:acme-[a-z]+-host", "env")
    assert deny.find("see ACME-Big-HOST now") == [(1, [(4, 17)])]
    assert deny.find("acme-1-host") == []


def test_substring_entry_is_not_a_regex():
    assert denylist.parse("a.c", "env").find("abc") == []


@pytest.mark.parametrize("bad", ["re:(", "re:", "re:   "])
def test_bad_regex_is_a_config_error_that_does_not_echo_the_entry(bad):
    with pytest.raises(denylist.ConfigError) as info:
        denylist.parse("fine\n" + bad, "env")
    assert "#2" in str(info.value) and "(" not in str(info.value)


@pytest.fixture
def work(tmp_path):
    base = tmp_path / "work"
    repo = init_repo(base / "repo")
    commit(repo, {"hit.txt": "alpha\nbeta\ngamma\n"})
    return base, repo


def lines_flagged(proc):
    return sorted(l.split()[0] for l in proc.stdout.splitlines() if l.startswith("hit.txt:"))


def test_env_content_beats_file_env_and_sibling(work, tmp_path):
    base, repo = work
    (base / "_testinfra-private").mkdir()
    write(base, "_testinfra-private/leak-guard-denylist.txt", "gamma\n")
    write(tmp_path, "other.txt", "beta\n")
    env = clean_env(LEAK_GUARD_DENYLIST_FILE=str(tmp_path / "other.txt"))
    proc = run_cli(repo, "scan", denylist="alpha", env=env)
    assert lines_flagged(proc) == ["hit.txt:1"]
    assert "(source: env)" in proc.stdout


def test_file_env_beats_sibling(work, tmp_path):
    base, repo = work
    (base / "_testinfra-private").mkdir()
    write(base, "_testinfra-private/leak-guard-denylist.txt", "gamma\n")
    write(tmp_path, "other.txt", "beta\n")
    proc = run_cli(repo, "scan", env=clean_env(LEAK_GUARD_DENYLIST_FILE=str(tmp_path / "other.txt")))
    assert lines_flagged(proc) == ["hit.txt:2"]
    assert "(source: file)" in proc.stdout


def test_sibling_file_used_when_no_env(work):
    base, repo = work
    (base / "_testinfra-private").mkdir()
    write(base, "_testinfra-private/leak-guard-denylist.txt", "﻿gamma\r\n")
    proc = run_cli(repo, "scan")
    assert lines_flagged(proc) == ["hit.txt:3"]
    assert "deny-list 1 entries (source: file)" in proc.stdout


def test_no_source_means_empty_list_and_exit_zero_without_require(work):
    _, repo = work
    proc = run_cli(repo, "scan")
    assert proc.returncode == 0
    assert "deny-list 0 entries (source: none)" in proc.stdout


def test_empty_env_value_falls_through_to_the_next_source(work, tmp_path):
    _, repo = work
    write(tmp_path, "other.txt", "beta\n")
    env = clean_env(LEAK_GUARD_DENYLIST_FILE=str(tmp_path / "other.txt"), LEAK_GUARD_DENYLIST="  ")
    proc = run_cli(repo, "scan", env=env)
    assert lines_flagged(proc) == ["hit.txt:2"]


@pytest.mark.parametrize("command", [["scan"], ["push-range", "0" * 40, "HEAD"]])
def test_require_denylist_without_a_list_is_exit_two(work, command):
    _, repo = work
    proc = run_cli(repo, *command, "--require-denylist")
    assert proc.returncode == 2


@pytest.mark.parametrize("content", ["", "# only a comment\n\n"])
def test_require_denylist_with_an_empty_list_is_exit_two(work, content):
    _, repo = work
    assert run_cli(repo, "scan", "--require-denylist", denylist=content).returncode == 2


def test_require_denylist_with_a_list_scans(work):
    _, repo = work
    assert run_cli(repo, "scan", "--require-denylist", denylist="zzz-not-present").returncode == 0


def test_missing_deny_list_file_is_exit_two(work, tmp_path):
    _, repo = work
    env = clean_env(LEAK_GUARD_DENYLIST_FILE=str(tmp_path / "absent.txt"))
    assert run_cli(repo, "scan", env=env).returncode == 2


def test_invalid_regex_entry_is_exit_two_and_not_echoed(work):
    _, repo = work
    proc = run_cli(repo, "scan", denylist="re:acme-(unclosed")
    assert proc.returncode == 2
    assert "acme-(unclosed" not in proc.stdout + proc.stderr


def test_env_deny_list_is_read_as_one_line_of_base64(work):
    _, repo = work
    env = clean_env(LEAK_GUARD_DENYLIST=b64("alpha\nre:gam+a\n"))
    proc = run_cli(repo, "scan", env=env)
    assert lines_flagged(proc) == ["hit.txt:1", "hit.txt:3"]
    assert "deny-list 2 entries (source: env)" in proc.stdout


def test_env_base64_tolerates_surrounding_whitespace_and_wrapping(work):
    _, repo = work
    encoded = b64("alpha\n")
    env = clean_env(LEAK_GUARD_DENYLIST=f"\n{encoded[:4]}\n{encoded[4:]}\n")
    assert lines_flagged(run_cli(repo, "scan", env=env)) == ["hit.txt:1"]


@pytest.mark.parametrize("value", ["not base64 at all!", "alpha", "abcd", "%%%%"])
def test_env_that_is_not_base64_text_is_exit_two_and_quotes_nothing(work, value):
    _, repo = work
    proc = run_cli(repo, "scan", env=clean_env(LEAK_GUARD_DENYLIST=value))
    assert proc.returncode == 2
    assert value not in proc.stdout + proc.stderr
    assert "base64" in proc.stderr


WIDE_LISTS = [
    pytest.param("utf-16-le", b"", id="utf-16-le-no-bom"),
    pytest.param("utf-16-le", b"\xff\xfe", id="utf-16-le-bom"),
    pytest.param("utf-16-be", b"", id="utf-16-be-no-bom"),
    pytest.param("utf-16-be", b"\xfe\xff", id="utf-16-be-bom"),
    pytest.param("utf-32-le", b"", id="utf-32-le-no-bom"),
    pytest.param("utf-32", b"", id="utf-32-bom"),
]


@pytest.mark.parametrize("encoding,bom", WIDE_LISTS)
def test_wide_encoded_deny_list_file_is_exit_two(work, tmp_path, encoding, bom):
    _, repo = work
    path = tmp_path / "wide.txt"
    path.write_bytes(bom + "alpha\nbeta\n".encode(encoding))
    proc = run_cli(repo, "scan", env=clean_env(LEAK_GUARD_DENYLIST_FILE=str(path)))
    assert proc.returncode == 2
    assert "alpha" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("encoding,bom", WIDE_LISTS)
def test_wide_encoded_deny_list_env_is_exit_two(work, encoding, bom):
    _, repo = work
    raw = bom + "alpha\nbeta\n".encode(encoding)
    proc = run_cli(repo, "scan", env=clean_env(LEAK_GUARD_DENYLIST=base64.b64encode(raw).decode()))
    assert proc.returncode == 2
    assert "alpha" not in proc.stdout + proc.stderr


def test_wide_encoded_sibling_deny_list_is_exit_two(work):
    base, repo = work
    (base / "_testinfra-private").mkdir()
    (base / "_testinfra-private" / "leak-guard-denylist.txt").write_bytes("alpha\n".encode("utf-16"))
    assert run_cli(repo, "scan").returncode == 2
