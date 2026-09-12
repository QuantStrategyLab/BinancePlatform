"""Synthetic prospective checkpoints; never evidence about a live account."""
import copy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from application.earn_accrual import collect_earn_checkpoint, compare_earn_checkpoints

NOW = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)


def client():
    return SimpleNamespace(
        get_account=lambda: {'uid': 'synthetic', 'balances': [
            {'asset': 'BNB', 'free': '1', 'locked': '0'}]},
        get_simple_earn_flexible_product_position=lambda **kw: {'total': 1, 'rows': [{
            'asset': 'BNB', 'productId': 'BNB001', 'totalAmount': '2',
            'cumulativeRealTimeRewards': '0.12345678', 'collateralAmount': '0',
            'autoSubscribe': True, 'canRedeem': True}]},
    )


def checkpoint():
    return collect_earn_checkpoint(client(), assets=['BNB'], observed_at=NOW)


def test_checkpoint_preserves_raw_decimal_and_has_no_authority():
    p = checkpoint()
    assert p['assets']['BNB']['quantity'] == '3'
    assert p['assets']['BNB']['products']['BNB001']['realtime_rewards'] == '0.12345678'
    assert 'synthetic' not in str(p)
    assert p['execution_authority_granted'] is False


def test_minute_accrual_is_delta_not_absolute_counter_or_a_second_daily_credit():
    previous = checkpoint()
    current = copy.deepcopy(previous)
    current['observed_at'] = '2026-09-12T14:01:00+00:00'
    current['assets']['BNB']['quantity'] = '3.00000001'
    current['assets']['BNB']['products']['BNB001']['total'] = '2.00000001'
    current['assets']['BNB']['products']['BNB001']['realtime_rewards'] = '0.12345679'
    result = compare_earn_checkpoints(previous, current, verified_net_changes={'BNB': '0'})
    assert result['broker_reported_accrual']['BNB'] == '0.00000001'
    assert result['quantities_conserve'] is True
    assert result['execution_authority_granted'] is False
    assert previous['assets']['BNB']['quantity'] == '3'
    again = copy.deepcopy(current)
    again['observed_at'] = '2026-09-12T14:02:00+00:00'
    assert Decimal(compare_earn_checkpoints(current, again, verified_net_changes={'BNB': '0'})['broker_reported_accrual']['BNB']) == 0


@pytest.mark.parametrize('change', ['reset', 'product', 'amount', 'scope', 'missing_flow'])
def test_unverified_changes_never_advance_checkpoint(change):
    previous = checkpoint()
    current = copy.deepcopy(previous)
    current['observed_at'] = '2026-09-12T14:01:00+00:00'
    flows = {'BNB': '0'}
    if change == 'reset': current['assets']['BNB']['products']['BNB001']['realtime_rewards'] = '0'
    if change == 'product': current['assets']['BNB']['products'] = {}
    if change == 'amount': current['assets']['BNB']['quantity'] = '2.9'
    if change == 'scope': current['account_scope_sha256'] = '0' * 64
    if change == 'missing_flow': flows = {}
    with pytest.raises(ValueError): compare_earn_checkpoints(previous, current, verified_net_changes=flows)


@pytest.mark.parametrize('change', ['missing_counter', 'collateral', 'full_page', 'duplicate'])
def test_incomplete_or_collateralized_earn_is_not_an_opening(change):
    c = client()
    response = c.get_simple_earn_flexible_product_position()
    if change == 'missing_counter': del response['rows'][0]['cumulativeRealTimeRewards']
    if change == 'collateral': response['rows'][0]['collateralAmount'] = '0.1'
    if change == 'full_page': response['total'] = 100
    if change == 'duplicate': response['rows'] *= 2; response['total'] = 2
    c.get_simple_earn_flexible_product_position = lambda **kw: response
    with pytest.raises(ValueError): collect_earn_checkpoint(c, assets=['BNB'], observed_at=NOW)


