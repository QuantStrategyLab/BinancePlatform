"""Execute the workflow's real input adapter with synthetic targets and no network."""
import copy
import json
import os
from pathlib import Path
import textwrap
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
from tests.test_dry_run_allowlist import dry_run_target_payload

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/dry-run-validation.yml'


def adapter():
    script = WORKFLOW.read_text().split("          uv run --no-sync python - <<'PYTHON'\n", 1)[1]
    return compile(textwrap.dedent(script.split('          PYTHON\n', 1)[0]), str(WORKFLOW), 'exec')


def run_adapter(monkeypatch, tmp_path, target, process=None, extra=None):
    env = {
        'SOURCE_RUNTIME_TARGET_JSON': json.dumps(target),
        'RUNTIME_TARGET_ENABLED': 'true', 'BINANCE_DRY_RUN': 'false',
        'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
        'RUNNER_TEMP': str(tmp_path),
    }
    env.update(extra or {})
    monkeypatch.setattr(os, 'environ', env)
    monkeypatch.chdir(ROOT)
    with patch('subprocess.run', side_effect=process) as call:
        with pytest.raises(SystemExit) as stopped:
            exec(adapter(), {})
    result = json.loads((tmp_path / 'binance-dry-run-result-123-1.json').read_text())
    return stopped.value.code, result, call


def test_only_explicit_main_dispatch_can_start_isolated_job():
    text = WORKFLOW.read_text()
    assert 'default: false' in text
    assert "if: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run_full_cycle == true && github.ref == 'refs/heads/main' }}" in text
    assert 'schedule:' not in text and 'workflow_call:' not in text and 'push:' not in text
    assert 'group: Runtime-${{ github.ref_name }}' in text
    assert 'cancel-in-progress: false' in text
    assert 'ref: ${{ github.sha }}' in text
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main' in text
    assert 'persist-credentials: false' in text
    assert 'uv sync --frozen' in text
    assert "vars.RUNTIME_TARGET_ENABLED" not in text
    assert "vars.BINANCE_DRY_RUN" not in text
    assert 'candidate_release_sha' not in text
    assert 'cron' not in text.replace('"cron_written": False', '')


def test_production_defaults_and_entry_remain_separate():
    production = (ROOT / '.github/workflows/main.yml').read_text()
    assert "RUNTIME_TARGET_ENABLED: ${{ vars.RUNTIME_TARGET_ENABLED || 'false' }}" in production
    assert "BINANCE_DRY_RUN: ${{ vars.BINANCE_DRY_RUN || 'true' }}" in production
    assert 'RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON }}' in production
    assert 'dry_run_full_cycle' not in production
    assert '"$VENV_PATH/bin/python" main.py' in production
    isolated = WORKFLOW.read_text()
    assert '"scripts/validate_runtime_startup.py", "--full-cycle"' in isolated
    for forbidden in ('binance_recovery_controller.py', 'migrate_daily_accounting_state.py',
                      'binance_recovery_controller.py', 'TG_TOKEN:', 'GITHUB_TOKEN:',
                      'EXECUTION_EVIDENCE_SYNC_TOKEN:', 'EXECUTION_REPORT_GCS_URI:',
                      'DIGEST_STORE_TARGET:', 'actions: write', 'contents: write'):
        assert forbidden not in isolated
    # Broker credentials and the private target only reach the sole validator step.
    before, after = isolated.split('      - name: Run isolated full-cycle validator', 1)
    validator, artifact = after.split('      - name: Retain safe validation result', 1)
    assert 'BINANCE_API_KEY:' not in before + artifact
    assert 'SOURCE_RUNTIME_TARGET_JSON: ${{ vars.RUNTIME_TARGET_JSON }}' in validator
    assert 'SOURCE_RUNTIME_TARGET_JSON' not in artifact
    assert 'process.stdout' in validator and 'process.stderr' not in validator
    assert 'path: ${{ runner.temp }}/binance-dry-run-result-${{ github.run_id }}-${{ github.run_attempt }}.json' in artifact


