"""Exercise the workflow's privacy boundary with synthetic child output."""
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / ".github/workflows/main.yml"
PROTECTED = ENTRY.with_name("runtime-protected.yml")
PRIVATE = (
    "RUNTIME_TARGET_JSON", "DIGEST_CANDIDATES_GCS_URI", "EXECUTION_REPORT_GCS_URI",
    "DIGEST_STORE_TARGET", "GLOBAL_TELEGRAM_CHAT_ID",
)


def test_private_values_only_enter_through_reusable_secrets():
    entry, protected = ENTRY.read_text(), PROTECTED.read_text()
    before, secret_mapping = entry.split("    secrets:\n", 1)
    assert "uses: ./.github/workflows/runtime-protected.yml" in before
    assert "runs-on:" not in entry and "steps:" not in entry
    assert "workflow_call:" in protected and "workflow_dispatch:" not in protected
    assert "environment: binance-runtime" in protected
    for name in PRIVATE:
        assert f"PRIVATE_{name}: ${{{{ vars.{name}" in secret_mapping
        assert f"vars.{name}" not in protected
        assert f"{name}: ${{{{ secrets.PRIVATE_{name}" in protected
    assert "BINANCE_AUTHORITY_APP_PRIVATE_KEY" not in entry + protected
    assert "::add-mask::" not in entry + protected
    # Every dispatch input is forwarded and declared in the protected call.
    dispatch = entry.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
    names = re.findall(r"^      ([a-z_]+):$", dispatch, re.M)
    for name in names:
        assert f"      {name}: ${{{{ inputs.{name}" in before
        assert f"      {name}:\n" in protected


def projection_code():
    source = PROTECTED.read_text().split("<<'SAFE_VALIDATION'\n", 1)[1]
    return compile(textwrap.dedent(source.split("          SAFE_VALIDATION", 1)[0]),
                   str(PROTECTED), "exec")


def run_projection(capsys, output, exit_code=1):
    process = SimpleNamespace(stdout=output, stderr="PRIVATE_BROKER_FAILURE", returncode=exit_code)
    with patch("subprocess.run", return_value=process) as child:
        with pytest.raises(SystemExit) as stopped:
            exec(projection_code(), {})
    args, kwargs = child.call_args
    assert args[0][1:] == ["scripts/validate_runtime_startup.py", "--full-cycle"]
    assert kwargs == {"capture_output": True, "text": True, "check": False}
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert captured.err == ""
    return stopped.value.code, json.loads(captured.out)


def test_child_logs_and_unknown_failure_metadata_never_escape(capsys):
    code, result = run_projection(capsys, "PRIVATE_BALANCE\n" + json.dumps({
        "status": "failed", "reason_code": "PRIVATE_ACCOUNT",
        "failure_stage": "PRIVATE_SELECTOR", "balance": "PRIVATE_BALANCE",
    }))
    assert code == 1
    assert result["reason_code"] == "validator_failed"
    assert "failure_stage" not in result and "balance" not in result


def test_known_failure_retains_fixed_diagnostic_without_provider_output(capsys):
    code, result = run_projection(capsys, "PRIVATE_DETAILS\n" + json.dumps({
        "status": "failed", "reason_code": "full_cycle_broker_connect_failed",
        "failure_stage": "connect", "provider_response": "PRIVATE_ACCOUNT",
    }))
    assert code == 1
    assert result["reason_code"] == "full_cycle_broker_connect_failed"
    assert result["failure_stage"] == "connect"
    assert "provider_response" not in result


def passed():
    return dict(status="passed", validation_scope="full_cycle_no_submit", cycle_complete=True,
                execution_permitted=False, risk_outcome="APPROVE", broker_read_count=2,
                suppressed_side_effect_count=3, suppressed_strategy_record_count=0)


def test_success_requires_complete_read_only_contract(capsys):
    code, result = run_projection(capsys, "PRIVATE_DETAILS\n" + json.dumps(passed()), 0)
    assert code == 0 and result["status"] == "passed"
    assert result["executed_call_count"] == 0 and result["execution_permitted"] is False


@pytest.mark.parametrize("override", [
    {"cycle_complete": False}, {"execution_permitted": True}, {"risk_outcome": "REJECT"},
    {"validation_scope": "live"}, {"broker_read_count": True},
    {"suppressed_side_effect_count": "PRIVATE_VALUE"},
    {"suppressed_strategy_record_count": -1},
])
def test_success_label_cannot_bypass_contract(capsys, override):
    candidate = passed() | override
    code, result = run_projection(capsys, json.dumps(candidate), 0)
    assert code == 1 and result["status"] == "failed"


def test_accounting_failure_keeps_existing_fixed_reason(capsys):
    code, result = run_projection(capsys, json.dumps({
        "status": "failed", "reason_code": "earn_cash_cursor_mismatch",
        "failure_stage": "daily_state",
    }))
    assert code == 1
    assert result["reason_code"] == "earn_cash_cursor_mismatch"
    assert result["failure_stage"] == "daily_state"


@pytest.mark.parametrize("changes,allowed", [
    ({}, True),
    ({"RUNTIME_TARGET_ENABLED": "true"}, False),
    ({"VALIDATE_ONLY_INPUT": "false"}, False),
    ({"FULL_CYCLE_INPUT": "false"}, False),
    ({"RECONCILE_ONLY_INPUT": "true"}, False),
    ({"RECONCILE_PERSIST_INPUT": "true"}, False),
    ({"DIAGNOSE_BALANCES_INPUT": "true"}, False),
    ({"RECOVERY_ACTION_INPUT": "activate"}, False),
    ({"ACCOUNTING_MIGRATION_ACTION_INPUT": "preview"}, False),
])
def test_trusted_production_ref_accepts_only_disabled_no_submit(changes, allowed):
    # Execute the real mode guard. Its untouched lowercase/output suffix needs
    # Ubuntu Bash; the mode checks themselves also run on local macOS Bash 3.
    text = PROTECTED.read_text().split("      - name: 0. Validate runtime target control", 1)[1]
    block = text.split("        run: |\n", 1)[1].split(
        '          case "${RUNTIME_TARGET_ENABLED,,}"', 1)[0]
    env = {
        "PATH": os.environ.get("PATH", ""), "GITHUB_REF": "refs/heads/runtime-production",
        "RUNTIME_TARGET_ENABLED": "false", "VALIDATE_ONLY_INPUT": "true",
        "FULL_CYCLE_INPUT": "true", "RECONCILE_ONLY_INPUT": "false",
        "RECONCILE_PERSIST_INPUT": "false", "DIAGNOSE_BALANCES_INPUT": "false",
        "RECOVERY_ACTION_INPUT": "none", "ACCOUNTING_MIGRATION_ACTION_INPUT": "none",
        "CANDIDATE_RELEASE_SHA_INPUT": "afa916ae2e4e10060b96f9f6b393e59f66127fcb",
    } | changes
    result = subprocess.run(["bash", "-c", textwrap.dedent(block)],
                                        env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is allowed
