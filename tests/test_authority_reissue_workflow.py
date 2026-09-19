from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "authority-version-reissue.yml"


def test_authority_reissue_uses_an_isolated_hosted_runner() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-24.04" in workflow
    assert "runs-on: self-hosted" not in workflow
    assert "GH_TOKEN: ${{ secrets.BINANCE_AUTHORITY_UPDATE_TOKEN }}" in workflow
    assert (
        "BINANCE_AUTHORITY_UPDATE_TOKEN: ${{ inputs.mode == 'apply' "
        "&& secrets.BINANCE_AUTHORITY_UPDATE_TOKEN || '' }}"
    ) in workflow
