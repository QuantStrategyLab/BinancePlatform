from pathlib import Path
import re


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "main.yml"
WATCHDOG_WORKFLOW = WORKFLOW.with_name("watchdog.yml")
HEARTBEAT_WORKFLOW = WORKFLOW.with_name("runtime-heartbeat.yml")
LIFECYCLE_WORKFLOW = WORKFLOW.with_name("runtime-target-lifecycle.yml")
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


def test_reconciliation_run_does_not_try_to_publish_a_regular_execution_report() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    log_job = _job_block(workflow, "publish-execution-log")

    assert "github.event.inputs.reconcile_only != 'true'" in log_job


def test_reconciliation_artifact_retention_matches_repository_policy() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    reconciliation_step = workflow[workflow.index("      - name: 5b. Retain redacted reconciliation candidate") :]

    assert "retention-days: 7" in reconciliation_step


def test_reconciliation_failure_still_uploads_the_redacted_candidate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    reconciliation_step = workflow[workflow.index("      - name: 5b. Retain redacted reconciliation candidate") :]

    assert "always()" in reconciliation_step.splitlines()[1]
    assert "github.event.inputs.reconcile_only == 'true'" in reconciliation_step.splitlines()[1]
    assert "continue-on-error: true" not in reconciliation_step.split("      - name: 6.", 1)[0]


def test_reconciliation_only_can_collect_evidence_while_normal_runtime_is_disabled() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    broker_job = _job_block(workflow, "deploy", "publish-execution-log")

    reconciliation_gate = "env.RUNTIME_TARGET_ENABLED == 'true' || github.event.inputs.reconcile_only == 'true'"
    assert broker_job.count(reconciliation_gate) >= 4
    assert 'if [ "${RECONCILE_ONLY:-false}" = "true" ]; then' in broker_job
    assert '"$VENV_PATH/bin/python" main.py' in broker_job


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


def test_heartbeat_installs_locked_dependencies_before_importing_qpk() -> None:
    workflow = HEARTBEAT_WORKFLOW.read_text(encoding="utf-8")

    checkout = workflow.index("      - name: Checkout repository")
    setup_uv = workflow.index("      - name: Set up uv")
    install = workflow.index("      - name: Install locked dependencies")
    check = workflow.index("      - name: Check recent Runtime workflow success")

    assert checkout < setup_uv < install < check
    setup_uv_step = workflow[setup_uv:install]
    install_step = workflow[install:check]
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9" in setup_uv_step
    assert "uv sync --frozen --no-dev" in install_step
    assert "python -m pip install" not in workflow
    assert "uv run --no-sync python scripts/runtime_workflow_heartbeat.py" in workflow[check:]
    assert "run: python scripts/runtime_workflow_heartbeat.py" not in workflow[check:]


def test_lifecycle_workflow_is_read_only_and_uses_pinned_actions() -> None:
    workflow = LIFECYCLE_WORKFLOW.read_text(encoding="utf-8")
    action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

    assert action_lines
    assert all(FULL_SHA_ACTION.fullmatch(line) for line in action_lines)
    assert "BINANCE_API_KEY" not in workflow
    assert "BINANCE_API_SECRET" not in workflow
    assert "contents: write" not in workflow
    assert "id-token: write" in workflow
    assert "EXECUTION_EVIDENCE_SYNC_TOKEN: ${{ secrets.EXECUTION_EVIDENCE_SYNC_TOKEN }}" in workflow
    assert "source-id: binance.runtime-target-lifecycle" in workflow


def test_reconciliation_defaults_to_zero_persistence_and_no_notification() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    broker_job = _job_block(workflow, "deploy", "publish-execution-log")

    assert "reconcile_persist_candidate:" in workflow
    assert 'default: false' in workflow[workflow.index("reconcile_persist_candidate:") : workflow.index("permissions:")]
    assert 'args=(--no-persist)' in broker_job
    assert "github.event.inputs.reconcile_persist_candidate == 'true'" in broker_job
    assert "github.event.inputs.reconcile_only != 'true'" in broker_job


def test_disabled_host_observation_uses_actual_control_read_and_existing_source() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    broker_job = _job_block(workflow, "deploy", "publish-execution-log")
    control = broker_job.split("id: runtime-target-control", 1)[1].split("      - name:", 1)[0]
    assert 'echo "observed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$GITHUB_OUTPUT"' in control
    assert 'if [ "${RUNTIME_TARGET_ENABLED,,}" = "false" ]; then' in control
    disabled = broker_job.split("      - name: Prepare disabled host observation", 1)[1].split("      - name:", 1)[0]
    assert "steps.runtime-target-control.outputs.enabled == 'false'" in disabled
    assert "RUNTIME_TARGET_ENABLED: ${{ steps.runtime-target-control.outputs.enabled }}" in disabled
    assert "RUNTIME_TARGET_CONTROL_OBSERVED_AT: ${{ steps.runtime-target-control.outputs.observed_at }}" in disabled
    assert "python3 scripts/runtime_target_lifecycle_status.py" in disabled
    assert "BINANCE_API_KEY" not in disabled
    assert "google" not in disabled
    publish = broker_job.split("      - name: Publish disabled host observation", 1)[1].split("      - name:", 1)[0]
    assert "steps.disabled-host.outputs.publish_ready == 'true'" in publish
    assert "source-id: binance.runtime-target-lifecycle" in publish
    assert "deployment-json: ${{ steps.disabled-host.outputs.deployment_json }}" in publish
    assert "observe-gcp:" not in publish
    assert "EXECUTION_EVIDENCE_SYNC_TOKEN: ${{ secrets.EXECUTION_EVIDENCE_SYNC_TOKEN }}" in publish
    assert "observation not published" in disabled


def test_disabled_host_does_not_require_cloud_identity_or_runtime_dependencies() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    broker_job = _job_block(workflow, "deploy", "publish-execution-log")
    identity = broker_job.split("      - name: 0. Validate deployment identity configuration", 1)[1].split("      - name:", 1)[0]
    assert "if: ${{ env.RUNTIME_TARGET_ENABLED == 'true' || github.event.inputs.reconcile_only == 'true' }}" in identity
    assert broker_job.index("Publish disabled host observation") < broker_job.index("Authenticate to Google Cloud")
    assert broker_job.index("Publish disabled host observation") < broker_job.index("Prepare or update dependency environment")


def test_disabled_host_prepare_requires_state_credentials_without_refreshing_time(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys
    import textwrap

    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.split("      - name: Prepare disabled host observation", 1)[1].split("      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    for available in (False, True):
        output = tmp_path / f"output-{available}"
        env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "GITHUB_OUTPUT": str(output),
            "RUNTIME_TARGET_ENABLED": "false",
            "RUNTIME_TARGET_CONTROL_OBSERVED_AT": "2026-09-06T01:00:00Z",
            "RUNTIME_TARGET_JSON": json.dumps({
                "platform_id": "binance", "strategy_profile": "crypto_live_pool_rotation",
                "execution_mode": "paper", "dry_run_only": True,
            }),
        }
        if available:
            env.update(EXECUTION_EVIDENCE_SYNC_URL="https://example.invalid",
                       EXECUTION_EVIDENCE_SYNC_TOKEN="synthetic-test-value")
        result = subprocess.run(["bash", "-c", script], env=env, cwd=WORKFLOW.parents[2],
                                text=True, capture_output=True, check=True)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert values["publish_ready"] == str(available).lower()
        assert json.loads(values["deployment_json"])["observed_at"] == "2026-09-06T01:00:00Z"
        assert ("observation not published" in result.stdout) is not available
        assert "synthetic-test-value" not in result.stdout + result.stderr
