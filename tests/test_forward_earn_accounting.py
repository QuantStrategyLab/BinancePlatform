import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.portfolio_service import maybe_rebase_daily_state_for_balance_change, compute_daily_pnls
from runtime_support import ExecutionIntegrityError

NOW = datetime(2026, 9, 12, 14, 1, tzinfo=timezone.utc)


def materials():
    old = {'account_scope_sha256': 'a' * 64, 'observed_at': '2026-09-12T14:00:00+00:00',
           'execution_authority_granted': False, 'assets': {
        'USDT': {'spot_free': '100', 'spot_locked': '0', 'products': {}, 'quantity': '100'},
        'BNB': {'spot_free': '1', 'spot_locked': '0', 'quantity': '3', 'products': {
            'BNB001': {'total': '2', 'realtime_rewards': '0.1', 'auto_subscribe': True, 'can_redeem': True}}}}}
    new = copy.deepcopy(old)
    new['observed_at'] = NOW.isoformat()
    new['assets']['BNB']['products']['BNB001'].update(total='2.00000001', realtime_rewards='0.10000001')
    new['assets']['BNB']['quantity'] = '3.00000001'
    state = {'earn_accrual_checkpoint': old, 'last_balance_snapshot': {'USDT': 100, 'BNB': 3},
             'earn_accounted_net_changes': {'USDT': '0', 'BNB': '0'},
             'external_cash_flow_cursor': {'version': 1, 'observed_at': old['observed_at'], 'records': {}},
             'last_reset_date': '2026-09-12', 'daily_equity_base': 1600, 'daily_trend_equity_base': 0,
             'daily_trend_pnl_basis': 'trend_mark_plus_cash_flow_v1', 'daily_trend_cash_flow_usdt': 0,
             'daily_trend_third_fee_usdt': 0, 'daily_trend_risk_base_usdt': 0,
             'order_submission': {'state': 'TERMINAL'}}
    cash = {'new_deposit_principal_usdt': '0', 'new_deposit_completed_at': [],
            'new_confirmed_deposit_count': 0, 'new_unsupported_deposit_count': 0,
            'new_or_changed_withdrawal_count': 0,
            'cursor': {'version': 1, 'observed_at': NOW.isoformat(), 'records': {}}}
    return state, new, cash


def consume(state, new, cash, writer=None):
    writes = []
    runtime = SimpleNamespace(client=object(), now_utc=NOW, earn_accrual_observation=new)
    snapshot = {a: round(float(r['quantity']), 8) for a, r in new['assets'].items()}
    result = maybe_rebase_daily_state_for_balance_change(state, runtime, {}, 1600.000005, 0, snapshot, [],
        collect_external_cash_flows_fn=lambda *a, **kw: cash,
        runtime_set_trade_state_fn=writer or (lambda *a, **kw: writes.append(copy.deepcopy(a[2]))),
        append_log_fn=lambda *a: None, translate_fn=lambda *a, **kw: '')
    return result, writes


def test_minute_income_consumed_once_and_not_added_as_principal_or_extra_pnl():
    state, new, cash = materials()
    _, writes = consume(state, new, cash)
    assert len(writes) == 1
    assert state['earn_accrual_checkpoint'] == new
    assert state.get('daily_external_principal_usdt', 0) == 0
    assert state['daily_equity_base'] == 1600
    assert compute_daily_pnls(state, 1600.000005, 0)[0] == pytest.approx(0.000005 / 1600)
    later = copy.deepcopy(new)
    later['observed_at'] = '2026-09-12T14:02:00+00:00'
    cash['cursor']['observed_at'] = later['observed_at']
    consume(state, later, cash)
    assert state['earn_accounted_net_changes'] == {'USDT': '0', 'BNB': '0'}
    assert state['daily_equity_base'] == 1600


@pytest.mark.parametrize('kind', ['unknown_delta', 'withdrawal', 'counter_reset', 'unknown_order', 'missing_net', 'net_drift'])
def test_unverified_forward_activity_does_not_mutate_state(kind):
    state, new, cash = materials()
    if kind == 'unknown_delta':
        new['assets']['USDT'].update(spot_free='101', quantity='101')
    if kind == 'withdrawal': cash['new_or_changed_withdrawal_count'] = 1
    if kind == 'counter_reset': new['assets']['BNB']['products']['BNB001']['realtime_rewards'] = '0'
    if kind == 'unknown_order': state['order_submission']['state'] = 'SUBMISSION_UNKNOWN'
    if kind == 'missing_net': del state['earn_accounted_net_changes']
    if kind == 'net_drift': state['earn_accounted_net_changes']['USDT'] = '2'
    before = copy.deepcopy(state)
    with pytest.raises(ExecutionIntegrityError): consume(state, new, cash)
    assert state == before


def test_deposit_and_interest_are_separate_in_same_window():
    state, new, cash = materials()
    new['assets']['USDT'].update(spot_free='110', quantity='110')
    cash.update(new_deposit_principal_usdt='10', new_deposit_completed_at=[NOW.isoformat()], new_confirmed_deposit_count=1)
    consume(state, new, cash)
    assert state['daily_external_principal_usdt'] == 10
    assert state['daily_equity_base'] == 1600


def test_known_buy_and_bnb_fee_use_durable_fill_deltas():
    state, new, cash = materials()
    state['earn_accounted_net_changes'] = {'USDT': '-10', 'BNB': '0.01999'}
    state['last_balance_snapshot'] = {'USDT': 90, 'BNB': 3.01999}
    new['assets']['USDT'].update(spot_free='90', quantity='90')
    new['assets']['BNB'].update(spot_free='1.01999', quantity='3.01999001')
    consume(state, new, cash)
    assert state['earn_accounted_net_changes'] == {'USDT': '0', 'BNB': '0'}


