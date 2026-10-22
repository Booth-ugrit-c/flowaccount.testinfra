from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "security_scan.sh"
CONFIG = SCRIPT.parent / "security"
SH = shutil.which("sh")
GIT = shutil.which("git")
LANES = ["secret-scan", "security-scan"]

pytestmark = pytest.mark.skipif(SH is None or GIT is None, reason="sh and git are required")

FAKE = """\
import json, os, sys
from pathlib import Path

home = Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
(home / "args.json").write_text(json.dumps(args))
files = sorted(Path(root, name).as_posix() for root, _, names in os.walk(".") for name in names)
(home / "files.json").write_text(json.dumps(files))
if os.path.exists(".semgrepignore"):
    (home / "semgrepignore").write_text(Path(".semgrepignore").read_text())
spec = json.loads((home / "spec.json").read_text())
report = spec.get("report")
if report is not None:
    if isinstance(report, dict) and report.get("paths") == "auto":
        report["paths"] = {"scanned": [name for name in files if name != ".semgrepignore"]}
    text = report if isinstance(report, str) else json.dumps(report)
    for flag in ("--report-path", "--output"):
        if flag in args:
            Path(args[args.index(flag) + 1]).write_text(text)
sys.exit(spec.get("rc", 0))
"""

WRAPPER = '#!/bin/sh\nexec "$FAKE_PY" "$FAKE_DIR/fake.py" "$@"\n'


def fake_token():
    return "SIMFAKE-" + "".join(["ab"] * 16)


def clean_report(lane):
    return [] if lane == "secret-scan" else {"results": [], "errors": [], "paths": "auto"}


def finding_report(lane, path="app/main.py", line=3):
    if lane == "secret-scan":
        return [{"RuleID": "sim-fake-token", "File": "./" + path, "StartLine": line, "Secret": fake_token()}]
    return {
        "results": [{"check_id": "core.security.sim-python-eval", "path": path, "start": {"line": line}}],
        "errors": [],
        "paths": "auto",
    }


