from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_SOURCE = REPO_ROOT / ".githooks" / "pre-push"
TOKEN = "acme-internal-host"
ZERO_SHA = "0" * 40
_STRIPPED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "LEAK_GUARD_DENYLIST", "LEAK_GUARD_DENYLIST_FILE")


def j(*parts: str) -> str:
    return "".join(parts)


def clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(extra)
    return env


def git(repo: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", env=env or clean_env(),
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stdout}{proc.stderr}")
    return proc


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Test Author"), ("user.email", "author@example.com"),
        ("commit.gpgsign", "false"), ("core.autocrlf", "false"),
    ):
        git(path, "config", key, value)
    return path


def write(repo: Path, relative: str, content: str | bytes) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def commit(repo: Path, files: dict, message: str = "test: fixture commit", author: str | None = None) -> str:
    for relative, content in files.items():
        if content is None:
            git(repo, "rm", "-q", "-f", "--", relative)
        else:
            write(repo, relative, content)
            git(repo, "add", "--", relative)
    args = ["commit", "-q", "--allow-empty", "-m", message]
    if author:
        args += ["--author", author]
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def run_cli(repo: Path, *args: str, denylist: str | None = None, env: dict | None = None, input_text: str | None = None):
    full_env = env if env is not None else clean_env()
    if denylist is not None:
        full_env = {**full_env, "LEAK_GUARD_DENYLIST": b64(denylist)}
    return subprocess.run(
        [sys.executable, "-m", "tools.leak_guard", *args], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", env=full_env, input=input_text,
    )


def combined(proc) -> str:
    return proc.stdout + proc.stderr


def install_guard(repo: Path) -> None:
    package = repo / "tools" / "leak_guard"
    shutil.copytree(
        REPO_ROOT / "tools" / "leak_guard", package,
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    shutil.copy2(REPO_ROOT / "tools" / "__init__.py", repo / "tools" / "__init__.py")
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(HOOK_SOURCE, hooks / "pre-push")
    git(repo, "add", "-A")
    git(repo, "update-index", "--chmod=+x", ".githooks/pre-push")
    git(repo, "config", "core.hooksPath", ".githooks")
