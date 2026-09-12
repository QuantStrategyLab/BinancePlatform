from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application.broker_reconciliation import (
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
)
from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256 as digest


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def deposit(*, event_id="deposit-1", amount="100", status=1, complete_time=None, coin="USDT", wallet_type=0):
    completed = complete_time or int(NOW.timestamp() * 1000)
    return {
        "id": event_id,
        "amount": amount,
        "coin": coin,
        "status": status,
        "insertTime": completed - 1000,
        "completeTime": completed,
        "walletType": wallet_type,
        "transferType": 0,
        "txId": f"tx-{event_id}",
    }


def cash_flow_client(*, deposits=None, withdrawals=None):
    calls = []

    def read(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "capital/deposit/hisrec":
            return list(deposits or [])
        if path == "capital/withdraw/history":
            return list(withdrawals or [])
        raise AssertionError(path)

    return SimpleNamespace(_request_margin_api=read), calls


def test_external_cash_flow_bootstrap_records_history_without_reaccounting_old_deposit():
    c, calls = cash_flow_client(deposits=[deposit()])

    result = collect_spot_usdt_external_cash_flows(c, now=NOW, cursor=None)

    assert result["bootstrap"] is True
    assert result["new_deposit_principal_usdt"] == "0"
    assert len(result["cursor"]["records"]) == 1
    assert len(calls) == 2
    assert all(call[0] == "get" for call in calls)
    assert "deposit-1" not in str(result)


def test_external_cash_flow_new_confirmed_deposit_is_returned_once_and_then_deduplicated():
    c, _ = cash_flow_client()
    initial = collect_spot_usdt_external_cash_flows(c, now=NOW - timedelta(hours=1), cursor=None)
    c, _ = cash_flow_client(deposits=[deposit()])

    observed = collect_spot_usdt_external_cash_flows(c, now=NOW, cursor=initial["cursor"])
    repeated = collect_spot_usdt_external_cash_flows(
        c, now=NOW + timedelta(days=1), cursor=observed["cursor"]
    )

    assert observed["new_deposit_principal_usdt"] == "100"
    assert observed["new_confirmed_deposit_count"] == 1
    assert observed["new_deposit_completed_at"] == [NOW.isoformat()]
    assert repeated["new_deposit_principal_usdt"] == "0"
    assert repeated["new_confirmed_deposit_count"] == 0


def test_external_cash_flow_pending_deposit_can_become_final_inside_overlap_window():
    pending_client, _ = cash_flow_client(deposits=[deposit(status=0)])
    initial = collect_spot_usdt_external_cash_flows(
        pending_client, now=NOW, cursor=None
    )
    final_client, _ = cash_flow_client(deposits=[deposit()])

    result = collect_spot_usdt_external_cash_flows(
        final_client, now=NOW + timedelta(minutes=1), cursor=initial["cursor"]
    )

    assert result["new_deposit_principal_usdt"] == "100"
    assert result["new_confirmed_deposit_count"] == 1


def test_external_cash_flow_rejects_changed_final_identity_payload():
    c, _ = cash_flow_client(deposits=[deposit()])
    initial = collect_spot_usdt_external_cash_flows(c, now=NOW, cursor=None)
    changed, _ = cash_flow_client(deposits=[deposit(amount="101")])

    with pytest.raises(ValueError, match="external_cash_flow_record_changed"):
        collect_spot_usdt_external_cash_flows(changed, now=NOW, cursor=initial["cursor"])


@pytest.mark.parametrize(
    "deposits,withdrawals,field",
    [
        ([deposit(coin="BTC")], [], "new_unsupported_deposit_count"),
        ([deposit(wallet_type=1)], [], "new_unsupported_deposit_count"),
        ([], [{"id": "withdraw-1", "status": 6}], "new_or_changed_withdrawal_count"),
    ],
)
def test_external_cash_flow_marks_new_unsupported_activity_without_blocking_unchanged_balance(
    deposits, withdrawals, field
):
    empty, _ = cash_flow_client()
    initial = collect_spot_usdt_external_cash_flows(empty, now=NOW - timedelta(hours=1), cursor=None)
    changed, _ = cash_flow_client(deposits=deposits, withdrawals=withdrawals)

    result = collect_spot_usdt_external_cash_flows(changed, now=NOW, cursor=initial["cursor"])

    assert result[field] == 1
    assert result["new_deposit_principal_usdt"] == "0"


def test_external_cash_flow_bootstraps_old_withdrawal_without_permanent_block_and_rejects_full_page():
    pending, _ = cash_flow_client(withdrawals=[{"id": "withdraw-1", "status": 0}])
    initial = collect_spot_usdt_external_cash_flows(pending, now=NOW, cursor=None)
    repeated = collect_spot_usdt_external_cash_flows(
        pending, now=NOW + timedelta(days=1), cursor=initial["cursor"]
    )

    assert initial["new_or_changed_withdrawal_count"] == 0
    assert repeated["new_or_changed_withdrawal_count"] == 0

    full, _ = cash_flow_client(deposits=[deposit(event_id=str(index)) for index in range(1000)])
    with pytest.raises(ValueError, match="external_cash_flow_history_incomplete"):
        collect_spot_usdt_external_cash_flows(full, now=NOW, cursor=None)


def test_external_cash_flow_cursor_has_a_hard_bounded_capacity_before_provider_reads():
    c, calls = cash_flow_client()
    records = {
        f"{index:064x}": {
            "kind": "deposit",
            "payload_sha256": "b" * 64,
            "status": "pending",
        }
        for index in range(257)
    }

    with pytest.raises(ValueError, match="external_cash_flow_cursor_invalid"):
        collect_spot_usdt_external_cash_flows(
            c,
            now=NOW,
            cursor={"version": 1, "observed_at": NOW.isoformat(), "records": records},
        )

    assert calls == []


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
            return {'rows': [{'asset': 'USDT', 'rewards': '0.1', 'type': 'BONUS', 'projectId': {},
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


@pytest.mark.parametrize("duplicate", [False, True])
def test_reward_delta_diagnostic_is_bounded_private_and_not_causal_authority(duplicate):
    from decimal import Decimal
    row = {"asset": "BNB", "rewards": "0.01234567", "type": "BONUS",
           "projectId": "synthetic", "time": int(NOW.timestamp() * 1000)}
    def read(method, path, **kwargs):
        assert method == "get"
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            rows = [row, row] if duplicate else [row]
            return {"rows": rows, "total": len(rows)}
        return {"rows": [], "total": 0}
    result = diagnose_balance_flows(
        SimpleNamespace(_request_margin_api=read), start=NOW-timedelta(hours=2), end=NOW, now=NOW,
        reward_quantity_changes={"BNB": Decimal("0.01234567")},
    )
    assert "0.01234567" not in str(result)
    assert result["execution_authority_granted"] is False
    if duplicate:
        assert result["reason_code"] == "balance_history_reward_validation_failed"
    else:
        check = result["reward_quantity_checks"]["BNB"]
        assert check["delta_matches_bonus"] is True
        assert check["causal_reconciliation"] is False
