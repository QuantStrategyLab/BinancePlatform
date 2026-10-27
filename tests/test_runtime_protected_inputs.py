"""Exercise the workflow's privacy boundary with synthetic child output."""
import copy
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


def input_target():
    from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
    target = dict(platform_id="binance", strategy_profile="crypto_live_pool_rotation",
                  dry_run_only=False, execution_mode="live", deployment_selector="fixture",
                  account_selector=["fixture-account"], account_scope="fixture-scope",
                  service_name="fixture-service")
    target["live_continuity"] = dict(
        state="RECONCILE_ONLY", baseline_kind="legacy_authorized", baseline_id="synthetic-no-submit",
        baseline_target_sha256=runtime_target_fingerprint(target), captured_at="2026-10-22")
    return target


def input_env(target=None):
    return {"RUNTIME_TARGET_JSON": json.dumps(target or input_target()),
            "RUNTIME_TARGET_ENABLED": "false", "BINANCE_DRY_RUN": "false",
            "STRATEGY_PROFILE": "crypto_live_pool_rotation"}


def run_projection(capsys, output, exit_code=1, *, env=None, source_builder=None,
                   child_effect=None, invoked=True):
    process = SimpleNamespace(stdout=output, stderr="PRIVATE_BROKER_FAILURE", returncode=exit_code)
    fixture_env = input_env() if env is None else env
    original_env = dict(fixture_env)
    source = SimpleNamespace(dry_run=False, standard_execution_permitted=False,
                             risk_authority=object(), runtime_target=SimpleNamespace(
                                 live_continuity=SimpleNamespace(state="ACTIVE_LKG")))
    builder = source_builder or (lambda: source)
    child_errors = []
    def effect(*args, **kwargs):
        try:
            return child_effect(*args, **kwargs) if child_effect else process
        except AssertionError as error:
            child_errors.append(error)
            raise
    with patch.dict(os.environ, fixture_env, clear=True), patch(
        "runtime_config_support.build_live_runtime", new=builder
    ), patch("subprocess.run", side_effect=effect) as child:
        with pytest.raises(SystemExit) as stopped:
            exec(projection_code(), {})
        assert dict(os.environ) == original_env
    if child_errors:
        raise child_errors[0]
    if invoked:
        args, kwargs = child.call_args
        assert args[0][1:] == ["scripts/validate_runtime_startup.py", "--full-cycle"]
        assert {k:v for k,v in kwargs.items() if k != "env"} == {
            "capture_output": True, "text": True, "check": False}
        child_env = kwargs["env"]
        target = json.loads(child_env["RUNTIME_TARGET_JSON"])
        assert target["dry_run_only"] is True and target["execution_mode"] == "paper"
        assert target["live_continuity"]["state"] == "RECONCILE_ONLY"
        assert child_env["BINANCE_DRY_RUN"] == "true" and child_env["RUNTIME_TARGET_ENABLED"] == "false"
    else:
        child.assert_not_called()
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
    assert result["error_type"] == "unknown"
    assert "provider_response" not in result


@pytest.mark.parametrize("error_type", [
    "state_persistence_error", "order_reconciliation_error", "execution_integrity_error",
    "client_call_error", "timeout_error", "connection_error", "permission_error",
    "io_error", "value_error", "type_error", "key_error", "runtime_error",
    "unclassified_error", "ValueError", "KeyError", "TypeError", "OSError", "RuntimeError",
])
def test_failed_child_retains_only_known_error_type(capsys, error_type):
    code, result = run_projection(capsys, "PRIVATE_PROVIDER_MESSAGE\n" + json.dumps({
        "status": "failed", "reason_code": "full_cycle_cycle_error",
        "failure_stage": "daily_state", "error_type": error_type,
        "message": "PRIVATE_ACCOUNT_AND_BALANCE",
    }))
    assert code == 1 and result["status"] == "failed"
    assert result["reason_code"] == "full_cycle_cycle_error"
    assert result["failure_stage"] == "daily_state"
    assert result["error_type"] == error_type
    assert "message" not in result


@pytest.mark.parametrize("error_type", [
    "PRIVATE_ACCOUNT", "custom_error", "RuntimeError: PRIVATE_BALANCE", "",
    None, False, 1, ["PRIVATE_ACCOUNT"], {"account": "PRIVATE_ACCOUNT"},
])
def test_unknown_child_error_type_is_fixed_unknown(capsys, error_type):
    code, result = run_projection(capsys, json.dumps({
        "status": "failed", "reason_code": "full_cycle_cycle_error",
        "failure_stage": "daily_state", "error_type": error_type,
    }))
    assert code == 1 and result["status"] == "failed"
    assert result["error_type"] == "unknown"


