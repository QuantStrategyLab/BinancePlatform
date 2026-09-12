import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import migrate_daily_accounting_state as m

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)


class ReadOnlyClient:
    def __init__(self, *, cash=600, deposits=None):
        self.cash = cash
        self.deposits = deposits if deposits is not None else [{
            'id': 'PRIVATE_SYNTHETIC_ID', 'status': 1, 'amount': '100', 'coin': 'USDT',
            'walletType': 0, 'transferType': 0, 'txId': 'PRIVATE_SYNTHETIC_TX',
            'insertTime': int((NOW-timedelta(minutes=2)).timestamp()*1000),
            'completeTime': int((NOW-timedelta(minutes=1)).timestamp()*1000),
        }]
        self.history_calls = []

    def get_account(self):
        return {'uid': 'PRIVATE_SYNTHETIC_ACCOUNT', 'balances': [
            {'asset': a, 'free': str(self.cash if a == 'USDT' else 0), 'locked': '0'}
            for a in ('USDT', 'BTC', 'BNB')
        ]}

    def get_simple_earn_flexible_product_position(self, **kwargs):
        return {'rows': [], 'total': 0}

    def _request_margin_api(self, method, path, **kwargs):
        assert method == 'get' and kwargs['signed'] is True
        assert path in {'capital/deposit/hisrec', 'capital/withdraw/history'}
        self.history_calls.append(path)
        return self.deposits if path == 'capital/deposit/hisrec' else []


def setup_preview(monkeypatch, *, cursor=True):
    ledger = {
        'order_submission': {'state': 'TERMINAL'},
        'last_reset_date': NOW.date().isoformat(),
        'last_balance_snapshot': {'USDT': 500.0, 'BTC': 0.0, 'BNB': 0.0},
        'daily_trend_risk_base_usdt': 1000.0, 'is_circuit_broken': True,
    }
    if cursor:
        ledger['external_cash_flow_cursor'] = {
            'version': 1, 'observed_at': (NOW-timedelta(hours=1)).isoformat(), 'records': {},
        }
    source = (SimpleNamespace(update_time=NOW), ledger, {'state': 'RECONCILE_ONLY'})
    monkeypatch.setattr(m, '_read_source', lambda refs: source)
    monkeypatch.setenv('BINANCE_RECONCILIATION_SYMBOLS', 'BTCUSDT,BNBUSDT')
    expected = {'account_scope_sha256': m.digest({'account_uid': 'PRIVATE_SYNTHETIC_ACCOUNT'})}
    return ledger, expected


@pytest.mark.parametrize('cursor,cash,deposits,status,reason', [
    (True, 600, None, 'reconciled_preview', None),
    (False, 500, None, 'baseline_preview', None),
    (True, 500, [], 'no_new_deposit', None),
    (False, 600, None, 'blocked', 'external_cash_flow_cursor_missing'),
    (True, 700, None, 'blocked', 'external_cash_flow_balance_mismatch'),
])
def test_preview_reuses_real_collector_and_consumer_without_mutating_ledger(
    monkeypatch, cursor, cash, deposits, status, reason,
):
    ledger, expected = setup_preview(monkeypatch, cursor=cursor)
    before = copy.deepcopy(ledger)
    client = ReadOnlyClient(cash=cash, deposits=deposits)
    result = m.preview_external_cash_flow({}, client=client, expected=expected, now=NOW)
    assert ledger == before
    assert result['status'] == status
    assert result.get('reason_code') == reason
    assert result['write_performed'] is False
    assert result['execution_authority_granted'] is False
    assert result['complete_balance_reconciliation'] is False
    assert result['cash_flow_history_read'] is True
    assert len(client.history_calls) == 2
    assert 'PRIVATE_SYNTHETIC' not in json.dumps(result)
    assert 'principal' not in json.dumps(result)