def test_adapter_derives_process_only_paper_target_and_uses_sole_validator(monkeypatch, tmp_path, capsys):
    original = dry_run_target_payload(dry_run_only=False, execution_mode='live')
    untouched = copy.deepcopy(original)
    continuity = copy.deepcopy(original['live_continuity'])

    def process(args, **kwargs):
        assert args[1:] == ['scripts/validate_runtime_startup.py', '--full-cycle']
        assert kwargs == {'capture_output': True, 'text': True, 'check': False}
        candidate = json.loads(os.environ['RUNTIME_TARGET_JSON'])
        assert candidate['dry_run_only'] is True and candidate['execution_mode'] == 'paper'
        expected = copy.deepcopy(untouched)
        expected.update(dry_run_only=True, execution_mode='paper')
        expected['live_continuity']['baseline_target_sha256'] = runtime_target_fingerprint(expected)
        assert candidate == expected
        assert {k: v for k, v in candidate['live_continuity'].items() if k != 'baseline_target_sha256'} == {
            k: v for k, v in continuity.items() if k != 'baseline_target_sha256'}
        assert os.environ['RUNTIME_TARGET_ENABLED'] == 'false'
        assert os.environ['BINANCE_DRY_RUN'] == 'true'
        assert 'SOURCE_RUNTIME_TARGET_JSON' not in os.environ
        assert 'BINANCE_RISK_AUTHORITY_JSON' not in os.environ
        authority = Path(os.environ['BINANCE_RISK_AUTHORITY_FILE'])
        assert authority.stat().st_mode & 0o777 == 0o600
        assert authority.read_text() == 'private-fixture-authority'
        projection = dict(status='passed', validation_scope='full_cycle_no_submit', cycle_complete=True,
                          risk_outcome='APPROVE', execution_permitted=False, broker_read_count=7,
                          suppressed_side_effect_count=4, suppressed_strategy_record_count=1)
        return SimpleNamespace(returncode=0, stdout='private-fixture-raw\n' + json.dumps(projection),
                               stderr='private-fixture-stderr')

    code, result, call = run_adapter(monkeypatch, tmp_path, original, process, {
        'BINANCE_RISK_AUTHORITY_JSON': 'private-fixture-authority'})
    assert code == 0 and result['status'] == 'passed'
    assert result['executed_call_count'] == 0 and result['broker_read_count'] == 7
    assert result['validator_invoked'] is True
    assert call.call_count == 1 and original == untouched
    assert not list(tmp_path.glob('binance-dry-run-authority-*'))
    assert 'private-fixture' not in capsys.readouterr().out
    assert 'fixture-account' not in json.dumps(result)
    assert not result['production_config_written'] and not result['cron_written']


@pytest.mark.parametrize('mode', ['active', 'bad_hash', 'missing_continuity', 'duplicate_authority'])
def test_invalid_inputs_stop_before_validator(monkeypatch, tmp_path, mode):
    target = dry_run_target_payload()
    extra = {}
    if mode == 'active':
        target['live_continuity']['state'] = 'ACTIVE_LKG'
    elif mode == 'bad_hash':
        target['live_continuity']['baseline_target_sha256'] = '0' * 64
    elif mode == 'missing_continuity':
        target.pop('live_continuity')
    else:
        extra = {'BINANCE_RISK_AUTHORITY_JSON': 'private', 'BINANCE_RISK_AUTHORITY_FILE_CONFIG': '/private'}
    code, result, call = run_adapter(monkeypatch, tmp_path, target, extra=extra)
    assert code == 1 and not result['validator_invoked']
    assert result['reason_code'] == 'isolated_input_invalid'
    call.assert_not_called()
    assert 'executed_call_count' not in result


def test_authority_rejection_is_failure_without_raw_logs_or_success_claim(monkeypatch, tmp_path, capsys):
    def process(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout='private-log\n' + json.dumps({
            'status': 'failed', 'reason_code': 'runner_revision_mismatch',
            'failure_stage': 'build', 'error_type': 'ValueError', 'private': 'never-retain'}))
    code, result, call = run_adapter(monkeypatch, tmp_path, dry_run_target_payload(), process,
                                    {'BINANCE_RISK_AUTHORITY_JSON': 'private-authority'})
    assert code == 1 and result['reason_code'] == 'runner_revision_mismatch'
    assert result['validator_exit_code'] == 1 and call.call_count == 1
    assert 'executed_call_count' not in result and 'cycle_complete' not in result
    assert 'private' not in capsys.readouterr().out
    assert not list(tmp_path.glob('binance-dry-run-authority-*'))


@pytest.mark.parametrize('stdout,returncode', [
    ('private-not-json', 1),
    (json.dumps({'status': 'passed', 'execution_permitted': True}), 0),
])
def test_malformed_or_unsafe_validator_result_cannot_pass(monkeypatch, tmp_path, stdout, returncode):
    code, result, _ = run_adapter(monkeypatch, tmp_path, dry_run_target_payload(),
                                  lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout))
    assert code == 1 and result['status'] == 'failed'
    assert result['reason_code'] == 'validator_output_invalid'
