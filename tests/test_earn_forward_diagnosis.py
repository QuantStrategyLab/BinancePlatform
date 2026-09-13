import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
SCOPE = "a" * 64


class Snapshot:
    def __init__(self, value, update_time="2026-09-13T09:59:00Z"):
        self.value = value
        self.exists = value is not None
        self.update_time = update_time

    def to_dict(self):
        return copy.deepcopy(self.value)


class Ref:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, **_kwargs):
        return self.snapshot


def checkpoint(*, observed_at, quantity="1", reward="0"):
    return {
        "account_scope_sha256": SCOPE,
        "observed_at": observed_at,
        "assets": {
            "USDT": {
                "spot_free": "10",
                "spot_locked": "0",
                "products": {},
                "quantity": "10",
            },
            "BTC": {
                "spot_free": "0",
                "spot_locked": "0",
                "products": {
                    "BTC001": {
                        "total": quantity,
                        "realtime_rewards": reward,
                        "auto_subscribe": False,
                        "can_redeem": True,
                    }
                },
                "quantity": quantity,
            },
        },
        "execution_authority_granted": False,
    }


def source(*, owner=True, checkpoint_value=None):
    checkpoint_value = checkpoint_value or checkpoint(
        observed_at="2026-09-13T09:00:00+00:00"
    )
    ledger = {
        "earn_accrual_checkpoint": checkpoint_value,
        "earn_accounted_net_changes": {"USDT": "0", "BTC": "0"},
        "external_cash_flow_cursor": {
            "version": 1,
            "observed_at": checkpoint_value["observed_at"],
            "records": {},
        },
        "order_submission": {"state": "TERMINAL"},
    }
    return {
        "ledger_ref": Ref(Snapshot(ledger)),
        "owner_ref": Ref(Snapshot({"owner": "held"}) if owner else Snapshot(None)),
        "control_ref": Ref(Snapshot({"state": "RECONCILE_ONLY"})),
    }


def install_read_stubs(monkeypatch, migration, refs, *, current=None, history=None, observations=None, flows=None):
    current = current or checkpoint(
        observed_at="2026-09-13T10:00:00+00:00", quantity="1.1", reward="0"
    )
    history = history or {
        "history_complete_for_requested_surfaces": True,
        "history_counts": {"earn_rewards": 1},
        "reward_quantity_checks": {
            "USDT": {
                "delta_matches_bonus": False,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": False,
                "reward_counts": {"BONUS": 0, "REALTIME": 0},
            },
            "BTC": {
                "delta_matches_bonus": True,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": True,
                "reward_counts": {"BONUS": 1, "REALTIME": 0},
            },
        },
    }
    observations = observations or SimpleNamespace(
        account_scope={"account_uid": "123"},
        recent_executions=(),
        open_orders=(),
    )
    flows = flows or {
        "new_deposit_principal_usdt": "0",
        "new_confirmed_deposit_count": 0,
        "new_unsupported_deposit_count": 0,
        "new_or_changed_withdrawal_count": 0,
        "cursor": {"version": 1, "observed_at": "2026-09-13T10:00:00+00:00", "records": {}},
    }
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    monkeypatch.setattr(migration, "_symbols_from_env", lambda: ("BTCUSDT",))
    monkeypatch.setattr(migration, "_expected_digests", lambda: {"account_scope_sha256": SCOPE})
    monkeypatch.setattr(migration, "digest", lambda _value: SCOPE)
    monkeypatch.setattr(
        "application.earn_accrual.collect_earn_checkpoint",
        lambda *args, **kwargs: copy.deepcopy(current),
    )
    monkeypatch.setattr(migration, "collect_spot_usdt_external_cash_flows", lambda *a, **k: copy.deepcopy(flows))
    monkeypatch.setattr(migration, "collect_read_only_reconciliation_observations", lambda *a, **k: observations)
    monkeypatch.setattr(migration, "diagnose_balance_flows", lambda *a, **k: copy.deepcopy(history))
    return current, history


