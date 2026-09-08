from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application.broker_reconciliation import diagnose_balance_flows
from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256 as digest


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def client(response=None):
    calls = []
    def read(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path in {'capital/deposit/hisrec', 'capital/withdraw/history'}:
            return []
        return {"rows": [], "total": 0} if response is None else response
    return SimpleNamespace(_request_margin_api=read), calls


def test_bounded_history_uses_only_get_and_returns_counts_not_private_rows():
    c, calls = client({"rows": [{"asset": "PRIVATE_ASSET", "amount": "123"}], "total": 1})
    result = diagnose_balance_flows(c, start=NOW-timedelta(days=5), end=NOW, now=NOW)
    assert calls and all(m == 'get' for m, _, _ in calls)
    assert len(calls) <= 20
    assert all(kw['data']['startTime'] < kw['data']['endTime'] for _, _, kw in calls)
    assert 'PRIVATE_ASSET' not in str(result)
    assert '123' not in str(result)
    assert result['complete_balance_reconciliation'] is False
    assert result['execution_authority_granted'] is False
    assert result['history_counts']['earn_subscriptions'] == 1


def test_truncated_history_stops_before_downstream_queries():
    c, calls = client({"rows": [{}], "total": 200})
    result = diagnose_balance_flows(c, start=NOW-timedelta(days=5), end=NOW, now=NOW)
    assert result['history_complete_for_requested_surfaces'] is False
    assert result['reason_code'] == 'balance_history_incomplete'
    assert result['history_counts']['earn_subscriptions'] is None
    assert len(calls) == 3


def test_provider_failure_is_redacted_and_not_retried():
    calls = []
    def read(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('SENSITIVE_PROVIDER_TEXT')
    result = diagnose_balance_flows(SimpleNamespace(_request_margin_api=read), start=NOW-timedelta(days=5), end=NOW, now=NOW)
    assert result['reason_code'] == 'balance_history_read_failed'
    assert len(calls) == 1
    assert 'SENSITIVE_PROVIDER_TEXT' not in str(result)


@pytest.mark.parametrize('span', [0, 8, -1])
def test_history_window_is_checked_before_broker_read(span):
    c, calls = client()
    with pytest.raises(ValueError):
        diagnose_balance_flows(c, start=NOW-timedelta(days=span), end=NOW, now=NOW)
    assert not calls


def test_explicit_zero_total_allows_omitted_empty_transfer_rows():
    def read(method, path, **kwargs):
        if path.startswith('capital/'):
            return []
        if path == 'asset/transfer':
            return {'total': 0}
        return {'rows': [], 'total': 0}
    result = diagnose_balance_flows(SimpleNamespace(_request_margin_api=read), start=NOW-timedelta(days=5), end=NOW, now=NOW)
    assert result['history_complete_for_requested_surfaces'] is True
    assert result['history_counts']['transfer_main_funding'] == 0
    assert result['execution_authority_granted'] is False


@pytest.mark.parametrize('payload', [{}, {'total': 1}, {'total': False}, {'total': -1}, {'total': 'unknown'}])
def test_unknown_or_nonzero_total_does_not_imply_empty_rows(payload):
    c, calls = client(payload)
    result = diagnose_balance_flows(c, start=NOW-timedelta(days=5), end=NOW, now=NOW)
    assert result['history_complete_for_requested_surfaces'] is False
    assert result['history_counts']['earn_subscriptions'] is None


def test_complete_history_reconciles_bonus_rows_without_additional_broker_reads():
    calls = []
    def read(method, path, **kwargs):
        calls.append((method, path))
        if path.startswith('capital/'):
            return []
        if path.endswith('/rewardsRecord'):
            return {'rows': [{'asset': 'USDT', 'rewards': '0.1', 'type': 'BONUS',
                              'projectId': 'synthetic', 'time': int(NOW.timestamp()*1000)}], 'total': 1}
        return {'rows': [], 'total': 0}
    rows = ({'asset': 'USDT', 'free': 1.0, 'locked': 0.0},)
    expected = {'account_scope_sha256': digest({'account_uid': 'synthetic'}),
                'positions_sha256': digest(rows), 'cash_sha256': digest({'balances': list(rows)})}
    result = diagnose_balance_flows(
        SimpleNamespace(_request_margin_api=read), start=NOW-timedelta(days=5), end=NOW, now=NOW,
        account={'uid': 'synthetic', 'balances': [{'asset': 'USDT', 'free': '1.1', 'locked': '0'}]},
        expected_digests=expected,
    )
    assert result['spot_bonus_reconciliation']['historical_balance_hashes_match'] is True
    assert result['complete_balance_reconciliation'] is False
    assert result['execution_authority_granted'] is False
    assert len(calls) == 17
    assert all(method == 'get' for method, _ in calls)


def test_invalid_reward_rows_stop_before_transfer_reads():
    calls = []
    def read(method, path, **kwargs):
        calls.append(path)
        if path.startswith('capital/'):
            return []
        if path.endswith('/rewardsRecord'):
            return {'rows': [{'asset': 'USDT', 'rewards': '0.1', 'type': 'BONUS',
                              'time': int(NOW.timestamp()*1000)}], 'total': 1}
        return {'rows': [], 'total': 0}
    rows = ({'asset': 'USDT', 'free': 1.0, 'locked': 0.0},)
    result = diagnose_balance_flows(
        SimpleNamespace(_request_margin_api=read), start=NOW-timedelta(days=5), end=NOW, now=NOW,
        account={'uid': 'synthetic', 'balances': [{'asset': 'USDT', 'free': '1.1', 'locked': '0'}]},
        expected_digests={'account_scope_sha256': digest({'account_uid': 'synthetic'}),
                          'positions_sha256': digest(rows), 'cash_sha256': digest({'balances': list(rows)})},
    )
    assert len(calls) == 5
    assert result['history_complete_for_requested_surfaces'] is False
    assert result['history_counts']['transfer_main_funding'] is None
    assert result['spot_bonus_reconciliation']['validation_failure_code'] == 'reward_project_invalid'
