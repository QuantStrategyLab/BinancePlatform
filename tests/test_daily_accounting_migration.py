import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest


NOW = datetime(2026, 9, 11, 0, 5, tzinfo=timezone.utc)


def _ledger(**changes):
    value = {
        "last_reset_date": "2026-09-11",
        "daily_equity_base": 1000.0,
        "daily_trend_equity_base": 400.0,
        "daily_trend_pnl_basis": "trend_val",
        "is_circuit_broken": True,
        "last_balance_snapshot": {"USDT": 500.0, "BTC": 0.1, "ETH": 2.0},
        "order_submission": {"state": "TERMINAL"},
        "trend_action_history": {"ETHUSDT": {"action": "buy", "date": "20260910"}},
        "unknown_future_field": {"keep": True},
    }
    value.update(changes)
    return value


def _evidence(**changes):
    value = {
        "account_scope_sha256": "a" * 64,
        "balance_snapshot": {"USDT": 500.0, "BTC": 0.1, "BNB": 0.25, "ETH": 2.0},
        "prices": {"BTCUSDT": 100000.0, "BNBUSDT": 1000.0, "ETHUSDT": 2000.0},
        "open_order_count": 0,
        "recent_execution_count": 0,
        "history_counts": {"deposits": 0, "withdrawals": 0, "earn_rewards": 0},
        "history_complete": True,
        "broker_snapshot_sha256": "b" * 64,
        "activity_sha256": "c" * 64,
    }
    value.update(changes)
    return value


def _candidate(ledger=None, evidence=None, now=NOW):
    from scripts.migrate_daily_accounting_state import build_candidate

    return build_candidate(
        ledger=ledger or _ledger(),
        ledger_update_time="2026-09-11T00:04:00Z",
        control={"state": "RECONCILE_ONLY"},
        evidence=evidence or _evidence(),
        source_sha="f" * 40,
        observed_at=now,
    )


def test_same_day_zero_activity_preserves_bases_latch_and_unknown_fields():
    candidate = _candidate()
    patch = candidate["patch"]

    assert candidate["mode"] == "same_utc_day_zero_activity"
    assert patch == {
        "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
        "daily_trend_cash_flow_usdt": 0.0,
        "daily_trend_net_invested_usdt": 0.0,
        "daily_trend_risk_base_usdt": 400.0,
        "daily_trend_third_fee_usdt": 0.0,
    }
    assert candidate["preserved_circuit_breaker_latch"] is True
    assert "last_balance_snapshot" not in candidate["public_preview"]
    assert "account_scope_sha256" not in candidate["public_preview"]


def test_utc_rollover_uses_current_marks_but_preserves_latch():
    ledger = _ledger(last_reset_date="2026-09-10")
    candidate = _candidate(ledger=ledger)

    assert candidate["mode"] == "new_utc_day_zero_activity"
    assert candidate["patch"]["daily_equity_base"] == 14750.0
    assert candidate["patch"]["daily_trend_equity_base"] == 4000.0
    assert candidate["patch"]["last_reset_date"] == "2026-09-11"
    assert candidate["patch"]["daily_trend_risk_base_usdt"] == 4000.0
    assert "is_circuit_broken" not in candidate["patch"]


def test_utc_rollover_rejects_zero_price_for_nonzero_asset():
    from scripts.migrate_daily_accounting_state import MigrationBlocked

    with pytest.raises(MigrationBlocked, match="price_snapshot_incomplete"):
        _candidate(
            ledger=_ledger(last_reset_date="2026-09-10"),
            evidence=_evidence(
                prices={"BTCUSDT": 100000.0, "BNBUSDT": 0.0, "ETHUSDT": 2000.0}
            ),
        )


@pytest.mark.parametrize(
    "ledger,evidence,reason",
    [
        (
            _ledger(order_submission={"state": "SUBMISSION_UNKNOWN"}),
            _evidence(),
            "unsafe_order_state",
        ),
        (
            _ledger(order_submission={"state": "FILLED_ACCOUNTING_PENDING"}),
            _evidence(),
            "unsafe_order_state",
        ),
        (_ledger(), _evidence(open_order_count=1), "open_orders_present"),
        (
            _ledger(),
            _evidence(recent_execution_count=1),
            "current_day_activity_present",
        ),
        (
            _ledger(),
            _evidence(history_counts={"earn_rewards": 1}),
            "current_day_activity_present",
        ),
        (_ledger(), _evidence(history_complete=False), "activity_evidence_incomplete"),
        (
            _ledger(),
            _evidence(balance_snapshot={"USDT": 500.0, "BTC": 0.1, "ETH": 2.0}),
            "bnb_balance_missing",
        ),
        (
            _ledger(),
            _evidence(
                balance_snapshot={"USDT": 500.0, "BTC": 0.1, "BNB": 0.0, "ETH": 3.0}
            ),
            "same_day_balance_changed",
        ),
    ],
)
def test_unsafe_or_incomplete_inputs_fail_closed(ledger, evidence, reason):
    from scripts.migrate_daily_accounting_state import MigrationBlocked

    with pytest.raises(MigrationBlocked, match=reason):
        _candidate(ledger=ledger, evidence=evidence)


