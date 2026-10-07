from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "authority-version-reissue.yml"


def _step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("      - name:", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def test_authority_reissue_uses_an_isolated_hosted_runner() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-24.04" in workflow
    assert "runs-on: self-hosted" not in workflow
    assert "timeout-minutes: 15" in workflow
    assert "persist-credentials: false" in workflow


def test_app_token_is_scoped_and_wired_before_authority_materialization() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mint = _step(workflow, "Mint scoped authority maintenance token")
    reissue = _step(workflow, "Plan or apply authority version reissue")

    assert workflow.index(mint) < workflow.index(
        _step(workflow, "Materialize authority secret to a 0600 temp file")
    )
    assert "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1" in mint
    assert "client-id: ${{ vars.BINANCE_AUTHORITY_APP_CLIENT_ID }}" in mint
    assert "private-key: ${{ secrets.BINANCE_AUTHORITY_APP_PRIVATE_KEY }}" in mint
    assert "owner: ${{ github.repository_owner }}" in mint
    assert "repositories: ${{ github.event.repository.name }}" in mint
    assert "permission-actions: read" in mint
    assert "INPUT_PERMISSION-ACTIONS-VARIABLES: read" in mint
    assert "permission-variables: read" not in mint
    assert "permission-environments: ${{ inputs.mode == 'apply' && 'write' || 'read' }}" in mint
    assert "permission-metadata: read" in mint
    assert "permission-contents:" not in mint
    assert "permission-administration:" not in mint
    assert "permission-secrets:" not in mint
    assert "skip-token-revoke:" not in mint

    assert "GH_TOKEN: ${{ steps.app-token.outputs.token }}" in reissue
    assert (
        "BINANCE_AUTHORITY_UPDATE_TOKEN: ${{ inputs.mode == 'apply' "
        "&& steps.app-token.outputs.token || '' }}"
    ) in reissue
    assert "secrets.BINANCE_AUTHORITY_UPDATE_TOKEN" not in workflow


def test_manual_inputs_and_existing_stop_contract_remain() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    docs = (WORKFLOW.parents[2] / "docs" / "authority_reissue.md").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "options: [plan, apply]" in workflow
    assert "schedule:" not in workflow
    assert "does not enable trading" in workflow.lower()
    assert "does not dispatch runtime / no-submit full_cycle" in workflow.lower()
    for name in (
        "target_strategy_revision",
        "target_runner_revision",
        "target_source_revision",
        "expected_authority_sha256",
        "expected_source_revision",
    ):
        assert f"      {name}:" in workflow
        assert f"      {name}:\n        description:" in workflow
    assert "RUNTIME_TARGET_ENABLED` live value is exactly `false`" in docs
    assert "first failure or uncertain outcome: stop" in docs
