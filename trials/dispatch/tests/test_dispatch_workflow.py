from pathlib import Path

from trials.dispatch.replay import WORKFLOW_FILE, run_name
from trials.dispatch.specs import JOB_ID_RE, MAX_SPECS, SAFE_RE

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / WORKFLOW_FILE


def test_workflow_run_name_matches_the_driver():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "run-name: " + run_name("${{ inputs.trial_id }}", "${{ inputs.gate_id }}") in text


def test_workflow_validation_uses_the_same_patterns_as_the_driver():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert JOB_ID_RE.pattern in text and SAFE_RE.pattern in text and f"> {MAX_SPECS}" in text


def test_workflow_has_no_concurrency_group_and_no_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "concurrency:" not in text and "secrets." not in text and "continue-on-error" not in text
    assert "permissions:\n  contents: read" in text