def test_new_opening_captures_forward_cursors_without_reconstructing_old_rewards():
    from scripts.migrate_daily_accounting_state import collect_prospective_opening
    c = client()
    c.get_open_orders = lambda: []
    c.get_my_trades = lambda **kw: []
    c.get_avg_price = lambda **kw: {'price': '500'}
    ledger = {'last_balance_snapshot': {'BNB': 2.9}}
    cursor = {'version': 1, 'observed_at': NOW.isoformat(), 'records': {}}
    result = collect_prospective_opening(c, ledger=ledger,
        expected={'account_scope_sha256': checkpoint()['account_scope_sha256']}, now=NOW,
        clock=lambda: NOW.replace(second=10),
        collect_cash_flows=lambda *a, **kw: {'cursor': cursor})
    assert result['balance_snapshot']['BNB'] == 3
    assert result['opening_mode'] == 'prospective'
    assert result['history_complete'] is False
    assert result['earn_accrual_checkpoint']['assets']['BNB']['quantity'] == '3'
    assert result['external_cash_flow_cursor'] == cursor
    assert ledger['last_balance_snapshot']['BNB'] == 2.9


def test_opening_accepts_only_conserving_minute_reward_during_sampling():
    from scripts.migrate_daily_accounting_state import collect_prospective_opening
    c = client()
    original = c.get_simple_earn_flexible_product_position
    calls = []
    def positions(**kw):
        payload = original(**kw)
        if calls:
            payload['rows'][0]['totalAmount'] = '2.00000001'
            payload['rows'][0]['cumulativeRealTimeRewards'] = '0.12345679'
        calls.append(True)
        return payload
    c.get_simple_earn_flexible_product_position = positions
    c.get_open_orders = lambda: []
    c.get_my_trades = lambda **kw: []
    c.get_avg_price = lambda **kw: {'price': '500'}
    result = collect_prospective_opening(c, ledger={'last_balance_snapshot': {'BNB': 2.9}},
        expected={'account_scope_sha256': checkpoint()['account_scope_sha256']}, now=NOW,
        clock=lambda: NOW.replace(second=10),
        collect_cash_flows=lambda *a, **kw: {'cursor': {'records': {}}})
    assert result['earn_accrual_checkpoint']['assets']['BNB']['quantity'] == '3.00000001'
    assert result['earn_accrual_checkpoint']['observed_at'] == NOW.replace(second=10).isoformat()


@pytest.mark.parametrize('change', ['orders', 'trades', 'flows', 'identity', 'unexplained'])
def test_opening_rejects_activity_and_unexplained_sampling_changes(change):
    from scripts.migrate_daily_accounting_state import collect_prospective_opening, MigrationBlocked
    c = client()
    c.get_open_orders = lambda: [{'symbol': 'BNBUSDT'}] if change == 'orders' else []
    c.get_my_trades = lambda **kw: [{'id': 1}] if change == 'trades' else []
    c.get_avg_price = lambda **kw: {'price': '500'}
    calls = []
    def cash(*a, **kw):
        records = {'new': {}} if calls and change == 'flows' else {}
        calls.append(True)
        return {'cursor': {'records': records}}
    if change == 'unexplained':
        original = c.get_simple_earn_flexible_product_position
        reads = []
        def positions(**kw):
            result = original(**kw)
            if reads: result['rows'][0]['totalAmount'] = '2.00000001'
            reads.append(True)
            return result
        c.get_simple_earn_flexible_product_position = positions
    scope = '0' * 64 if change == 'identity' else checkpoint()['account_scope_sha256']
    with pytest.raises(MigrationBlocked):
        collect_prospective_opening(c, ledger={'last_balance_snapshot': {'BNB': 2.9}},
            expected={'account_scope_sha256': scope}, now=NOW,
            clock=lambda: NOW.replace(second=10), collect_cash_flows=cash)


@pytest.mark.parametrize('endpoint,expected', [
    ('get_account', 'earn_checkpoint_account_read_unavailable'),
    ('get_simple_earn_flexible_product_position', 'earn_checkpoint_earn_read_unavailable'),
])
def test_checkpoint_read_diagnostic_never_exposes_provider_error(endpoint, expected):
    c = client()
    def fail(**kw):
        raise RuntimeError('PRIVATE_TOKEN_AND_ACCOUNT')
    setattr(c, endpoint, fail)
    with pytest.raises(ValueError) as error:
        collect_earn_checkpoint(c, assets=['BNB'], observed_at=NOW)
    assert str(error.value) == expected
    assert error.value.__suppress_context__