def passed():
    return dict(status="passed", validation_scope="full_cycle_no_submit", cycle_complete=True,
                execution_permitted=False, risk_outcome="APPROVE", broker_read_count=2,
                suppressed_side_effect_count=3, suppressed_strategy_record_count=0)


def test_success_requires_complete_read_only_contract(capsys):
    code, result = run_projection(capsys, "PRIVATE_DETAILS\n" + json.dumps(
        passed() | {"error_type": "PRIVATE_ACCOUNT"}), 0)
    assert code == 0 and result["status"] == "passed"
    assert result["executed_call_count"] == 0 and result["execution_permitted"] is False
    assert "error_type" not in result


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


@pytest.mark.parametrize('change', ['profile', 'dry_flag', 'enabled', 'fingerprint', 'json', 'continuity'])
def test_invalid_original_input_never_reaches_source_or_child(capsys, change):
    target = input_target()
    env = input_env(target)
    if change == 'profile':
        env['STRATEGY_PROFILE'] = 'other-profile'
    elif change == 'dry_flag':
        env['BINANCE_DRY_RUN'] = 'true'
    elif change == 'enabled':
        env['RUNTIME_TARGET_ENABLED'] = 'true'
    elif change == 'fingerprint':
        target['live_continuity']['baseline_target_sha256'] = '0' * 64
        env['RUNTIME_TARGET_JSON'] = json.dumps(target)
    elif change == 'json':
        env['RUNTIME_TARGET_JSON'] = 'PRIVATE_INVALID_INPUT'
    else:
        target['live_continuity']['state'] = 'ACTIVE_LKG'
        env['RUNTIME_TARGET_JSON'] = json.dumps(target)
    def forbidden():
        pytest.fail('invalid input must not load the source or contact a broker')
    code, result = run_projection(capsys, '', env=env, source_builder=forbidden, invoked=False)
    assert code == 1 and result['reason_code'] == 'validation_input_invalid'
    assert 'executed_call_count' not in result and 'validator_exit_code' not in result


