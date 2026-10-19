import pytest

from helpers import TOKEN, combined, init_repo, run_cli


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "repo")


def test_scan_text_finds_a_token_on_the_right_line_without_echoing_it(repo):
    proc = run_cli(repo, "scan-text", denylist=TOKEN, input_text=f"title\r\nbody has {TOKEN} inside\r\nend\r\n")
    assert proc.returncode == 1
    assert "stdin:2 deny-list entry #1" in proc.stdout
    assert TOKEN not in combined(proc)


def test_scan_text_clean_input_exits_zero(repo):
    proc = run_cli(repo, "scan-text", denylist=TOKEN, input_text="nothing here\n")
    assert proc.returncode == 0
    assert "0 findings" in proc.stdout


def test_scan_text_applies_the_generic_rules_and_masks_the_match(repo):
    address = "dev" + "@" + "corp-domain" + ".io"
    proc = run_cli(repo, "scan-text", denylist=TOKEN, input_text=f"mail {address} and {TOKEN}\n")
    assert proc.returncode == 1
    assert "stdin:1 email-address: mail *** and ***" in proc.stdout
    assert address not in combined(proc) and TOKEN not in combined(proc)


def test_scan_text_require_denylist_without_a_list_is_exit_two(repo):
    proc = run_cli(repo, "scan-text", "--require-denylist", input_text="x\n")
    assert proc.returncode == 2
    assert "deny-list" in proc.stderr


def test_scan_text_works_outside_a_git_repository(tmp_path):
    proc = run_cli(tmp_path, "scan-text", denylist=TOKEN, input_text=f"{TOKEN}\n")
    assert proc.returncode == 1
