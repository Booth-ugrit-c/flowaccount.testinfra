from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "build_artifact.sh"
SHELLS = [shutil.which(name) for name in ("bash", "sh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(not SHELLS or shutil.which("tar") is None, reason="a POSIX shell and tar are required")

OUTPUTS = {
    "sim/dotnet/src/Core/bin/Release/net10.0/Core.dll": b"MZ\x00\x01core",
    "sim/dotnet/src/Core/obj/Release/net10.0/Core.dll": b"MZ\x00\x02obj",
    "sim/dotnet/src/Core/obj/project.assets.json": b'{"version": 3}',
    "sim/dotnet/tests/Core.Tests/bin/Release/net10.0/Core.Tests.dll": b"MZ\x00\x03tests",
    "sim/dotnet/bin/shared.txt": b"root bin",
}
SOURCES = {
    "sim/dotnet/src/Core/Core.cs": b"namespace Core;",
    "sim/dotnet/src/Core/Core.csproj": b"<Project />",
}
VALID = "sim/dotnet/src/Core/bin/Release/ok.dll"


class Lab:
    def __init__(self, tmp_path: Path, shell: str):
        self.shell = shell
        self.tmp = tmp_path
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()
        self.archive = tmp_path / "out" / "build.tar"
        self.archive.parent.mkdir()

    def write(self, files: dict[str, bytes], mtime: int = 1_700_000_000) -> None:
        for name, data in files.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            os.utime(path, (mtime, mtime))

    def run(self, *args: str):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SIM_", "GITHUB_"))}
        return subprocess.run(
            [self.shell, SCRIPT.as_posix(), *args],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def craft(self, *members: tarfile.TarInfo) -> str:
        with tarfile.open(self.archive, "w", format=tarfile.GNU_FORMAT) as tar:
            ok = tarfile.TarInfo(VALID)
            ok.size = 2
            tar.addfile(ok, io.BytesIO(b"ok"))
            for member in members:
                tar.addfile(member, io.BytesIO(b"x" * member.size) if member.isreg() else None)
        return self.relative()

    def relative(self) -> str:
        return os.path.relpath(self.archive, self.workspace).replace(os.sep, "/")


def regular(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 1
    return info


def link(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


@pytest.fixture(params=SHELLS)
def lab(tmp_path, request):
    return Lab(tmp_path, request.param)


def test_a_round_trip_keeps_every_bin_and_obj_byte_and_mtime(lab):
    lab.write(OUTPUTS)
    lab.write(SOURCES)
    packed = lab.run("pack", lab.relative())
    assert packed.returncode == 0, packed.stderr
    with tarfile.open(lab.archive) as tar:
        names = {m.name.rstrip("/") for m in tar.getmembers() if m.isfile()}
    assert names == set(OUTPUTS)
    for name in OUTPUTS:
        (lab.workspace / name).unlink()
    for tree in ("sim/dotnet/src/Core/bin", "sim/dotnet/src/Core/obj", "sim/dotnet/tests", "sim/dotnet/bin"):
        shutil.rmtree(lab.workspace / tree)
    unpacked = lab.run("unpack", lab.relative())
    assert unpacked.returncode == 0, unpacked.stderr
    for name, data in OUTPUTS.items():
        path = lab.workspace / name
        assert path.read_bytes() == data
        assert int(path.stat().st_mtime) == 1_700_000_000
    for name, data in SOURCES.items():
        assert (lab.workspace / name).read_bytes() == data


def test_an_empty_tree_packs_and_unpacks_nothing(lab):
    lab.write(SOURCES)
    assert lab.run("pack", lab.relative()).returncode == 0
    result = lab.run("unpack", lab.relative())
    assert result.returncode == 0, result.stderr
    assert "empty archive" in result.stdout


@pytest.mark.parametrize(
    "name, escaped",
    [
        ("../x", "x"),
        ("sim/dotnet/src/Core/bin/../../../../../x", "x"),
        ("sim/dotnet/src/Core/bin/..", None),
    ],
)
def test_a_member_with_dot_dot_is_refused(lab, name, escaped):
    archive = lab.craft(regular(name))
    result = lab.run("unpack", archive)
    assert result.returncode == 1
    assert "refusing a member with .." in result.stderr
    assert not (lab.workspace / VALID).exists()
    if escaped:
        assert not (lab.tmp / escaped).exists()


@pytest.mark.parametrize("name", ["/sim/dotnet/src/Core/bin/abs.dll", "/tmp/abs.dll"])
def test_an_absolute_member_is_refused(lab, name):
    archive = lab.craft(regular(name))
    result = lab.run("unpack", archive)
    assert result.returncode == 1
    assert "refusing an absolute member" in result.stderr
    assert not (lab.workspace / VALID).exists()


@pytest.mark.parametrize(
    "member",
    [
        link("sim/dotnet/src/Core/bin/escape", "../../../../..", tarfile.SYMTYPE),
        link("sim/dotnet/src/Core/bin/hard.dll", "sim/dotnet/src/Core/bin/Release/ok.dll", tarfile.LNKTYPE),
    ],
    ids=["symlink", "hardlink"],
)
def test_a_link_member_is_refused(lab, member):
    archive = lab.craft(member)
    result = lab.run("unpack", archive)
    assert result.returncode == 1
    assert "a link or special file" in result.stderr
    assert not (lab.workspace / VALID).exists()


@pytest.mark.parametrize(
    "name",
    ["README.md", "sim/dotnet/src/Core/Core.cs", ".github/workflows/x.yml", "sim/other/bin/x.dll", "sim/dotnetx/a/bin/x"],
)
def test_a_member_outside_the_bin_and_obj_trees_is_refused(lab, name):
    archive = lab.craft(regular(name))
    result = lab.run("unpack", archive)
    assert result.returncode == 1
    assert "outside" in result.stderr
    assert not (lab.workspace / VALID).exists()


def test_a_symlink_in_the_checkout_refuses_the_unpack(lab, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (lab.workspace / "sim/dotnet/src/Core").mkdir(parents=True)
    try:
        os.symlink(outside, lab.workspace / "sim/dotnet/src/Core/bin", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable here: {exc}")
    listed = subprocess.run(
        [lab.shell, "-c", "find sim/dotnet -type l"], cwd=lab.workspace, capture_output=True, text=True, timeout=30
    )
    if "sim/dotnet/src/Core/bin" not in listed.stdout:
        pytest.skip("this shell does not see the symlink as a link")
    archive = lab.craft()
    result = lab.run("unpack", archive)
    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert list(outside.iterdir()) == []


def test_an_unreadable_archive_is_refused(lab):
    lab.archive.write_bytes(b"not a tar archive at all" * 40)
    result = lab.run("unpack", lab.relative())
    assert result.returncode == 1


def test_a_missing_archive_is_refused(lab):
    result = lab.run("unpack", "absent.tar")
    assert result.returncode == 1


def test_pack_without_the_solution_tree_is_refused(lab):
    result = lab.run("pack", lab.relative())
    assert result.returncode == 1
    assert "no directory sim/dotnet" in result.stderr


def test_an_unknown_mode_is_a_usage_error(lab):
    assert lab.run("extract", "x.tar").returncode == 2
