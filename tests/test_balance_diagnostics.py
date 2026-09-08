import json

import pytest

from application.broker_reconciliation import diagnose_balance_snapshot
from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256 as digest


def baseline():
    rows = ({"asset": "BTC", "free": 1.0, "locked": 0.0},
            {"asset": "USDT", "free": 10.0, "locked": 2.0})
    return {"account_scope_sha256": digest({"account_uid": "synthetic-account"}),
            "positions_sha256": digest(rows), "cash_sha256": digest({"balances": list(rows)})}


def account():
    return {"uid": "synthetic-account", "balances": [
        {"asset": "BTC", "free": "1", "locked": "0"},
        {"asset": "USDT", "free": "10", "locked": "2"},
    ]}


def test_identifies_added_zero_row_without_changing_expected_digests():
    expected = baseline()
    original = expected.copy()
    payload = account()
    payload["balances"].append({"asset": "SENSITIVE_ASSET", "free": "0", "locked": "0"})
    result = diagnose_balance_snapshot(payload, expected_digests=expected)
    assert result["reason_code"] == "balance_difference_is_zero_row_only"
    assert result["balance_difference_explained"] is True
    assert result["execution_authority_granted"] is False
    assert expected == original
    assert "SENSITIVE_ASSET" not in json.dumps(result)
    assert "synthetic-account" not in json.dumps(result)


@pytest.mark.parametrize("change", ["nonzero_added", "free_changed", "locked_changed"])
def test_real_balance_changes_never_pass_zero_row_diagnosis(change):
    payload = account()
    payload["balances"].append({"asset": "ZERO", "free": "0", "locked": "0"})
    if change == "nonzero_added":
        payload["balances"].append({"asset": "NEW", "free": "0.01", "locked": "0"})
    else:
        payload["balances"][0][change.split('_')[0]] = "9"
    result = diagnose_balance_snapshot(payload, expected_digests=baseline())
    assert result["reason_code"] == "balance_difference_unexplained"
    assert result["balance_difference_explained"] is False


def test_mismatched_account_is_rejected_before_comparison():
    payload = account()
    payload["uid"] = "another-account"
    with pytest.raises(ValueError, match="account_identity_mismatch"):
        diagnose_balance_snapshot(payload, expected_digests=baseline())


def test_mismatched_cash_digest_cannot_be_ignored():
    payload = account()
    payload["balances"].append({"asset": "ZERO", "free": "0", "locked": "0"})
    expected = baseline()
    expected["cash_sha256"] = "0" * 64
    result = diagnose_balance_snapshot(payload, expected_digests=expected)
    assert result["balance_difference_explained"] is False