@pytest.fixture
def real_source(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    import live_risk_authority as authority
    import live_services
    import runtime_config_support
    import strategy_runtime
    from tests.test_live_risk_authority import _payload, _write_authority
    from tests.test_reconciliation_recovery import active_control

    args, control = active_control()
    allowed = {'platform_id', 'strategy_profile', 'dry_run_only', 'deployment_selector',
               'account_selector', 'account_scope', 'service_name', 'live_continuity'}
    original = {k:v for k,v in args['runtime_target'].to_dict().items() if k in allowed}
    env = input_env(original) | {
        'BINANCE_RECOVERY_CONTROL_ENABLED': 'true',
        'BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON': json.dumps(args['expected']),
    }
    # These are synthetic source revisions; loader validation remains real.
    monkeypatch.setattr(authority, 'resolve_strategy_revision', lambda: 'd' * 40)
    monkeypatch.setattr(authority, 'resolve_runner_revision', lambda: 'e' * 40)
    with patch.dict(os.environ, env, clear=True):
        strategy = strategy_runtime.load_research_only_strategy_runtime('crypto_live_pool_rotation')
        config = dict(strategy.merged_runtime_config) | strategy.runtime_overrides
    payload = _payload(datetime.now(timezone.utc))
    payload['runtime_target'] = {k:original[k] for k in (
        'platform_id', 'strategy_profile', 'account_scope', 'account_selector', 'deployment_selector')}
    payload['config_sha256'] = authority.config_sha256(config)
    path, digest = _write_authority(tmp_path, payload)
    env |= {'BINANCE_RISK_AUTHORITY_FILE': str(path),
            'BINANCE_RISK_AUTHORITY_SHA256': digest,
            'BINANCE_RISK_AUTHORITY_SOURCE_REVISION': 'a' * 40}
    observed = {'reads': 0, 'writes': 0, 'broker_submits': 0}
    def forbidden_broker(*_args, **_kwargs):
        observed['broker_submits'] += 1
        pytest.fail('source validation must never connect or submit to a broker')
    monkeypatch.setattr('quant_platform_kit.binance.connect_client', forbidden_broker)
    monkeypatch.setattr('runtime_support.runtime_call_client', forbidden_broker)
    # The full replay suite may supply a Client stub without this method.
    # Add the same deny tripwire to that stub as well as to the real SDK class.
    monkeypatch.setattr('binance.client.Client.create_order', forbidden_broker, raising=False)
    state = {'control': control}
    class ReadOnlyControl:
        def collection(self, name):
            assert name == 'strategy'
            return self
        def document(self, name):
            assert name == 'MULTI_ASSET_STATE__recovery'
            return self
        def get(self, *, retry):
            assert retry is None
            observed['reads'] += 1
            return SimpleNamespace(exists=state['control'] is not None,
                                   to_dict=lambda: copy.deepcopy(state['control']))
        def set(self, *_a, **_kw):
            observed['writes'] += 1
            pytest.fail('source validation must never write')
        update = delete = transaction = batch = set
    monkeypatch.setattr(live_services, 'get_firestore_client', lambda: ReadOnlyControl())
    return SimpleNamespace(env=env, original=original, authority_path=path, payload=payload,
                           state=state, observed=observed, builder=runtime_config_support.build_live_runtime)


def test_real_source_control_and_authority_precede_process_copy(real_source, capsys):
    source = real_source
    raw_target = source.env['RUNTIME_TARGET_JSON']
    authority_bytes = source.authority_path.read_bytes()
    def child(*_args, **kwargs):
        assert source.observed['reads'] >= 1
        child_env = kwargs['env']
        candidate = json.loads(child_env['RUNTIME_TARGET_JSON'])
        expected = json.loads(source.env['RUNTIME_TARGET_JSON'])
        expected['dry_run_only'] = True
        expected['execution_mode'] = 'paper'
        expected['live_continuity']['baseline_target_sha256'] = candidate['live_continuity']['baseline_target_sha256']
        assert candidate == expected
        for key in ('BINANCE_RISK_AUTHORITY_FILE', 'BINANCE_RISK_AUTHORITY_SHA256',
                    'BINANCE_RISK_AUTHORITY_SOURCE_REVISION',
                    'BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON', 'BINANCE_RECOVERY_CONTROL_ENABLED'):
            assert child_env[key] == source.env[key]
        from datetime import datetime, timezone
        from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env
        from live_risk_authority import load_live_risk_authority
        loaded = load_live_risk_authority(
            env=child_env, runtime_target=resolve_runtime_target_from_env(
                env=child_env, expected_platform_id="binance"),
            strategy_revision="d" * 40, runner_revision="e" * 40,
            config_sha256=source.payload["config_sha256"], now_utc=datetime.now(timezone.utc),
        )
        assert loaded.authority_receipt_sha256 == child_env["BINANCE_RISK_AUTHORITY_SHA256"]
        return SimpleNamespace(stdout=json.dumps(passed()), stderr='PRIVATE_CHILD_LOG', returncode=0)
    code, result = run_projection(capsys, '', env=source.env, source_builder=source.builder,
                                  child_effect=child)
    assert code == 0 and result['executed_call_count'] == 0
    assert source.env['RUNTIME_TARGET_JSON'] == raw_target
    assert source.authority_path.read_bytes() == authority_bytes
    assert source.observed['writes'] == source.observed['broker_submits'] == 0


@pytest.mark.parametrize('change', [
    'missing_control', 'confirmation', 'source', 'expected_scope', 'authority_digest',
    'authority_account', 'runner', 'strategy', 'config', 'budget',
])
def test_real_source_or_authority_failure_cannot_be_hidden_by_paper_copy(real_source, capsys, change):
    from tests.test_live_risk_authority import _write_authority
    source = real_source
    if change == 'missing_control':
        source.state['control'] = None
    elif change == 'confirmation':
        source.state['control']['confirmation']['candidate_sha256'] = 'f' * 64
    elif change == 'source':
        source.state['control']['source']['runtime_target_sha256'] = 'f' * 64
    elif change == 'expected_scope':
        expected = json.loads(source.env['BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON'])
        expected['account_scope_sha256'] = 'f' * 64
        source.env['BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON'] = json.dumps(expected)
    elif change == 'authority_digest':
        source.env['BINANCE_RISK_AUTHORITY_SHA256'] = '0' * 64
    else:
        payload = source.payload
        if change == 'authority_account':
            payload['runtime_target']['account_scope'] = 'other-synthetic-scope'
        elif change in {'runner', 'strategy'}:
            payload[change + '_revision'] = 'b' * 40
        elif change == 'config':
            payload['config_sha256'] = '0' * 64
        else:
            payload['mandate']['loss_budget'] = -1
        _, digest = _write_authority(source.authority_path.parent, payload)
        source.env['BINANCE_RISK_AUTHORITY_SHA256'] = digest
    before_target = source.env['RUNTIME_TARGET_JSON']
    before_authority = source.authority_path.read_bytes()
    code, result = run_projection(capsys, '', env=source.env, source_builder=source.builder, invoked=False)
    assert code == 1 and result['reason_code'] == 'source_binding_invalid'
    assert 'validator_exit_code' not in result and 'executed_call_count' not in result
    assert source.env['RUNTIME_TARGET_JSON'] == before_target
    assert source.authority_path.read_bytes() == before_authority
    assert source.observed['writes'] == source.observed['broker_submits'] == 0
