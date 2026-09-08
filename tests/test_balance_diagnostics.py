import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from application.broker_reconciliation import diagnose_balance_snapshot
from application.broker_reconciliation import diagnose_bonus_reward_balance
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


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def reward(**overrides):
    return {"asset": "USDT", "rewards": "0.1", "projectId": "synthetic-product",
            "type": "BONUS", "time": int((NOW-timedelta(hours=1)).timestamp()*1000), **overrides}


def bonus_diagnosis(payload, rewards, expected=None):
    return diagnose_bonus_reward_balance(
        payload, rewards=rewards, expected_digests=expected or baseline(),
        start=NOW-timedelta(days=5), end=NOW,
    )


def test_bonus_rewards_reconstruct_exact_legacy_hashes_without_mutation_or_authority():
    payload = account()
    payload["balances"][1]["free"] = "10.3"
    rewards = [reward(), reward(rewards="0.2", time=int(NOW.timestamp()*1000))]
    original = deepcopy((payload, rewards))
    result = bonus_diagnosis(payload, rewards)
    assert result["historical_balance_hashes_match"] is True
    assert result["reason_code"] == "spot_bonus_rewards_explain_balance_difference"
    assert result["execution_authority_granted"] is False
    assert result["complete_balance_reconciliation"] is False
    assert result["reward_type_counts"] == {"BONUS": 2, "REALTIME": 0, "REWARDS": 0}
    assert (payload, rewards) == original
    assert all(value not in json.dumps(result) for value in ["USDT", "synthetic-product", "synthetic-account", "10.3"])


@pytest.mark.parametrize("change", ["cash_hash", "locked", "unexplained_free", "new_nonzero"])
def test_bonus_math_cannot_hide_other_balance_changes(change):
    payload = account()
    payload["balances"][1]["free"] = "10.1"
    expected = baseline()
    if change == "cash_hash":
        expected["cash_sha256"] = "0"*64
    elif change == "locked":
        payload["balances"][0]["locked"] = "1"
    elif change == "unexplained_free":
        payload["balances"][0]["free"] = "2"
    else:
        payload["balances"].append({"asset": "NEW", "free": "1", "locked": "0"})
    assert bonus_diagnosis(payload, [reward()], expected)["historical_balance_hashes_match"] is False


def test_realtime_rewards_are_not_subtracted_from_spot_balance():
    payload = account()
    payload["balances"][1]["free"] = "10.1"
    result = bonus_diagnosis(payload, [reward(type="REALTIME")])
    assert result["historical_balance_hashes_match"] is False
    assert result["reward_type_counts"]["REALTIME"] == 1


@pytest.mark.parametrize("bad", [
    {"rewards": "NaN"}, {"rewards": "-1"}, {"rewards": True}, {"rewards": "1e999999999"},
    {"type": "UNKNOWN"}, {"time": True}, {"time": 0}, {"asset": ""}, {"projectId": ""},
])
def test_invalid_reward_rows_cannot_prove_balance_reconstruction(bad):
    result = bonus_diagnosis(account(), [reward(**bad)])
    assert result["historical_balance_hashes_match"] is False
    assert result["reason_code"] == "spot_bonus_reward_rows_invalid"
    assert result["reward_type_counts"] is None


def test_duplicate_rewards_do_not_get_counted_twice():
    payload = account()
    payload["balances"][1]["free"] = "10.2"
    result = bonus_diagnosis(payload, [reward(), reward()])
    assert result["historical_balance_hashes_match"] is False
    assert result["reason_code"] == "spot_bonus_reward_rows_invalid"


def test_reward_amount_cannot_make_historical_free_balance_negative():
    result = bonus_diagnosis(account(), [reward(rewards="99")])
    assert result["historical_balance_hashes_match"] is False


def test_bonus_reconstruction_still_checks_account_identity():
    payload = account()
    payload["uid"] = "other-account"
    with pytest.raises(ValueError, match="account_identity_mismatch"):
        bonus_diagnosis(payload, [reward()])


def test_bonus_reconstruction_can_prove_an_added_zero_row_without_discarding_nonzero_rows():
    payload = account()
    payload["balances"][1]["free"] = "10.1"
    payload["balances"].append({"asset": "NEW_ZERO", "free": "0", "locked": "0"})
    assert bonus_diagnosis(payload, [reward()])["historical_balance_hashes_match"] is True
