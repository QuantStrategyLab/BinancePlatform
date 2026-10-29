from pathlib import Path


def test_natural_daily_is_read_only_and_separate_from_runtime() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    lifecycle = (workflows / "runtime-target-lifecycle.yml").read_text()
    protected = (workflows / "runtime-protected.yml").read_text()
    call = lifecycle.split("  runtime-daily:\n", 1)[1].split("  observe-scheduler:\n", 1)[0]
    daily = protected.split("  runtime-daily:\n", 1)[1].split("  deploy:\n", 1)[0]
    deploy = protected.split("  deploy:\n", 1)[1]

    assert 'cron: "47 * * * *"' in lifecycle
    assert "github.event_name == 'schedule'" in call
    assert "github.run_attempt == 1" in call
    assert "runtime_daily_only: true" in call
    assert "PRIVATE_RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON }}" in call
    assert "RUNTIME_TARGET_JSON: ${{ secrets.PRIVATE_RUNTIME_TARGET_JSON }}" in daily
    assert "vars.RUNTIME_TARGET_JSON" not in daily
    assert "if: ${{ !inputs.runtime_daily_only }}" in deploy
    assert "github.event_name == 'schedule'" in daily
    assert "github.run_attempt == 1" in daily
    assert "scripts.publish_runtime_daily" in daily
    assert "TZ=America/New_York date +%F" in daily
    assert "RUNTIME_TARGET_ENABLED" not in daily
    assert "scripts.runtime_target_lifecycle_status" not in daily
    assert "BINANCE_API_KEY" not in daily


def test_manual_daily_uses_same_protected_job_and_checks_skip_cloud_auth() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    manual = (workflows / "publish-runtime-daily-once.yml").read_text()
    protected = (workflows / "runtime-protected.yml").read_text()
    daily = protected.split("  runtime-daily:\n", 1)[1].split("  deploy:\n", 1)[0]
    auth = daily.split("      - name: Authenticate existing report reader\n", 1)[1].split("      - uses: google-github-actions/setup-gcloud", 1)[0]
    assert "uses: ./.github/workflows/runtime-protected.yml" in manual
    assert "PRIVATE_RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON }}" in manual
    assert "\n      RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON }}" not in manual
    assert "runtime_daily_only: true" in manual
    assert "scripts.publish_runtime_daily" not in manual
    assert "inputs.runtime_daily_compare_nonce == '' && !inputs.runtime_daily_preflight_only" in auth
    assert "inputs.runtime_daily_compare_nonce != ''" in daily
    assert "inputs.runtime_daily_preflight_only" in daily
    assert "github.event_name == 'workflow_dispatch'" in daily
    assert "github.event_name == 'schedule'" in daily