def test_identity_mismatch_blocks_before_private_history(monkeypatch):
    _, expected = setup_preview(monkeypatch)
    expected['account_scope_sha256'] = 'f'*64
    client = ReadOnlyClient()
    with pytest.raises(m.MigrationBlocked, match='account_scope_unverified'):
        m.preview_external_cash_flow({}, client=client, expected=expected, now=NOW)
    assert client.history_calls == []


def test_history_failure_is_generic_and_not_retried(monkeypatch):
    _, expected = setup_preview(monkeypatch)
    client = ReadOnlyClient()
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('PRIVATE_SYNTHETIC_PROVIDER_ERROR')
    client._request_margin_api = fail
    with pytest.raises(m.MigrationBlocked, match='^external_cash_flow_history_read_failed$'):
        m.preview_external_cash_flow({}, client=client, expected=expected, now=NOW)
    assert calls == [1]


def test_source_change_during_reads_blocks_preview(monkeypatch):
    ledger, expected = setup_preview(monkeypatch)
    sources = iter([
        (SimpleNamespace(update_time=NOW), ledger, {'state': 'RECONCILE_ONLY'}),
        (SimpleNamespace(update_time=NOW+timedelta(seconds=1)), copy.deepcopy(ledger), {'state': 'RECONCILE_ONLY'}),
    ])
    monkeypatch.setattr(m, '_read_source', lambda refs: next(sources))
    with pytest.raises(m.MigrationBlocked, match='^cash_flow_preview_state_changed$'):
        m.preview_external_cash_flow({}, client=ReadOnlyClient(), expected=expected, now=NOW)


@pytest.mark.parametrize('field,value,reason', [
    ('completeTime', int((NOW-timedelta(days=1)).timestamp()*1000), 'external_cash_flow_late_completion'),
    ('coin', 'BTC', 'external_cash_flow_scope_unsupported'),
])
def test_unsupported_or_late_deposit_keeps_production_refusal(monkeypatch, field, value, reason):
    ledger, expected = setup_preview(monkeypatch)
    client = ReadOnlyClient()
    client.deposits[0][field] = value
    if field == 'completeTime':
        client.deposits[0]['insertTime'] = value-60000
    before = copy.deepcopy(ledger)
    result = m.preview_external_cash_flow({}, client=client, expected=expected, now=NOW)
    assert result['status'] == 'blocked'
    assert result['reason_code'] == reason
    assert ledger == before


@pytest.mark.parametrize('cash,exit_code,status', [(500, 0, 'baseline_preview'), (600, 2, 'blocked')])
def test_cli_uses_real_preview_without_migration_or_persistence(monkeypatch, capsys, cash, exit_code, status):
    _, expected = setup_preview(monkeypatch, cursor=False)
    monkeypatch.setattr(m, 'require_runtime_context', lambda: None)
    monkeypatch.setattr(m, 'resolve_runtime_target_from_env', lambda **kwargs: SimpleNamespace(live_continuity=SimpleNamespace(state='RECONCILE_ONLY')))
    monkeypatch.setattr(m, '_expected_digests', lambda: expected)
    monkeypatch.setattr(m, '_refs', lambda: {})
    monkeypatch.setattr(m, 'connect_client', lambda *args, **kwargs: ReadOnlyClient(cash=cash))
    monkeypatch.setenv('BINANCE_API_KEY', 'PRIVATE_SYNTHETIC_KEY')
    monkeypatch.setenv('BINANCE_API_SECRET', 'PRIVATE_SYNTHETIC_SECRET')
    original = m.run
    monkeypatch.setattr(m, 'run', lambda action, **kwargs: original(action, now=NOW, **kwargs))
    def forbidden(*args, **kwargs):
        raise AssertionError('must not reach migration, write, or execution')
    for name in ('_collect_evidence', 'build_candidate', 'compare_and_apply', 'get_firestore_client', 'publish_private_spot_scope_preview'):
        monkeypatch.setattr(m, name, forbidden)
    assert m.main(['cash-flow-preview']) == exit_code
    output = capsys.readouterr()
    assert json.loads(output.out)['status'] == status
    assert 'PRIVATE_SYNTHETIC' not in output.out+output.err
