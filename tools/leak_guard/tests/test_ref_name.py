import pytest

from helpers import TOKEN, combined, commit, init_repo, run_cli


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo")


@pytest.mark.parametrize("name", [f"refs/heads/feature-{TOKEN}", f"refs/tags/v1-{TOKEN}", TOKEN.upper()])
def test_ref_name_with_a_denied_token_is_found_and_never_printed(repo, name):
    proc = run_cli(repo, "ref-name", name, denylist=TOKEN)
    assert proc.returncode == 1
    assert TOKEN not in combined(proc).lower()
    assert "deny-list entry #1" in proc.stdout


def test_clean_ref_name_exits_zero(repo):
    proc = run_cli(repo, "ref-name", "refs/heads/feature-login", denylist=TOKEN)
    assert proc.returncode == 0
    assert "scanned 1 ref names, 0 findings" in proc.stdout


def test_ref_name_flags_generic_rules_without_printing_the_name(repo):
    address = "dev" + "@" + "corp-domain" + ".io"
    proc = run_cli(repo, "ref-name", f"refs/heads/{address}", denylist=TOKEN)
    assert proc.returncode == 1
    assert address not in combined(proc)


def test_ref_name_require_denylist_without_a_list_is_exit_two(repo):
    proc = run_cli(repo, "ref-name", "refs/heads/x", "--require-denylist")
    assert proc.returncode == 2
    assert "deny-list" in proc.stderr