def test_write_failure_does_not_advance_in_memory_checkpoint():
    state, new, cash = materials()
    before = copy.deepcopy(state)
    def fail(*a, **kw): raise RuntimeError('persist failed')
    with pytest.raises(RuntimeError): consume(state, new, cash, writer=fail)
    assert state == before


def test_fill_accumulator_is_exact_and_terminal_transition_is_idempotent():
    from runtime_support import account_known_fill_for_earn
    state, _, _ = materials()
    state['order_submission'] = {'state': 'FILLED_ACCOUNTING_PENDING', 'symbol': 'BNBUSDT', 'known_fill': {
        'side': 'BUY', 'executed_qty': '0.02', 'cummulative_quote_qty': '10',
        'commissions': [{'price': '500', 'qty': '0.02', 'commission': '0.00001', 'commission_asset': 'BNB'}]}}
    account_known_fill_for_earn(state)
    assert state['earn_accounted_net_changes'] == {'USDT': '-10', 'BNB': '0.01999'}
    state['order_submission'] = {'state': 'TERMINAL'}
    account_known_fill_for_earn(state)
    assert state['earn_accounted_net_changes'] == {'USDT': '-10', 'BNB': '0.01999'}


def test_normalizer_preserves_checkpoint_and_pending_fill_deltas():
    from trade_state_support import normalize_trade_state
    state, _, _ = materials()
    result = normalize_trade_state(state, trend_universe={}, last_good_payload_key='pool',
                                   action_history_key='history', retired_positions_key='retired')
    assert result['earn_accrual_checkpoint'] == state['earn_accrual_checkpoint']
    assert result['earn_accounted_net_changes'] == state['earn_accounted_net_changes']
    result['earn_accounted_net_changes']['USDT'] = '1'
    assert state['earn_accounted_net_changes']['USDT'] == '0'


def test_cash_refresh_checks_income_and_fill_without_advancing_unread_flow_cursor(monkeypatch):
    from runtime_support import reconcile_runtime_cash_effects
    state, new, cash = materials()
    state['order_submission'] = {'state': 'FILLED_ACCOUNTING_PENDING', 'symbol': 'BNBUSDT', 'known_fill': {
        'side': 'BUY', 'executed_qty': '0.02', 'cummulative_quote_qty': '10',
        'commissions': [{'price': '500', 'qty': '0.02', 'commission': '0.00001', 'commission_asset': 'BNB'}]}}
    new['assets']['USDT'].update(spot_free='90', quantity='90')
    new['assets']['BNB'].update(spot_free='1.01999', quantity='3.01999001')
    before_checkpoint = copy.deepcopy(state['earn_accrual_checkpoint'])
    before_cursor = copy.deepcopy(state['external_cash_flow_cursor'])
    monkeypatch.setattr('application.earn_accrual.collect_earn_checkpoint', lambda *a, **kw: new)
    runtime = SimpleNamespace(client=object(), state_owner_held=True, state_owner_id='synthetic',
        fuel_symbol='BNBUSDT', pending_funds=[{'asset': 'BNB', 'confirmed': True}])
    reconcile_runtime_cash_effects(runtime, state)
    assert state['order_submission'] == {'state': 'TERMINAL'}
    assert state['earn_accrual_checkpoint'] == before_checkpoint
    assert state['external_cash_flow_cursor'] == before_cursor
    assert state['earn_accounted_net_changes'] == {'USDT': '-10', 'BNB': '0.01999'}
    consume(state, new, cash)
    assert state['earn_accrual_checkpoint'] == new
    assert state['earn_accounted_net_changes'] == {'USDT': '0', 'BNB': '0'}


def test_market_valuation_uses_same_checkpoint_not_independent_balance_reads(monkeypatch):
    from market_snapshot_support import capture_market_snapshot
    state, new, _ = materials()
    for checkpoint in (state['earn_accrual_checkpoint'], new):
        checkpoint['assets']['BTC'] = {'spot_free': '0', 'spot_locked': '0', 'quantity': '0', 'products': {}}
    c = SimpleNamespace(get_avg_price=lambda **kw: {'price': '500'})
    runtime = SimpleNamespace(client=c, trade_state=state, now_utc=NOW)
    monkeypatch.setattr('application.earn_accrual.collect_earn_checkpoint', lambda *a, **kw: new)
    def forbidden(*a, **kw): raise AssertionError('separate balance read')
    result = capture_market_snapshot(runtime, {}, {}, [], get_total_balance_fn=forbidden,
        resolve_btc_snapshot_fn=lambda *a: {'ready': True}, resolve_trend_indicators_fn=lambda *a: {})
    assert result['balances']['BNBUSDT'] == 3.00000001
    assert result['u_total'] == 100
    assert runtime.earn_accrual_observation == new
    assert runtime.now_utc.isoformat() == new['observed_at']


def test_accounting_terminal_hook_records_raw_fill_before_discarding_it():
    from application.execution_service import _prepare_accounting_state
    state, _, _ = materials()
    state['order_submission'] = {'state': 'FILLED_ACCOUNTING_PENDING', 'symbol': 'BNBUSDT', 'known_fill': {
        'side': 'BUY', 'executed_qty': '0.02', 'cummulative_quote_qty': '10',
        'commissions': [{'price': '500', 'qty': '0.02', 'commission': '0.00001', 'commission_asset': 'BNB'}]}}
    _prepare_accounting_state(state, symbol='BNBUSDT', base_asset='BNB', balances={'BNBUSDT': 3.01999}, u_total=90)
    assert state['order_submission'] == {'state': 'TERMINAL'}
    assert state['earn_accounted_net_changes'] == {'USDT': '-10', 'BNB': '0.01999'}