def test_candidate_is_redacted_and_tampering_or_expiry_is_rejected():
    from scripts.migrate_daily_accounting_state import validate_candidate

    candidate = _candidate()
    public = candidate["public_preview"]
    assert "last_balance_snapshot" not in public
    assert public["balance_asset_count"] == 4
    assert public["candidate_sha256"] == candidate["candidate_sha256"]

    validate_candidate(
        candidate,
        expected_digest=candidate["candidate_sha256"],
        now=NOW + timedelta(minutes=9),
    )
    tampered = copy.deepcopy(candidate)
    tampered["patch"]["daily_trend_risk_base_usdt"] = 0.0
    with pytest.raises(ValueError, match="candidate_digest_mismatch"):
        validate_candidate(
            tampered, expected_digest=candidate["candidate_sha256"], now=NOW
        )
    with pytest.raises(ValueError, match="candidate_expired"):
        validate_candidate(
            candidate,
            expected_digest=candidate["candidate_sha256"],
            now=NOW + timedelta(minutes=11),
        )


class Snapshot:
    def __init__(self, value, update_time="2026-09-11T00:04:00Z"):
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


class Transaction:
    def __init__(self):
        self.writes = []

    def update(self, ref, patch):
        self.writes.append((ref, copy.deepcopy(patch)))


def test_atomic_apply_updates_only_allowlisted_fields():
    from scripts.migrate_daily_accounting_state import compare_and_apply

    ledger = _ledger()
    candidate = _candidate(ledger=ledger)
    tx = Transaction()
    ledger_ref = Ref(Snapshot(ledger))
    owner_ref = Ref(Snapshot(None))
    control_ref = Ref(Snapshot({"state": "RECONCILE_ONLY"}))

    compare_and_apply(
        tx,
        ledger_ref=ledger_ref,
        owner_ref=owner_ref,
        control_ref=control_ref,
        candidate=candidate,
        balance_snapshot=_evidence()["balance_snapshot"],
    )

    assert tx.writes == [
        (
            ledger_ref,
            {
                **candidate["patch"],
                "last_balance_snapshot": _evidence()["balance_snapshot"],
            },
        )
    ]
    assert "is_circuit_broken" not in tx.writes[0][1]
    assert "order_submission" not in tx.writes[0][1]


def test_apply_readback_confirms_patch_and_preserved_fields():
    from scripts.migrate_daily_accounting_state import _verify_applied

    ledger = _ledger()
    candidate = _candidate(ledger=ledger)
    migrated = {
        **ledger,
        **candidate["patch"],
        "last_balance_snapshot": _evidence()["balance_snapshot"],
    }
    refs = {
        "ledger_ref": Ref(Snapshot(migrated, "2026-09-11T00:04:01Z")),
        "owner_ref": Ref(Snapshot(None)),
        "control_ref": Ref(Snapshot({"state": "RECONCILE_ONLY"})),
    }

    _verify_applied(
        refs, candidate=candidate, balance_snapshot=_evidence()["balance_snapshot"]
    )
    refs["ledger_ref"] = Ref(
        Snapshot({**migrated, "unknown_future_field": {"keep": False}})
    )
    with pytest.raises(RuntimeError, match="migration_readback_mismatch"):
        _verify_applied(
            refs, candidate=candidate, balance_snapshot=_evidence()["balance_snapshot"]
        )


