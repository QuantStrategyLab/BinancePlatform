from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "main.yml"


def test_runtime_workflow_is_dispatch_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "workflow_run:" not in workflow
    assert "github.event.workflow_run" not in workflow
