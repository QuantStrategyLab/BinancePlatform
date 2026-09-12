"""Synthetic treasury receipts: an ACK is not spendable cash."""
from unittest.mock import Mock
import pytest
from tests.test_runtime_support import owned_runtime, DurableSubmissionStateStore
from runtime_support import (
    OrderReconciliationError,
    build_execution_report,
    reconcile_pending_funding_submission,
    runtime_call_client,
)


def _case(status='PAID', after='16'):
    store = DurableSubmissionStateStore()
    client = Mock()
    client.get_asset_balance.side_effect = [{'free': '6', 'locked': '0'}, {'free': after, 'locked': '0'}]
    client.redeem_simple_earn_flexible_product.return_value = {'success': True, 'redeemId': 7}
    client._request_margin_api.return_value = {'rows': [{'redeemId': 7, 'projectId': 'USDT001', 'asset': 'USDT', 'amount': '10', 'destAccount': 'SPOT', 'status': status}], 'total': 1}
    runtime = owned_runtime(client=client, state_loader=store.load, state_writer=store.write)
    return store, client, runtime


@pytest.mark.parametrize('status,after', [('PENDING', '16'), ('PAID', '6'), ('PAID', 'NaN')])
def test_ack_without_matched_settlement_keeps_durable_unknown(status, after):
    store, client, runtime = _case(status, after)
    with pytest.raises(OrderReconciliationError):
        runtime_call_client(runtime, build_execution_report(runtime), method_name='redeem_simple_earn_flexible_product',
                            payload={'productId': 'USDT001', 'amount': 10, 'destAccount': 'SPOT'}, effect_type='earn_redeem', accounting_asset='USDT')
    assert store.data['order_submission']['state'] == 'SUBMISSION_UNKNOWN'
    assert store.data['order_submission']['funding_receipt']['id'] == 7
    client.redeem_simple_earn_flexible_product.assert_called_once()
    restarted = owned_runtime(client=client, state_loader=store.load, state_writer=store.write)
    with pytest.raises(OrderReconciliationError):
        runtime_call_client(restarted, build_execution_report(restarted), method_name='order_market_buy',
                            payload={'symbol': 'BTCUSDT', 'quantity': .001}, effect_type='order_buy')
    client.order_market_buy.assert_not_called()


def test_paid_receipt_and_actual_cash_allow_funding_to_complete():
    store, client, runtime = _case()
    runtime_call_client(runtime, build_execution_report(runtime), method_name='redeem_simple_earn_flexible_product',
                        payload={'productId': 'USDT001', 'amount': 10, 'destAccount': 'SPOT'}, effect_type='earn_redeem', accounting_asset='USDT')
    assert store.data['order_submission'] == {'state': 'TERMINAL'}
    client._request_margin_api.assert_called_once()


def test_restart_reconciles_durable_receipt_without_repeating_funding_post():
    receipt = {
        'id': 7,
        'asset': 'USDT',
        'product_id': 'USDT001',
        'amount': '10',
        'spot_before': '6',
    }
    store = DurableSubmissionStateStore({
        'state': 'SUBMISSION_UNKNOWN',
        'identity_sha256': 'a' * 64,
        'method_name': 'redeem_simple_earn_flexible_product',
        'funding_receipt': receipt,
    })
    store.data['last_balance_snapshot'] = {'USDT': 16.0}
    client = Mock()
    client.get_asset_balance.return_value = {'asset': 'USDT', 'free': '16', 'locked': '0'}
    client.get_simple_earn_flexible_product_position.return_value = {'total': 0, 'rows': []}
    client._request_margin_api.return_value = {
        'total': 1,
        'rows': [{
            'redeemId': 7,
            'projectId': 'USDT001',
            'asset': 'USDT',
            'amount': '10',
            'destAccount': 'SPOT',
            'status': 'PAID',
        }],
    }
    runtime = owned_runtime(client=client, state_loader=store.load, state_writer=store.write)

    assert reconcile_pending_funding_submission(runtime) is True

    client.redeem_simple_earn_flexible_product.assert_not_called()
    client.subscribe_simple_earn_flexible_product.assert_not_called()
    client._request_margin_api.assert_called_once()
    client.get_simple_earn_flexible_product_position.assert_called_once()
    assert store.data['order_submission'] == {'state': 'TERMINAL'}
    assert store.data['last_balance_snapshot']['USDT'] == 16.0
    assert runtime.pending_funds == []


def test_restart_does_not_swallow_unattributed_managed_balance_change():
    receipt = {
        'id': 7,
        'asset': 'USDT',
        'product_id': 'USDT001',
        'amount': '10',
        'spot_before': '6',
    }
    record = {
        'state': 'SUBMISSION_UNKNOWN',
        'identity_sha256': 'a' * 64,
        'method_name': 'redeem_simple_earn_flexible_product',
        'funding_receipt': receipt,
    }
    store = DurableSubmissionStateStore(record)
    store.data['last_balance_snapshot'] = {'USDT': 15.0}
    client = Mock()
    client.get_asset_balance.return_value = {'asset': 'USDT', 'free': '16', 'locked': '0'}
    client.get_simple_earn_flexible_product_position.return_value = {'total': 0, 'rows': []}
    client._request_margin_api.return_value = {
        'total': 1,
        'rows': [{
            'redeemId': 7,
            'projectId': 'USDT001',
            'asset': 'USDT',
            'amount': '10',
            'destAccount': 'SPOT',
            'status': 'PAID',
        }],
    }
    runtime = owned_runtime(client=client, state_loader=store.load, state_writer=store.write)

    with pytest.raises(OrderReconciliationError, match='funding_balance_conservation_unverified'):
        reconcile_pending_funding_submission(runtime)

    client.redeem_simple_earn_flexible_product.assert_not_called()
    client.subscribe_simple_earn_flexible_product.assert_not_called()
    client._request_margin_api.assert_called_once()
    assert store.data['order_submission'] == record
    assert store.data['last_balance_snapshot'] == {'USDT': 15.0}


@pytest.mark.parametrize('dry_run,permitted', [(True, True), (False, False)])
def test_disabled_funding_recovery_performs_no_reads_or_writes(dry_run, permitted):
    client = Mock()
    runtime = owned_runtime(
        dry_run=dry_run,
        standard_execution_permitted=permitted,
        client=client,
    )

    assert reconcile_pending_funding_submission(runtime) is False
    assert client.mock_calls == []


def test_managed_valuation_keeps_earn_but_rejects_unavailable_or_partial_data():
    from runtime_support import read_managed_balance, ExecutionIntegrityError
    client = Mock()
    client.get_asset_balance.return_value = {'free': '6', 'locked': '1'}
    client.get_simple_earn_flexible_product_position.return_value = {'total': 2, 'rows': [
        {'asset': 'USDT', 'productId': 'one', 'totalAmount': '10'},
        {'asset': 'USDT', 'productId': 'two', 'totalAmount': '20'}]}
    assert read_managed_balance(client, 'USDT') == 37
    client.get_simple_earn_flexible_product_position.return_value['total'] = 3
    with pytest.raises(ExecutionIntegrityError):
        read_managed_balance(client, 'USDT')
    client.get_simple_earn_flexible_product_position.side_effect = TimeoutError('private')
    with pytest.raises(ExecutionIntegrityError, match='managed_balance_unavailable'):
        read_managed_balance(client, 'USDT')