class Lab:
    def __init__(self, tmp_path):
        self.repo = tmp_path / "repo"
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        (self.fake / "fake.py").write_text(FAKE)
        self.bin = self.fake / "scanner"
        self.bin.write_text(WRAPPER, newline="\n")
        self.bin.chmod(0o755)
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "sim")
        self.git("config", "core.autocrlf", "false")
        self.write("keep.txt", "keep\n")
        self.write("edit.txt", "before\n")
        self.write("gone.txt", "gone\n")
        self.write("old/name.txt", "".join(f"line {i}\n" for i in range(40)))
        self.write(".gitignore", "ignored/\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, newline="\n")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def scan(self, lane, base=None, rc=0, report="clean", env=None, args=None):
        if report == "clean":
            report = clean_report(lane)
        (self.fake / "spec.json").write_text(json.dumps({"rc": rc, "report": report}))
        for name in ("args.json", "files.json", "semgrepignore"):
            (self.fake / name).unlink(missing_ok=True)
        full = dict(os.environ)
        full.pop("GITHUB_OUTPUT", None)
        full.pop("SIM_SECURITY_CONFIG", None)
        full.update(
            SIM_GITLEAKS=self.bin.as_posix(),
            SIM_SEMGREP=self.bin.as_posix(),
            SIM_PYTHON=Path(sys.executable).as_posix(),
            FAKE_PY=Path(sys.executable).as_posix(),
            FAKE_DIR=self.fake.as_posix(),
        )
        full.update(env or {})
        command = [SH, SCRIPT.as_posix(), *(args if args is not None else [lane, base or self.base])]
        return subprocess.run(command, cwd=self.repo, env=full, capture_output=True, text=True, timeout=120)

    def staged(self):
        return [name for name in json.loads((self.fake / "files.json").read_text()) if name != ".semgrepignore"]

    def args(self):
        return json.loads((self.fake / "args.json").read_text())


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def last_line(proc):
    return proc.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("lane", LANES)
def test_clean_scan_passes(lab, lane):
    lab.write("app/main.py", "print(1)\n")
    proc = lab.scan(lane)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert last_line(proc) == f"{lane}: pass"


@pytest.mark.parametrize("lane", LANES)
def test_findings_fail_and_never_print_the_secret(lab, lane):
    lab.write("app/main.py", "print(1)\n")
    proc = lab.scan(lane, rc=1, report=finding_report(lane))
    assert proc.returncode == 1
    assert last_line(proc) == f"{lane}: findings 1"
    rule = "sim-fake-token" if lane == "secret-scan" else "sim-python-eval"
    assert f"{lane}: finding {rule} app/main.py:3" in proc.stdout
    assert fake_token() not in proc.stdout + proc.stderr


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("rc", [2, 7, 126])
def test_other_exit_codes_are_scanner_errors_even_with_an_empty_report(lab, lane, rc):
    proc = lab.scan(lane, rc=rc)
    assert proc.returncode == 1
    assert last_line(proc) == f"{lane}: scanner error: exit code {rc}"


@pytest.mark.parametrize("lane", LANES)
def test_missing_binary_is_a_scanner_error(lab, lane):
    missing = (lab.fake / "no-such-scanner").as_posix()
    proc = lab.scan(lane, env={"SIM_GITLEAKS": missing, "SIM_SEMGREP": missing})
    assert proc.returncode == 1
    assert last_line(proc).startswith(f"{lane}: scanner error: exit code 127")


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("report", [None, "{not json", "null", '"text"'])
def test_missing_or_unparsable_report_is_a_scanner_error(lab, lane, report):
    proc = lab.scan(lane, report=report)
    assert proc.returncode == 1
    assert ": scanner error: " in last_line(proc)


@pytest.mark.parametrize("lane", LANES)
def test_exit_zero_with_findings_is_a_scanner_error(lab, lane):
    lab.write("app/main.py", "print(1)\n")
    proc = lab.scan(lane, rc=0, report=finding_report(lane))
    assert proc.returncode == 1
    assert last_line(proc) == f"{lane}: scanner error: exit 0 with 1 findings"


@pytest.mark.parametrize("lane", LANES)
def test_exit_one_without_findings_is_a_scanner_error(lab, lane):
    proc = lab.scan(lane, rc=1)
    assert proc.returncode == 1
    assert last_line(proc) == f"{lane}: scanner error: exit 1 with no findings"


def test_semgrep_errors_are_scanner_errors(lab):
    lab.write("app/main.py", "print(1)\n")
    report = {"results": [], "errors": [{"type": ["PartialParsing", []], "path": "app/main.py"}], "paths": "auto"}
    proc = lab.scan("security-scan", report=report)
    assert proc.returncode == 1
    assert "security-scan: semgrep error PartialParsing app/main.py" in proc.stdout
    assert last_line(proc) == "security-scan: scanner error: semgrep reported 1 errors"


def test_staged_source_missing_from_semgrep_scanned_paths_is_a_scanner_error(lab):
    lab.write("app/main.py", "print(1)\n")
    lab.write("web/app.js", "let a = 1;\n")
    proc = lab.scan("security-scan", report={"results": [], "errors": [], "paths": {"scanned": ["web/app.js"]}})
    assert proc.returncode == 1
    assert "security-scan: not scanned app/main.py" in proc.stdout
    assert last_line(proc) == "security-scan: scanner error: 1 staged files not scanned"


def test_changed_set_is_acmrt_plus_untracked_without_deleted_or_ignored(lab):
    lab.write("edit.txt", "after\n")
    (lab.repo / "gone.txt").unlink()
    (lab.repo / "new").mkdir()
    lab.git("mv", "old/name.txt", "new/name.txt")
    lab.write("added.txt", "added\n")
    lab.commit("head")
    lab.write("keep.txt", "uncommitted edit\n")
    lab.write("loose/untracked.txt", "untracked\n")
    lab.write("ignored/build.txt", "ignored\n")
    proc = lab.scan("secret-scan")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert lab.staged() == ["added.txt", "edit.txt", "keep.txt", "loose/untracked.txt", "new/name.txt"]
    assert "secret-scan: 5 changed files vs " in proc.stdout


def test_type_change_from_symlink_to_file_is_scanned(lab):
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=lab.repo, input="target", capture_output=True, text=True, check=True
    ).stdout.strip()
    lab.git("update-index", "--add", "--cacheinfo", f"120000,{blob},flip")
    lab.git("commit", "-q", "-m", "link")
    base = lab.git("rev-parse", "HEAD").strip()
    lab.git("rm", "-q", "--cached", "flip")
    lab.write("flip", "now a file\n")
    lab.commit("file")
    assert lab.git("diff", "--name-status", base).split() == ["T", "flip"]
    proc = lab.scan("secret-scan", base=base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert lab.staged() == ["flip"]


def test_symlink_in_the_work_tree_is_skipped_not_followed(lab, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    try:
        os.symlink(outside, lab.repo / "link.txt")
    except OSError:
        pytest.skip("symlinks are not available")
    proc = lab.scan("secret-scan")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "secret-scan: skipped symlink link.txt" in proc.stdout
    assert lab.staged() == []


@pytest.mark.parametrize("lane", LANES)
def test_scanner_ignore_files_are_never_staged(lab, lane):
    lab.write(".gitleaksignore", "x\n")
    lab.write(".semgrepignore", "*\n")
    lab.write("sub/.semgrepignore", "*\n")
    lab.write("sub/app.py", "print(1)\n")
    proc = lab.scan(lane)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert lab.staged() == ["sub/app.py"]
    assert f"{lane}: skipped scanner ignore file sub/.semgrepignore" in proc.stdout


def test_semgrep_gets_an_empty_root_semgrepignore(lab):
    lab.write(".semgrepignore", "*\n")
    lab.write("tests/app.py", "print(1)\n")
    proc = lab.scan("security-scan")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (lab.fake / "semgrepignore").read_text() == ""


def test_gitleaks_flags(lab):
    assert lab.scan("secret-scan").returncode == 0
    args = lab.args()
    assert args[0] == "dir" and args[-1] == "."
    for flag in ("--redact", "--ignore-gitleaks-allow", "--no-banner"):
        assert flag in args
    assert args[args.index("--exit-code") + 1] == "1"
    assert args[args.index("--report-format") + 1] == "json"
    assert Path(args[args.index("--config") + 1]).resolve() == (CONFIG / "gitleaks.toml").resolve()


def test_semgrep_flags(lab):
    assert lab.scan("security-scan").returncode == 0
    args = lab.args()
    assert args[0] == "scan" and args[-1] == "."
    for flag in ("--json", "--error", "--metrics=off", "--disable-nosem", "--no-git-ignore", "--max-target-bytes=0"):
        assert flag in args
    assert Path(args[args.index("--config") + 1]).resolve() == (CONFIG / "semgrep.yml").resolve()


@pytest.mark.parametrize("lane", LANES)
def test_config_directory_comes_from_sim_security_config(lab, tmp_path, lane):
    trusted = tmp_path / "trusted"
    shutil.copytree(CONFIG, trusted)
    assert lab.scan(lane, env={"SIM_SECURITY_CONFIG": trusted.as_posix()}).returncode == 0
    args = lab.args()
    name = "gitleaks.toml" if lane == "secret-scan" else "semgrep.yml"
    assert Path(args[args.index("--config") + 1]).resolve() == (trusted / name).resolve()


def test_missing_config_is_a_scanner_error(lab, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = lab.scan("secret-scan", env={"SIM_SECURITY_CONFIG": empty.as_posix()})
    assert proc.returncode == 1
    assert last_line(proc) == "secret-scan: scanner error: config gitleaks.toml not found"


@pytest.mark.parametrize("lane", LANES)
def test_scanner_runs_on_an_empty_changed_set(lab, lane):
    proc = lab.scan(lane)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{lane}: 0 changed files vs " in proc.stdout
    assert (lab.fake / "args.json").exists()


def test_report_parser_ignores_modules_planted_in_the_work_tree(lab):
    lab.write("json.py", "import sys\nprint('pass')\nsys.exit(0)\n")
    proc = lab.scan("secret-scan", rc=1, report=finding_report("secret-scan", path="json.py", line=1))
    assert proc.returncode == 1
    assert last_line(proc) == "secret-scan: findings 1"


@pytest.mark.parametrize(
    "rc, report, result",
    [(0, "clean", "pass"), (1, "finding", "findings"), (2, "clean", "scanner-error")],
)
def test_github_output_result(lab, tmp_path, rc, report, result):
    out = tmp_path / "github_output"
    lab.write("app/main.py", "print(1)\n")
    body = finding_report("secret-scan") if report == "finding" else "clean"
    lab.scan("secret-scan", rc=rc, report=body, env={"GITHUB_OUTPUT": out.as_posix()})
    assert out.read_text() == f"result={result}\n"


@pytest.mark.parametrize(
    "args, reason",
    [
        ([], "usage: security_scan.sh secret-scan|security-scan BASE"),
        (["all", "HEAD"], "usage: security_scan.sh secret-scan|security-scan BASE"),
        (["secret-scan"], "BASE is missing"),
        (["secret-scan", "no-such-ref"], "base is not a commit: no-such-ref"),
    ],
)
def test_bad_arguments_are_scanner_errors(lab, args, reason):
    proc = lab.scan("secret-scan", args=args)
    assert proc.returncode == 1
    assert last_line(proc).endswith(f"scanner error: {reason}")
    assert not (lab.fake / "args.json").exists()


def test_gitleaks_config_holds_only_the_synthetic_rule():
    text = (CONFIG / "gitleaks.toml").read_text()
    assert text.count("[[rules]]") == 1
    assert "[extend]" not in text and "useDefault" not in text
    regexes = re.findall(r"^regex = '''(.*)'''$", text, re.M)
    assert regexes == ["SIMFAKE-[0-9a-f]{32}"]
    rule = re.compile(regexes[0])
    assert rule.search("token " + fake_token())
    assert not rule.search("SIMFAKE-" + "".join(["AB"] * 16))
    assert not rule.search("gh" + "p_" + "a" * 36)


def test_semgrep_rules_are_vendored_and_synthetic():
    text = (CONFIG / "semgrep.yml").read_text()
    ids = re.findall(r"^  - id: (\S+)$", text, re.M)
    assert ids and all(rule.startswith("sim-") for rule in ids)
    assert "pattern: eval(...)" in text
    assert not re.search(r"\bp/|r/|registry|https?://", text)
