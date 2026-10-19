import re
import subprocess
import textwrap

import pytest

from helpers import REPO_ROOT, TOKEN, b64, clean_env, combined, commit, git, init_repo, run_cli

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "leak-guard-scheduled.yml"
SECRET_PASS = "Scan branches and tags with the deny-list"
PULL_PASS = "Scan pull request refs with generic rules only"


def step_block(name):
    lines = WORKFLOW.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("      - name: ")), len(lines))
    return lines[start:end]


def step_script(name):
    block = step_block(name)
    for i, line in enumerate(block):
        if line.strip() == "run: |":
            return textwrap.dedent("\n".join(block[i + 1 :]))
        if line.strip().startswith("run: "):
            return line.strip()[5:]
    raise AssertionError(f"no run script in step {name}")


@pytest.fixture
def repo(tmp_path):
    repo = init_repo(tmp_path / "repo")
    commit(repo, {"a.txt": "clean\n"})
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def blob_commit(repo, files, ref):
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "--detach")
    sha = commit(repo, files)
    git(repo, "update-ref", ref, sha)
    git(repo, "checkout", "-q", base)
    git(repo, "checkout", "-q", "main")
    return sha


def scan_all(repo, *args, denylist=TOKEN):
    return run_cli(repo, "scan-all", *args, denylist=denylist)


SCOPE = ["--refs", "refs/remotes/origin/", "--refs", "refs/tags/", "--require-denylist"]


def test_pull_ref_content_is_outside_the_deny_list_scope(repo):
    blob_commit(repo, {"guess.txt": f"{TOKEN}\n"}, "refs/remotes/pull/99/head")
    proc = scan_all(repo, *SCOPE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "deny-list entry" not in proc.stdout


def test_the_same_blob_on_a_branch_is_still_reported(repo):
    blob_commit(repo, {"guess.txt": f"{TOKEN}\n"}, "refs/remotes/origin/feature")
    proc = scan_all(repo, *SCOPE)
    assert proc.returncode == 1
    assert proc.stdout.count("deny-list entry #1") == 1
    assert TOKEN not in combined(proc)


def test_a_pull_ref_name_is_not_checked_in_the_secret_scope(repo):
    git(repo, "update-ref", f"refs/remotes/pull/{TOKEN}/head", "HEAD")
    assert scan_all(repo, *SCOPE).returncode == 0


def test_refs_option_is_repeatable_and_counts_only_the_selected_refs(repo):
    git(repo, "tag", "v1")
    blob_commit(repo, {"x.txt": "x\n"}, "refs/remotes/pull/1/head")
    proc = scan_all(repo, "--refs", "refs/remotes/origin/", "--refs", "refs/tags/")
    assert "scan-all 1 commits, 2 refs" in proc.stdout


def test_a_prefix_that_matches_nothing_is_exit_two(repo):
    proc = scan_all(repo, "--refs", "refs/remotes/nowhere/")
    assert proc.returncode == 2
    assert "no refs" in proc.stderr


def test_one_matching_prefix_is_enough_when_another_matches_nothing(repo):
    assert scan_all(repo, "--refs", "refs/remotes/nowhere/", "--refs", "refs/remotes/origin/").returncode == 0


def run_step(repo, name, tmp_path, denylist=None):
    env = clean_env(RUNNER_TEMP=str(tmp_path))
    if denylist is not None:
        env["LEAK_GUARD_DENYLIST"] = b64(denylist)
    return subprocess.run(
        ["sh", "-c", step_script(name)], cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def test_workflow_pull_pass_reports_a_generic_hit_as_a_warning_and_exits_zero(repo, tmp_path):
    address = "dev" + "@" + "corp-domain" + ".io"
    blob_commit(repo, {"mail.txt": f"contact {address}\n"}, "refs/remotes/pull/99/head")
    proc = run_step(repo, PULL_PASS, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning::" in proc.stdout
    assert "email-address" in proc.stdout
    assert not any(line.startswith("::") and "warning" not in line for line in proc.stdout.splitlines())
    assert address not in combined(proc)


def test_workflow_pull_pass_with_no_findings_has_no_warning(repo, tmp_path):
    blob_commit(repo, {"fine.txt": "fine\n"}, "refs/remotes/pull/99/head")
    proc = run_step(repo, PULL_PASS, tmp_path)
    assert proc.returncode == 0
    assert "::warning::" not in proc.stdout


def test_workflow_pull_pass_ignores_the_deny_list_even_if_one_is_present(repo, tmp_path):
    blob_commit(repo, {"guess.txt": f"{TOKEN}\n"}, "refs/remotes/pull/99/head")
    proc = run_step(repo, PULL_PASS, tmp_path, denylist=TOKEN)
    assert proc.returncode == 0
    assert "deny-list entry" not in proc.stdout


def test_workflow_pull_pass_never_names_the_secret(repo):
    text = "\n".join(step_block(PULL_PASS))
    assert "secrets." not in text
    assert "        env:" not in text
    assert text.count("LEAK_GUARD_DENYLIST") == text.count("-u LEAK_GUARD_DENYLIST")


def test_workflow_pull_pass_with_no_pull_refs_does_not_fail_the_job(repo, tmp_path):
    proc = run_step(repo, PULL_PASS, tmp_path)
    assert proc.returncode == 0


def test_workflow_secret_pass_fails_on_a_branch_hit_and_ignores_a_pull_hit(repo, tmp_path):
    blob_commit(repo, {"guess.txt": f"{TOKEN}\n"}, "refs/remotes/pull/99/head")
    assert run_step(repo, SECRET_PASS, tmp_path, denylist=TOKEN).returncode == 0
    blob_commit(repo, {"leak.txt": f"{TOKEN}\n"}, "refs/remotes/origin/feature")
    proc = run_step(repo, SECRET_PASS, tmp_path, denylist=TOKEN)
    assert proc.returncode == 1
    assert "deny-list entry #1" in proc.stdout


def test_workflow_secret_pass_without_a_list_is_exit_two(repo, tmp_path):
    assert run_step(repo, SECRET_PASS, tmp_path).returncode == 2


def test_workflow_fetch_refspecs_land_where_the_passes_look(repo):
    script = "\n".join(step_block("Fetch every branch, tag and pull request ref"))
    assert "'+refs/heads/*:refs/remotes/origin/*'" in script
    assert "'+refs/pull/*:refs/remotes/pull/*'" in script
    assert re.search(r"--refs refs/remotes/origin/ --refs refs/tags/", step_script(SECRET_PASS))
    assert "--refs refs/remotes/pull/" in step_script(PULL_PASS)
