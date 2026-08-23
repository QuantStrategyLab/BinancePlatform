from pathlib import Path
import re


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "main.yml"
WATCHDOG_WORKFLOW = WORKFLOW.with_name("watchdog.yml")
HEARTBEAT_WORKFLOW = WORKFLOW.with_name("runtime-heartbeat.yml")
FULL_SHA_ACTION = re.compile(r"(?:-\s+)?uses:\s+[^\s@]+@[0-9a-f]{40}(?:\s+#\s+v\d+)?$")


def _job_block(workflow: str, job: str, next_job: str | None = None) -> str:
    start = workflow.index(f"  {job}:\n")
    end = workflow.index(f"  {next_job}:\n", start) if next_job else len(workflow)
    return workflow[start:end]


def test_runtime_remote_actions_are_pinned_to_full_commit_shas() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

    assert action_lines
    assert all(FULL_SHA_ACTION.fullmatch(line) for line in action_lines)


def test_broker_job_cannot_write_repository_contents() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    broker_job = _job_block(workflow, "deploy", "publish-execution-log")
    log_job = _job_block(workflow, "publish-execution-log")

    assert "BINANCE_API_KEY: ${{ secrets.BINANCE_API_KEY }}" in broker_job
    assert "contents: write" not in broker_job
    assert "contents: read" in broker_job
    assert "BINANCE_API_KEY" not in log_job
    assert "BINANCE_API_SECRET" not in log_job
    assert "contents: write" in log_job
    assert "actions/download-artifact@" in log_job


def test_oidc_and_notification_workflows_pin_remote_actions() -> None:
    for path in (WATCHDOG_WORKFLOW, HEARTBEAT_WORKFLOW):
        workflow = path.read_text(encoding="utf-8")
        action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

        assert action_lines
        assert all(FULL_SHA_ACTION.fullmatch(line) for line in action_lines)


def test_heartbeat_secrets_are_only_available_to_check_step() -> None:
    workflow = HEARTBEAT_WORKFLOW.read_text(encoding="utf-8")
    job_env = workflow[workflow.index("    env:\n") : workflow.index("    steps:\n")]
    check_step = workflow[workflow.index("      - name: Check recent Runtime workflow success") :]

    assert "secrets." not in job_env
    assert "TG_TOKEN: ${{ secrets.TG_TOKEN }}" in check_step
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in check_step
