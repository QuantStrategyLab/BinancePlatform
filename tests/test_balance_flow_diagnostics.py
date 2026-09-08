from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application.broker_reconciliation import diagnose_balance_flows


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