def test_existing_owner_is_reported_but_does_not_block_read_only_diagnosis(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source(owner=True)
    refs["control_ref"].snapshot.value["state"] = "ACTIVE_LKG"
    install_read_stubs(monkeypatch, migration, refs)
    result = migration.diagnose_earn_forward(
        refs,
        client=object(),
        expected={"account_scope_sha256": SCOPE},
        now=NOW,
    )

    assert result["owner_exists"] is True
    assert result["activation_allowed"] is False
    assert result["write_performed"] is False
    assert result["assets"]["BTC"]["residual_matches_bonus_records"] is True
    assert result["assets"]["BTC"]["trade_count"] == 0
    assert result["assets"]["BTC"]["trade_net_matches_persisted"] is True
    assert result["assets"]["BTC"]["trade_net_diagnostic_only"] is True
    assert result["assets"]["USDT"]["external_flow_matches_residual"] is True
    assert refs["ledger_ref"].snapshot.value["earn_accounted_net_changes"] == {"USDT": "0", "BTC": "0"}


def test_realtime_counter_is_subtracted_before_bonus_residual_match(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    current = checkpoint(
        observed_at="2026-09-13T10:00:00+00:00", quantity="3", reward="1"
    )
    history = {
        "history_complete_for_requested_surfaces": True,
        "history_counts": {"earn_rewards": 2},
        "reward_quantity_checks": {
            "USDT": {
                "delta_matches_bonus": False,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": False,
                "reward_counts": {"BONUS": 0, "REALTIME": 0},
            },
            "BTC": {
                "delta_matches_bonus": True,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": False,
                "reward_counts": {"BONUS": 1, "REALTIME": 1},
            },
        },
    }
    install_read_stubs(monkeypatch, migration, refs, current=current, history=history)
    result = migration.diagnose_earn_forward(
        refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
    )

    btc = result["assets"]["BTC"]
    assert btc["realtime_counter_status"] == "INCREASE"
    assert btc["realtime_record_count"] == 1
    assert btc["residual_matches_bonus_records"] is True
    assert btc["classification"] == "residual_matches_bonus_records"
    assert btc["causal_reconciliation"] is False


def test_realtime_only_change_is_zero_after_counter_and_not_bonus(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    current = checkpoint(
        observed_at="2026-09-13T10:00:00+00:00", quantity="2", reward="1"
    )
    history = {
        "history_complete_for_requested_surfaces": True,
        "history_counts": {"earn_rewards": 1},
        "reward_quantity_checks": {
            "USDT": {
                "delta_matches_bonus": False,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": False,
                "reward_counts": {"BONUS": 0, "REALTIME": 0},
            },
            "BTC": {
                "delta_matches_bonus": True,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": False,
                "reward_counts": {"BONUS": 0, "REALTIME": 1},
            },
        },
    }
    install_read_stubs(monkeypatch, migration, refs, current=current, history=history)
    from application.broker_reconciliation import diagnose_balance_flows as real_diagnose_balance_flows

    class EmptyHistoryClient:
        def _request_margin_api(self, _method, path, *, signed, data):
            assert signed is True
            if path in {"capital/deposit/hisrec", "capital/withdraw/history"}:
                return []
            return {"rows": [], "total": 0}

    monkeypatch.setattr(
        migration,
        "diagnose_balance_flows",
        lambda _client, **kwargs: real_diagnose_balance_flows(
            EmptyHistoryClient(), **kwargs
        ),
    )
    result = migration.diagnose_earn_forward(
        refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
    )

    btc = result["assets"]["BTC"]
    assert btc["residual_direction"] == "UNCHANGED"
    assert btc["residual_matches_bonus_records"] is False
    assert btc["classification"] == "residual_zero_after_realtime_counter"


def test_unknown_order_state_returns_restricted_diagnostic(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    refs["ledger_ref"].snapshot.value["order_submission"] = {"state": "UNKNOWN"}
    install_read_stubs(monkeypatch, migration, refs)
    result = migration.diagnose_earn_forward(
        refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
    )

    assert result["order_state_known"] is False
    assert result["diagnostic_restricted"] is True
    assert result["activation_allowed"] is False


def test_balance_difference_without_matching_history_stays_unexplained(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    history = {
        "history_complete_for_requested_surfaces": True,
        "history_counts": {"earn_rewards": 0},
        "reward_quantity_checks": {
            asset: {
                "delta_matches_bonus": False,
                "delta_matches_realtime": False,
                "delta_matches_visible_total": False,
                "reward_counts": {"BONUS": 0, "REALTIME": 0},
            }
            for asset in ("USDT", "BTC")
        },
    }
    install_read_stubs(monkeypatch, migration, refs, history=history)
    result = migration.diagnose_earn_forward(
        refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
    )

    assert result["assets"]["BTC"]["classification"] == "quantity_unexplained"
    assert result["assets"]["BTC"]["causal_reconciliation"] is False
    assert result["causal_reconciliation"] is False


def test_incomplete_or_duplicate_reward_history_is_rejected(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    history = {
        "history_complete_for_requested_surfaces": False,
        "reason_code": "balance_history_reward_validation_failed",
    }
    install_read_stubs(monkeypatch, migration, refs, history=history)
    with pytest.raises(migration.MigrationBlocked, match="earn_diagnosis_reward_history_invalid"):
        migration.diagnose_earn_forward(
            refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
        )


def test_account_scope_mismatch_is_rejected(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    observations = SimpleNamespace(
        account_scope={"account_uid": "wrong"}, recent_executions=(), open_orders=()
    )
    install_read_stubs(monkeypatch, migration, refs, observations=observations)
    monkeypatch.setattr(
        migration,
        "digest",
        lambda value: "b" * 64 if value == {"account_uid": "wrong"} else SCOPE,
    )
    with pytest.raises(migration.MigrationBlocked, match="earn_diagnosis_account_scope_mismatch"):
        migration.diagnose_earn_forward(
            refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
        )


def test_state_change_discards_diagnosis(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    def history(*_args, **_kwargs):
        refs["ledger_ref"].snapshot.value["changed"] = True
        return {
            "history_complete_for_requested_surfaces": True,
            "history_counts": {"earn_rewards": 0},
            "reward_quantity_checks": {
                asset: {
                    "delta_matches_bonus": False,
                    "delta_matches_realtime": False,
                    "delta_matches_visible_total": False,
                    "reward_counts": {"BONUS": 0, "REALTIME": 0},
                }
                for asset in ("USDT", "BTC")
            },
        }
    install_read_stubs(monkeypatch, migration, refs)
    monkeypatch.setattr(migration, "diagnose_balance_flows", history)
    with pytest.raises(migration.MigrationBlocked, match="earn_diagnosis_state_changed_during_read"):
        migration.diagnose_earn_forward(
            refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
        )


def test_diagnosis_output_contains_no_amounts_uids_or_raw_rows(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration

    refs = source()
    install_read_stubs(monkeypatch, migration, refs)
    result = migration.diagnose_earn_forward(
        refs, client=object(), expected={"account_scope_sha256": SCOPE}, now=NOW
    )
    output = json.dumps(result, sort_keys=True)
    assert "1.1" not in output
    assert "123" not in output
    assert "held" not in output
    assert "BTC001" not in output
    assert "cumulativeRealTimeRewards" not in output