@pytest.mark.parametrize("changed", ["owner", "ledger", "version", "control"])
def test_atomic_apply_rejects_owner_and_all_concurrent_changes(changed):
    from scripts.migrate_daily_accounting_state import compare_and_apply

    ledger = _ledger()
    candidate = _candidate(ledger=ledger)
    owner = None
    control = {"state": "RECONCILE_ONLY"}
    version = "2026-09-11T00:04:00Z"
    if changed == "owner":
        owner = {"owner_id": "active"}
    elif changed == "ledger":
        ledger = {**ledger, "unknown_future_field": {"keep": False}}
    elif changed == "version":
        version = "2026-09-11T00:04:01Z"
    else:
        control = {"state": "ACTIVE_LKG"}
    tx = Transaction()

    with pytest.raises(ValueError, match="migration_atomic_precondition_changed"):
        compare_and_apply(
            tx,
            ledger_ref=Ref(Snapshot(ledger, version)),
            owner_ref=Ref(Snapshot(owner)),
            control_ref=Ref(Snapshot(control)),
            candidate=candidate,
            balance_snapshot=_evidence()["balance_snapshot"],
        )
    assert tx.writes == []


def test_runtime_context_requires_disabled_main_reconcile_only(monkeypatch):
    from scripts.migrate_daily_accounting_state import require_runtime_context

    valid = {
        "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": "QuantStrategyLab/BinancePlatform/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false",
        "RECONCILE_ONLY": "true",
        "GITHUB_SHA": "f" * 40,
    }
    monkeypatch.setattr("scripts.migrate_daily_accounting_state.os.environ", valid)
    require_runtime_context()
    for key, value in (
        ("RUNTIME_TARGET_ENABLED", "true"),
        ("RECONCILE_ONLY", "false"),
        ("GITHUB_REF", "refs/heads/feature"),
    ):
        invalid = {**valid, key: value}
        monkeypatch.setattr(
            "scripts.migrate_daily_accounting_state.os.environ", invalid
        )
        with pytest.raises(
            ValueError, match="migration_requires_disabled_main_runtime"
        ):
            require_runtime_context()


def test_strict_balance_snapshot_sums_every_flexible_earn_row():
    from scripts.migrate_daily_accounting_state import _strict_balance_snapshot

    class Client:
        def get_simple_earn_flexible_product_position(self, *, asset, current, size):
            return {
                "rows": [
                    {"asset": asset, "totalAmount": "0.2"},
                    {"asset": asset, "totalAmount": "0.3"},
                ],
                "total": 2,
            }

    result = _strict_balance_snapshot(
        Client(),
        [{"asset": "BNB", "free": "1", "locked": "0.5"}],
        {"BNB"},
    )

    assert result == {"BNB": 2.0}


def test_strict_balance_snapshot_rejects_missing_spot_or_earn_evidence():
    from scripts.migrate_daily_accounting_state import (
        MigrationBlocked,
        _strict_balance_snapshot,
    )

    class Client:
        def get_simple_earn_flexible_product_position(self, *, asset, current, size):
            return {"rows": None, "total": 1}

    with pytest.raises(MigrationBlocked, match="spot_balance_missing"):
        _strict_balance_snapshot(Client(), [], {"BNB"})
    with pytest.raises(MigrationBlocked, match="earn_balance_rows_invalid"):
        _strict_balance_snapshot(
            Client(), [{"asset": "BNB", "free": "1", "locked": "0"}], {"BNB"}
        )


def test_main_marks_successful_single_apply_with_readback_timeout_uncertain(
    monkeypatch, tmp_path, capsys
):
    import google.cloud.firestore
    from scripts import migrate_daily_accounting_state as migration

    ledger = _ledger()
    actual_now = datetime.now(timezone.utc)
    candidate = _candidate(ledger=ledger, now=actual_now)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    class StatefulRef(Ref):
        def __init__(self, snapshot, *, fail_readback=False):
            super().__init__(snapshot)
            self.calls = 0
            self.fail_readback = fail_readback

        def get(self, **kwargs):
            self.calls += 1
            if self.fail_readback and self.calls == 3:
                raise TimeoutError("synthetic private provider text")
            return super().get(**kwargs)

    owner_ref = StatefulRef(Snapshot(None), fail_readback=True)
    refs = {
        "ledger_ref": StatefulRef(Snapshot(ledger)),
        "owner_ref": owner_ref,
        "control_ref": StatefulRef(Snapshot({"state": "RECONCILE_ONLY"})),
    }
    transaction = Transaction()

    class FirestoreClient:
        transaction_calls = 0

        def transaction(self, *, max_attempts):
            assert max_attempts == 1
            self.transaction_calls += 1
            return transaction

    firestore_client = FirestoreClient()
    monkeypatch.setattr(migration, "PREVIEW_PATH", candidate_path)
    monkeypatch.setattr(migration, "require_runtime_context", lambda: None)
    monkeypatch.setattr(
        migration,
        "resolve_runtime_target_from_env",
        lambda **kwargs: SimpleNamespace(
            live_continuity=SimpleNamespace(state="RECONCILE_ONLY")
        ),
    )
    monkeypatch.setattr(
        migration, "_expected_digests", lambda: {"account_scope_sha256": "a" * 64}
    )
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    monkeypatch.setattr(migration, "connect_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        migration,
        "_collect_evidence",
        lambda *args, **kwargs: {
            **_evidence(),
            "utc_date": actual_now.date().isoformat(),
        },
    )
    monkeypatch.setattr(migration, "get_firestore_client", lambda: firestore_client)
    monkeypatch.setattr(google.cloud.firestore, "transactional", lambda fn: fn, raising=False)
    monkeypatch.setattr(
        migration.os,
        "environ",
        {
            "GITHUB_SHA": "f" * 40,
            "BINANCE_API_KEY": "synthetic",
            "BINANCE_API_SECRET": "synthetic",
        },
    )

    assert (
        migration.main(["apply", "--expected-digest", candidate["candidate_sha256"]])
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "uncertain",
        "stage": "accounting_migration_apply",
        "reason_code": "migration_readback_mismatch",
        "no_retry": True,
        "no_order": True,
    }
    assert firestore_client.transaction_calls == 1
    assert len(transaction.writes) == 1


