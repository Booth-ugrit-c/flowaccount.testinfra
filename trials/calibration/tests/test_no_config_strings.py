from __future__ import annotations

from pathlib import Path

from trials.calibration.tests.helpers import config_dict

PACKAGE = Path(__file__).resolve().parents[1]


def config_specific_strings(cfg: dict) -> set[str]:
    owner, name = cfg["repo"].split("/")
    found = {cfg["repo"], owner, name, *cfg["workflows"], *cfg["runners"], *cfg["manual_runners"]}
    found |= {entry["job_pattern"].lstrip("^") for entry in cfg["lanes"]}
    found |= {entry["pattern"] for entry in cfg["service_patterns"]}
    return {s for s in found if len(s) >= 4}


def offending(root: Path, strings: set[str]) -> list[tuple[str, str]]:
    hits = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".py", ".json", ".md"} or "tests" in path.relative_to(root).parts:
            continue
        text = path.read_text(encoding="utf-8")
        hits += [(path.name, s) for s in sorted(strings) if s in text]
    return hits


def test_committed_code_holds_no_config_specific_string():
    strings = config_specific_strings(config_dict())
    assert {"acme/widgets", "lab-runner-a", "lab-runner-m", "pipeline.yml", "document-store"} <= strings
    assert offending(PACKAGE, strings) == []


def test_the_scan_catches_a_planted_string(tmp_path):
    (tmp_path / "module.py").write_text('REPO = "acme/widgets"\n', encoding="utf-8")
    assert offending(tmp_path, config_specific_strings(config_dict())) == [("module.py", "acme"), ("module.py", "acme/widgets"), ("module.py", "widgets")]
    (tmp_path / "module.py").write_text("REPO = None\n", encoding="utf-8")
    assert offending(tmp_path, config_specific_strings(config_dict())) == []