@pytest.mark.parametrize("error, expected_reason", [
    ("guard", "same_day_balance_changed"),
    ("provider", "migration_blocked"),
])
def test_preview_reports_internal_guard_without_provider_details(monkeypatch, capsys, error, expected_reason):
    from scripts import migrate_daily_accounting_state as migration

    def fail(*args, **kwargs):
        if error == "guard":
            raise migration.MigrationBlocked("same_day_balance_changed")
        raise TimeoutError("synthetic private provider detail")

    monkeypatch.setattr(migration, "run", fail)
    assert migration.main(["preview"]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["reason_code"] == expected_reason
    assert "private provider" not in output


def test_inspect_control_reads_metadata_without_broker_or_mutation(monkeypatch, capsys):
    from scripts import migrate_daily_accounting_state as migration
    control = {"state": "ACTIVE_LKG", "recovery_id": "binance-12345-1",
               "source": {"run": {"id": 12345, "head_sha": "a" * 40}},
               "confirmation": {"private": "sensitive confirmation"},
               "private": "sensitive account data"}
    refs = {"control_ref": Ref(Snapshot(control)),
            "ledger_ref": Ref(Snapshot(_ledger())), "owner_ref": Ref(Snapshot(None))}
    monkeypatch.setattr(migration, "require_runtime_context", lambda: None)
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not invoke broker or migration")
    monkeypatch.setattr(migration, "connect_client", forbidden)
    monkeypatch.setattr(migration, "_read_source", forbidden)
    monkeypatch.setattr(migration, "resolve_runtime_target_from_env", forbidden)
    assert migration.main(["inspect"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["state"] == "ACTIVE_LKG"
    assert result["source_run"] == {"id": 12345, "head_sha": "a" * 40}
    assert result["owner_exists"] is False
    assert result["no_order"] is True
    assert result["write_performed"] is False
    assert "sensitive" not in output


def test_inspect_control_does_not_export_arbitrary_state(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration
    refs = {"control_ref": Ref(Snapshot({"state": "private payload", "source": []})),
            "ledger_ref": Ref(Snapshot(None)), "owner_ref": Ref(Snapshot(None))}
    result = migration.inspect_control(refs)
    assert result["state"] == "INVALID"
    assert result["source_run"] is None
    assert "private payload" not in json.dumps(result)


def _audit_setup(monkeypatch):
    from scripts import migrate_daily_accounting_state as migration
    expected = {"account_scope_sha256": migration.digest({"account_uid": "synthetic"})}
    baseline = {**expected, "positions_sha256": "b" * 64, "cash_sha256": "c" * 64}
    control = {"state": "RECONCILE_ONLY", "candidate": {"expected_digests": baseline},
               "source": {"original_evidence": {**baseline, "observed_at": "2026-09-08T18:18:20Z"},
                          "frozen_expected_sha256": migration.digest(expected)}}
    monkeypatch.setattr(migration, "QUIESCE_CONTROL_SHA256", migration.digest({**control, "state": "ACTIVE_LKG"}))
    refs = {"control_ref": Ref(Snapshot(control)), "ledger_ref": Ref(Snapshot(_ledger())),
            "owner_ref": Ref(Snapshot(None))}
    monkeypatch.setenv("BINANCE_RECONCILIATION_SYMBOLS", "ETHUSDT")
    balances = _evidence()["balance_snapshot"]
    class Client:
        def get_account(self):
            return {"uid": "synthetic", "balances": [{"asset": a, "free": str(v), "locked": "0"} for a, v in balances.items()]}
        def get_simple_earn_flexible_product_position(self, **kwargs):
            return {"rows": [], "total": 0}
    monkeypatch.setattr(migration, "collect_read_only_reconciliation_observations", lambda *a, **kw: SimpleNamespace(
        account_scope={"account_uid": "synthetic"}, positions=(), open_orders=(), recent_executions=()))
    calls = []
    def history(*args, **kwargs):
        calls.append(kwargs)
        return {"history_complete_for_requested_surfaces": True, "history_counts": {"earn_rewards": 3},
                "spot_bonus_reconciliation": {"historical_balance_hashes_match": True}}
    monkeypatch.setattr(migration, "diagnose_balance_flows", history)
    return migration, refs, Client(), expected, balances, calls


def test_audit_uses_recovered_window_and_distinguishes_missing_from_mismatch(monkeypatch):
    migration, refs, client, expected, balances, calls = _audit_setup(monkeypatch)
    balances["ETH"] = 2.5
    result = migration.audit_ledger(refs, client=client, expected=expected, now=NOW)
    assert result["ledger_balance_comparison"] == {"BTC": "MATCH", "BNB": "MISSING_IN_LEDGER", "ETH": "MISMATCH", "USDT": "MATCH"}
    assert calls[0]["start"] == datetime(2026, 9, 8, 18, 18, 20, tzinfo=timezone.utc)
    assert calls[0]["expected_digests"]["positions_sha256"] == "b" * 64
    assert result["recovered_spot_history"]["spot_bonus_reconciliation"]["historical_balance_hashes_match"] is True
    assert result["complete_balance_reconciliation"] is False
    assert result["write_performed"] is False and result["no_order"] is True
    assert "500.0" not in json.dumps(result) and "synthetic" not in json.dumps(result)
    assert refs["ledger_ref"].snapshot.value == _ledger()


@pytest.mark.parametrize("change", ["control", "owner", "identity", "window", "ledger_during_read", "spot_during_read", "earn_during_read", "incomplete_history"])
def test_audit_rejects_untrusted_incomplete_or_changing_evidence(monkeypatch, change):
    migration, refs, client, expected, balances, calls = _audit_setup(monkeypatch)
    if change == "control": refs["control_ref"].snapshot.value["private"] = True
    if change == "owner": refs["owner_ref"] = Ref(Snapshot({"owner": "busy"}))
    if change == "identity": expected["account_scope_sha256"] = "d" * 64
    if change == "window":
        # A known source still cannot extend the finite history window.
        control = refs["control_ref"].snapshot.value
        control["source"]["original_evidence"]["observed_at"] = "2026-09-01T00:00:00Z"
        monkeypatch.setattr(migration, "QUIESCE_CONTROL_SHA256", migration.digest({**control, "state": "ACTIVE_LKG"}))
    original = migration.diagnose_balance_flows
    def history(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "ledger_during_read": refs["ledger_ref"].snapshot.value["daily_equity_base"] += 1
        if change == "spot_during_read": balances["USDT"] += 1
        if change == "earn_during_read":
            client.get_simple_earn_flexible_product_position = lambda **kw: {"rows": [{"asset": kw["asset"], "totalAmount": "1"}], "total": 1}
        if change == "incomplete_history": result["history_complete_for_requested_surfaces"] = False
        return result
    monkeypatch.setattr(migration, "diagnose_balance_flows", history)
    with pytest.raises(migration.MigrationBlocked):
        migration.audit_ledger(refs, client=client, expected=expected, now=NOW)


def test_audit_runtime_never_enters_candidate_or_write_path(monkeypatch, capsys):
    migration, refs, client, expected, _, _ = _audit_setup(monkeypatch)
    monkeypatch.setattr(migration, "require_runtime_context", lambda: None)
    monkeypatch.setattr(migration, "resolve_runtime_target_from_env", lambda **kw: SimpleNamespace(
        live_continuity=SimpleNamespace(state="RECONCILE_ONLY")))
    monkeypatch.setattr(migration, "_expected_digests", lambda: expected)
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    monkeypatch.setattr(migration, "connect_client", lambda *a, **kw: client)
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic")
    monkeypatch.setattr(migration, "datetime", SimpleNamespace(
        now=lambda *_: NOW, fromisoformat=datetime.fromisoformat, strptime=datetime.strptime))
    def forbidden(*a, **kw): raise AssertionError("audit must not prepare or apply a migration")
    monkeypatch.setattr(migration, "_collect_evidence", forbidden)
    monkeypatch.setattr(migration, "build_candidate", forbidden)
    monkeypatch.setattr(migration, "get_firestore_client", forbidden)
    assert migration.main(["audit"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "accounting_ledger_audit"


@pytest.mark.parametrize("changed", [None, "owner", "control", "version", "missing_ledger"])
def test_quiesce_only_updates_approved_control_state(monkeypatch, changed):
    from scripts import migrate_daily_accounting_state as migration
    control = {"state": "ACTIVE_LKG", "history": {"preserve": True}}
    monkeypatch.setattr(migration, "QUIESCE_CONTROL_SHA256", migration._canonical_sha(control))
    snapshot = Snapshot(copy.deepcopy(control), migration.QUIESCE_CONTROL_UPDATE_TIME)
    refs = {"control_ref": Ref(snapshot), "owner_ref": Ref(Snapshot(None)),
            "ledger_ref": Ref(Snapshot(_ledger()))}
    if changed == "owner": refs["owner_ref"] = Ref(Snapshot({"owner": "busy"}))
    if changed == "control": snapshot.value["history"]["preserve"] = False
    if changed == "version": snapshot.update_time = "2026-09-11T00:00:00Z"
    if changed == "missing_ledger": refs["ledger_ref"] = Ref(Snapshot(None))
    tx = Transaction()
    if changed:
        with pytest.raises(migration.MigrationBlocked, match="control_quiesce_precondition_changed"):
            migration._quiesce_control_transaction(tx, refs)
        assert tx.writes == []
    else:
        expected, ledger_sha = migration._quiesce_control_transaction(tx, refs)
        assert tx.writes == [(refs["control_ref"], {"state": "RECONCILE_ONLY"})]
        assert expected == migration._canonical_sha({**control, "state": "RECONCILE_ONLY"})
        assert ledger_sha == migration._canonical_sha(_ledger())


@pytest.mark.parametrize("readback_timeout", [False, True])
def test_quiesce_has_single_transaction_and_uncertain_readback(monkeypatch, capsys, readback_timeout):
    from google.cloud import firestore
    from scripts import migrate_daily_accounting_state as migration
    control = {"state": "ACTIVE_LKG", "history": {"preserve": True}}
    monkeypatch.setattr(migration, "QUIESCE_CONTROL_SHA256", migration._canonical_sha(control))
    class ReadbackRef(Ref):
        calls = 0
        def get(self, **kwargs):
            self.calls += 1
            if readback_timeout and self.calls > 1:
                raise TimeoutError("synthetic sensitive provider error")
            return super().get(**kwargs)
    refs = {"control_ref": ReadbackRef(Snapshot(control, migration.QUIESCE_CONTROL_UPDATE_TIME)),
            "owner_ref": Ref(Snapshot(None)), "ledger_ref": Ref(Snapshot(_ledger()))}
    class CommittingTransaction(Transaction):
        def update(self, ref, patch):
            super().update(ref, patch)
            ref.snapshot.value.update(patch)
    tx = CommittingTransaction()
    calls = []
    def transaction(*, max_attempts):
        calls.append(max_attempts); return tx
    monkeypatch.setattr(migration, "require_runtime_context", lambda: None)
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    monkeypatch.setattr(migration, "get_firestore_client", lambda: SimpleNamespace(transaction=transaction))
    monkeypatch.setattr(firestore, "transactional", lambda fn: fn, raising=False)
    def forbidden(*args, **kwargs): raise AssertionError("no broker or migration allowed")
    monkeypatch.setattr(migration, "connect_client", forbidden)
    monkeypatch.setattr(migration, "_read_source", forbidden)
    rc = migration.main(["quiesce"])
    output = capsys.readouterr().out; result = json.loads(output)
    assert calls == [1] and len(tx.writes) == 1
    assert "sensitive" not in output
    if readback_timeout:
        assert rc == 2 and result["status"] == "uncertain" and result["no_retry"] is True
    else:
        assert rc == 0 and result["state"] == "RECONCILE_ONLY"
        assert result["ledger_unchanged"] is True
    assert refs["control_ref"].snapshot.value["history"] == {"preserve": True}
