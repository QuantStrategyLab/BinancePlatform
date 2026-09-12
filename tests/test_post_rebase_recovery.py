"""Offline post-rebase recovery source and atomic-storage regressions."""

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256 as digest,
)
from tests.test_broker_reconciliation import _target


NOW = datetime(2026, 9, 11, 14, 5, tzinfo=timezone.utc)
OPENED_AT = NOW - timedelta(minutes=15)


def _old_ledger():
    return {
        "daily_trend_pnl_basis": "trend_val",
        "daily_trend_cash_flow_usdt": 12.0,
        "daily_trend_net_invested_usdt": 34.0,
        "daily_trend_risk_base_usdt": 56.0,
        "daily_trend_third_fee_usdt": 0.1,
        "last_balance_snapshot": {"USDT": 90.0, "BTC": 0.09},
        "daily_equity_base": 900.0,
        "daily_trend_equity_base": 300.0,
        "last_reset_date": "2026-08-11",
        "is_circuit_broken": True,
        "order_submission": {"state": "TERMINAL"},
        "unknown_future_field": {"keep": True},
    }


def _marker():
    return {
        "archive_document": "MULTI_ASSET_STATE__before_rebase_34601984051",
        "started_at": OPENED_AT.isoformat(),
        "opening_balance_observed_at": OPENED_AT.isoformat(),
        "historical_difference_unresolved": True,
        "approved_proposal_run_id": "34601984051",
    }


def _material():
    old = _old_ledger()
    control = {
        "state": "RECONCILE_ONLY",
        "source": {"original_evidence": {"account_scope_sha256": digest({"account_uid": "123"})}},
    }
    opening = {"BTC": 0.1, "USDT": 100.0}
    current = {
        **old,
        "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
        "daily_trend_cash_flow_usdt": 0.0,
        "daily_trend_net_invested_usdt": 0.0,
        "daily_trend_risk_base_usdt": 0.1,
        "daily_trend_third_fee_usdt": 0.0,
        "last_balance_snapshot": opening,
        "daily_equity_base": 10100.0,
        "daily_trend_equity_base": 0.0,
        "last_reset_date": "2026-09-11",
        "accounting_rebase": _marker(),
    }
    archive = {
        "ledger": copy.deepcopy(old),
        "recovery_control": copy.deepcopy(control),
        "ledger_update_time": "2026-09-11T13:00:00Z",
        **_marker(),
        "new_ledger_sha256": digest(current),
        "valuation_price_source": "binance_get_avg_price_estimate",
    }
    return current, archive, control, opening


def _legacy_expected():
    return {
        "account_scope_sha256": digest({"account_uid": "123"}),
        "positions_sha256": "1" * 64,
        "cash_sha256": "2" * 64,
        "open_orders_sha256": "3" * 64,
        "recent_executions_sha256": "4" * 64,
        "local_execution_ledger_sha256": "5" * 64,
    }


class Client:
    def __init__(self):
        self.balances = [
            {"asset": "BTC", "free": "0.1", "locked": "0"},
            {"asset": "USDT", "free": "100", "locked": "0"},
        ]
        self.earn = {"BTC": [], "USDT": []}
        self.open_orders = []
        self.trades = []
        self.history_counts = {}
        self.history_incomplete = False
        self.account_reads = 0
        self.mutate_after_first_read = None

    def get_account(self):
        self.account_reads += 1
        if self.account_reads > 1 and self.mutate_after_first_read is not None:
            self.mutate_after_first_read(self)
        return {"uid": "123", "balances": copy.deepcopy(self.balances)}

    def get_open_orders(self):
        return copy.deepcopy(self.open_orders)

    def get_my_trades(self, **_kwargs):
        return copy.deepcopy(self.trades)

    def get_simple_earn_flexible_product_position(self, *, asset, current, size):
        assert current == 1 and size == 100
        rows = copy.deepcopy(self.earn.get(asset, []))
        return {"total": len(rows), "rows": rows}

    def _request_margin_api(self, method, path, **kwargs):
        assert method == "get" and kwargs["signed"] is True
        name = {
            "capital/deposit/hisrec": "deposits",
            "capital/withdraw/history": "withdrawals",
            "simple-earn/flexible/history/subscriptionRecord": "earn_subscriptions",
            "simple-earn/flexible/history/redemptionRecord": "earn_redemptions",
            "simple-earn/flexible/history/rewardsRecord": "earn_rewards",
        }.get(path)
        if path == "asset/transfer":
            name = "transfer_" + kwargs["data"]["type"].lower()
        count = self.history_counts.get(name, 0)
        if self.history_incomplete and name == "withdrawals":
            return {"total": 1, "rows": []}
        if path in {"capital/deposit/hisrec", "capital/withdraw/history"}:
            return [{} for _ in range(count)]
        if name == "earn_rewards" and count:
            return {
                "total": count,
                "rows": [
                    {
                        "asset": "USDT",
                        "type": "BONUS",
                        "rewards": "0",
                        "time": int((NOW - timedelta(minutes=1)).timestamp() * 1000),
                    }
                    for _ in range(count)
                ],
            }
        return {"total": count, "rows": [{} for _ in range(count)]}


def _configure_anchors(monkeypatch, current, archive, control, opening):
    import application.rebased_recovery as rebased

    monkeypatch.setattr(rebased, "APPROVED_OLD_LEDGER_SHA256", digest(archive["ledger"]))
    monkeypatch.setattr(rebased, "APPROVED_OLD_CONTROL_SHA256", digest(control))
    monkeypatch.setattr(rebased, "APPROVED_OPENING_BALANCES_SHA256", digest(opening))
    assert archive["new_ledger_sha256"] == digest(current)


def _collect(monkeypatch, *, client=None, current=None, archive=None, now=NOW, observe_only_non_managed_spot=False):
    from application.rebased_recovery import collect_post_rebase_source

    base_current, base_archive, control, opening = _material()
    current = copy.deepcopy(current if current is not None else base_current)
    archive = copy.deepcopy(archive if archive is not None else base_archive)
    _configure_anchors(monkeypatch, base_current, base_archive, control, opening)
    return collect_post_rebase_source(
        client=client or Client(),
        runtime_target=_target(),
        legacy_expected=_legacy_expected(),
        ledger=current,
        archive=archive,
        symbols=("BTCUSDT",),
        source_run={"id": 200, "head_sha": "b" * 40, "head_branch": "main", "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34606795875, "head_sha": "ed7ee6e96cea0addb292f3f45338652095d0da58", "head_branch": "main", "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        now=now,
        observe_only_non_managed_spot=observe_only_non_managed_spot,
    )


def test_post_rebase_source_enrolls_exact_opening_without_raw_balances(monkeypatch):
    from application.rebased_recovery import SOURCE_KIND, validate_post_rebase_source

    package = _collect(monkeypatch)
    candidate = validate_post_rebase_source(
        package,
        runtime_target=_target(),
        legacy_expected=_legacy_expected(),
        now=NOW,
    )

    assert candidate.local_execution_ledger_sha256 == _material()[1]["new_ledger_sha256"]
    assert package["source"]["kind"] == SOURCE_KIND
    assert package["source"]["historical_difference_unresolved"] is True
    assert package["source"]["quantity_precision"] == 8
    assert package["source"]["proof"]["quantity_double_read_match"] is True
    assert package["source"]["proof"]["earn_rewards_observed"] == 0
    serialized = json.dumps(package, sort_keys=True)
    for private_name in ("balances", "free", "locked", "totalAmount", "account_uid"):
        assert private_name not in serialized


def test_real_rebase_producer_archive_and_ledger_feed_post_rebase_consumer(monkeypatch):
    from application import rebased_recovery as rebased
    from scripts import migrate_daily_accounting_state as migration
    from tests.test_daily_accounting_migration import Ref as MigrationRef
    from tests.test_daily_accounting_migration import Snapshot as MigrationSnapshot
    from tests.test_daily_accounting_migration import _evidence, _ledger
    from tests.test_approved_accounting_rebase import ArchiveTransaction

    ledger = _ledger(last_reset_date="2026-08-11")
    account_scope = digest({"account_uid": "123"})
    control = {
        "state": "RECONCILE_ONLY",
        "source": {"original_evidence": {"account_scope_sha256": account_scope}},
    }
    evidence = _evidence(history_counts={"earn_rewards": 0})
    refs = {
        "ledger_ref": MigrationRef(MigrationSnapshot(ledger)),
        "control_ref": MigrationRef(MigrationSnapshot(control)),
        "owner_ref": MigrationRef(MigrationSnapshot(None)),
    }
    archive_ref = MigrationRef(MigrationSnapshot(None))
    monkeypatch.setattr(migration, "APPROVED_REBASE_LEDGER_SHA256", digest(ledger))
    monkeypatch.setattr(migration, "APPROVED_REBASE_CONTROL_SHA256", digest(control))
    monkeypatch.setattr(migration, "APPROVED_REBASE_BALANCES_SHA256", digest(evidence["balance_snapshot"]))
    proposal = migration.build_rebase_proposal(ledger=ledger, evidence=evidence, observed_at=OPENED_AT)
    transaction = ArchiveTransaction()
    migration._rebase_transaction(
        transaction,
        refs=refs,
        archive_ref=archive_ref,
        ledger=ledger,
        control=control,
        ledger_update_time=refs["ledger_ref"].snapshot.update_time,
        proposed_fields=proposal["proposed_fields"],
        observed_at=OPENED_AT,
        started_at=OPENED_AT,
    )
    archive = transaction.creates[0][1]
    current = {**ledger, **transaction.writes[0][1]}
    monkeypatch.setattr(rebased, "APPROVED_OLD_LEDGER_SHA256", digest(ledger))
    monkeypatch.setattr(rebased, "APPROVED_OLD_CONTROL_SHA256", digest(control))
    monkeypatch.setattr(rebased, "APPROVED_OPENING_BALANCES_SHA256", digest(evidence["balance_snapshot"]))
    client = Client()
    client.balances = [
        {"asset": asset, "free": str(quantity), "locked": "0"}
        for asset, quantity in evidence["balance_snapshot"].items()
    ]
    client.earn = {asset: [] for asset in evidence["balance_snapshot"]}

    package = rebased.collect_post_rebase_source(
        client=client,
        runtime_target=_target(),
        legacy_expected={**_legacy_expected(), "account_scope_sha256": account_scope},
        ledger=current,
        archive=archive,
        symbols=("BTCUSDT", "BNBUSDT", "ETHUSDT"),
        source_run={"id": 200, "head_sha": "b" * 40, "head_branch": "main", "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34606795875, "head_sha": "ed7ee6e96cea0addb292f3f45338652095d0da58", "head_branch": "main", "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        now=NOW,
    )

    assert package["source"]["new_ledger_sha256"] == archive["new_ledger_sha256"]


def test_post_rebase_anchor_constants_are_imported_from_rebase_producer():
    from application import rebased_recovery as rebased
    from scripts import migrate_daily_accounting_state as migration

    assert rebased.APPROVED_OLD_LEDGER_SHA256 == migration.APPROVED_REBASE_LEDGER_SHA256
    assert rebased.APPROVED_OLD_CONTROL_SHA256 == migration.APPROVED_REBASE_CONTROL_SHA256
    assert rebased.APPROVED_OPENING_BALANCES_SHA256 == migration.APPROVED_REBASE_BALANCES_SHA256
    assert rebased.ARCHIVE_DOCUMENT == migration.REBASE_ARCHIVE_DOCUMENT


@pytest.mark.parametrize(
    "change,reason",
    [
        ("old_ledger", "post_rebase_archive_invalid"),
        ("old_control", "post_rebase_archive_invalid"),
        ("new_ledger", "post_rebase_ledger_invalid"),
        ("marker", "post_rebase_ledger_invalid"),
        ("history_flag", "post_rebase_archive_invalid"),
        ("unfinished_order", "post_rebase_order_state_unsafe"),
    ],
)
def test_post_rebase_source_rejects_tampered_private_material(monkeypatch, change, reason):
    current, archive, control, opening = _material()
    _configure_anchors(monkeypatch, current, archive, control, opening)
    if change == "old_ledger":
        archive["ledger"]["unknown_future_field"] = {"keep": False}
    elif change == "old_control":
        archive["recovery_control"]["state"] = "ACTIVE_LKG"
    elif change == "new_ledger":
        current["unknown_future_field"] = {"keep": False}
    elif change == "marker":
        current["accounting_rebase"]["approved_proposal_run_id"] = "other"
        archive["new_ledger_sha256"] = digest(current)
    elif change == "history_flag":
        archive["historical_difference_unresolved"] = False
    else:
        current["order_submission"] = {"state": "SUBMISSION_UNKNOWN"}
        archive["new_ledger_sha256"] = digest(current)
    with pytest.raises(ValueError, match=reason):
        _collect(monkeypatch, current=current, archive=archive)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("quantity", "post_rebase_quantity_mismatch"),
        ("negative_free", "post_rebase_balance_amount_invalid"),
        ("negative_locked", "post_rebase_balance_amount_invalid"),
        ("locked", "post_rebase_locked_balance_present"),
        ("negative_earn", "post_rebase_balance_amount_invalid"),
        ("nonfinite_earn", "post_rebase_balance_amount_invalid"),
        ("unknown_nonzero_spot", "post_rebase_unknown_spot_balance"),
        ("changed_second_read", "post_rebase_quantity_changed_during_read"),
    ],
)
def test_post_rebase_source_rejects_unsafe_or_changed_quantities(monkeypatch, change, reason):
    client = Client()
    if change == "quantity":
        client.balances[0]["free"] = "0.2"
    elif change == "negative_free":
        client.balances[0]["free"] = "-0.1"
    elif change == "negative_locked":
        client.balances[0]["locked"] = "-0.1"
    elif change == "locked":
        client.balances[0]["free"], client.balances[0]["locked"] = "0", "0.1"
    elif change == "negative_earn":
        client.earn["BTC"] = [{"asset": "BTC", "totalAmount": "-0.01"}]
    elif change == "nonfinite_earn":
        client.earn["BTC"] = [{"asset": "BTC", "totalAmount": "NaN"}]
    elif change == "unknown_nonzero_spot":
        client.balances.append({"asset": "OTHER", "free": "1", "locked": "0"})
    else:
        client.mutate_after_first_read = lambda value: value.balances.__setitem__(0, {"asset": "BTC", "free": "0.2", "locked": "0"})
    with pytest.raises(ValueError, match=reason):
        _collect(monkeypatch, client=client)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("open_order", "post_rebase_open_orders_present"),
        ("trade", "post_rebase_recent_executions_present"),
        ("deposit", "post_rebase_non_reward_activity_present"),
        ("incomplete", "post_rebase_history_incomplete"),
        ("expired", "post_rebase_history_window_invalid"),
    ],
)
def test_post_rebase_source_requires_complete_zero_activity_since_opening(monkeypatch, change, reason):
    client = Client()
    now = NOW
    if change == "open_order":
        client.open_orders = [{"orderId": 1, "symbol": "BTCUSDT", "status": "NEW", "side": "BUY", "type": "LIMIT", "origQty": "1", "executedQty": "0", "updateTime": 1}]
    elif change == "trade":
        client.trades = [{"id": 1, "orderId": 1, "symbol": "BTCUSDT", "qty": "1", "price": "1", "commission": "0", "commissionAsset": "BTC", "time": 1, "isBuyer": True}]
    elif change == "deposit":
        client.history_counts["deposits"] = 1
    elif change == "incomplete":
        client.history_incomplete = True
    else:
        now = OPENED_AT + timedelta(days=7, seconds=1)
    with pytest.raises(ValueError, match=reason):
        _collect(monkeypatch, client=client, now=now)


def test_post_rebase_source_allows_reward_rows_without_using_them_as_an_explanation(monkeypatch):
    client = Client()
    client.history_counts["earn_rewards"] = 1
    package = _collect(monkeypatch, client=client)
    assert package["source"]["proof"]["earn_rewards_observed"] == 1
    assert "spot_bonus_reconciliation" not in package["source"]["proof"]


@pytest.mark.parametrize("field,value", [("id", 34606795876), ("head_sha", "f" * 40)])
def test_stored_post_rebase_source_binds_exact_migration_run(monkeypatch, field, value):
    from application.rebased_recovery import validate_post_rebase_source

    package = _collect(monkeypatch)
    package["source"]["migration_run"][field] = value
    with pytest.raises(ValueError, match="post_rebase_source_binding_mismatch"):
        validate_post_rebase_source(
            package,
            runtime_target=_target(),
            legacy_expected=_legacy_expected(),
            now=NOW,
        )


class Snapshot:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.exists = value is not None

    def to_dict(self):
        return copy.deepcopy(self.value)


class Ref:
    def __init__(self, value):
        self.snapshot = Snapshot(value)

    def get(self, **_kwargs):
        return self.snapshot


def test_post_rebase_atomic_control_cas_binds_archive_owner_ledger_and_control():
    from scripts.binance_recovery_controller import compare_and_set_post_rebase_control

    ledger, archive, control, _ = _material()
    refs = {
        "owner_ref": Ref(None),
        "ledger_ref": Ref(ledger),
        "control_ref": Ref(control),
        "archive_ref": Ref(archive),
    }
    writes = []
    tx = SimpleNamespace(set=lambda *args: writes.append(args))
    next_value = {"state": "ACTIVE_LKG"}
    compare_and_set_post_rebase_control(
        tx,
        refs=refs,
        previous=control,
        next_value=next_value,
        ledger_sha256=digest(ledger),
        archive_sha256=digest(archive),
    )
    assert writes == [(refs["control_ref"], next_value)]

    for changed in ("owner_ref", "ledger_ref", "control_ref", "archive_ref"):
        broken = dict(refs)
        if changed == "owner_ref":
            broken[changed] = Ref({"owner": "active"})
        elif changed == "ledger_ref":
            broken[changed] = Ref({**ledger, "tampered": True})
        elif changed == "control_ref":
            broken[changed] = Ref({"state": "ACTIVE_LKG"})
        else:
            broken[changed] = Ref({**archive, "tampered": True})
        no_writes = SimpleNamespace(set=lambda *_: pytest.fail("unexpected write"))
        with pytest.raises(ValueError, match="post_rebase_atomic_precondition_changed"):
            compare_and_set_post_rebase_control(
                no_writes,
                refs=broken,
                previous=control,
                next_value=next_value,
                ledger_sha256=digest(ledger),
                archive_sha256=digest(archive),
            )


@pytest.mark.parametrize("outcome", ["success", "commit_timeout", "readback_mismatch"])
def test_post_rebase_control_write_is_single_attempt_and_read_back(monkeypatch, outcome):
    from scripts import binance_recovery_controller as controller

    ledger, archive, control, _ = _material()
    refs = {
        "owner_ref": Ref(None),
        "ledger_ref": Ref(ledger),
        "control_ref": Ref(control),
        "archive_ref": Ref(archive),
    }
    next_value = {"state": "ACTIVE_LKG"}
    attempts = []

    class Tx:
        def set(self, ref, value):
            ref.snapshot = Snapshot(value)

    def transaction(*, max_attempts):
        attempts.append(max_attempts)
        return Tx()

    def transactional(fn):
        def execute(tx):
            result = fn(tx)
            if outcome == "commit_timeout":
                raise TimeoutError("commit may already be durable")
            if outcome == "readback_mismatch":
                refs["archive_ref"].snapshot.value["new_ledger_sha256"] = "f" * 64
            return result
        return execute

    monkeypatch.setattr(controller.firestore, "transactional", transactional, raising=False)
    monkeypatch.setattr(
        controller,
        "_post_rebase_transaction",
        lambda _db: transaction(max_attempts=1),
    )
    db = SimpleNamespace(transaction=transaction)
    if outcome == "success":
        controller.save_post_rebase_control(
            db,
            refs,
            previous=control,
            next_value=next_value,
            ledger_sha256=digest(ledger),
            archive_sha256=digest(archive),
        )
        assert refs["control_ref"].snapshot.value == next_value
    else:
        with pytest.raises(controller.RecoveryWriteUncertain):
            controller.save_post_rebase_control(
                db,
                refs,
                previous=control,
                next_value=next_value,
                ledger_sha256=digest(ledger),
                archive_sha256=digest(archive),
            )
        if outcome == "commit_timeout":
            assert refs["control_ref"].snapshot.value == next_value
    assert attempts == [1]


@pytest.mark.parametrize(
    "outcome", ["success", "response_lost_after_commit", "control_readback_mismatch"]
)
def test_post_rebase_control_uses_one_real_sdk_commit_rpc_and_keeps_transaction_id(
    monkeypatch, outcome
):
    from google.api_core import exceptions, gapic_v1
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import _helpers
    from google.cloud.firestore_v1.client import Client as FirestoreClient
    from google.cloud.firestore_v1.transaction import transactional as sdk_transactional
    from google.cloud.firestore_v1.types import firestore as firestore_types
    from scripts import binance_recovery_controller as controller

    ledger, archive, control, _ = _material()

    class SDKRef(Ref):
        def __init__(self, value, document):
            super().__init__(value)
            self._document_path = (
                "projects/offline-review/databases/(default)/documents/strategy/" + document
            )

    refs = {
        "owner_ref": SDKRef(None, "owner"),
        "ledger_ref": SDKRef(ledger, "ledger"),
        "control_ref": SDKRef(control, "control"),
        "archive_ref": SDKRef(archive, "archive"),
    }
    next_value = {
        "state": "RECONCILE_ONLY",
        "recovery_id": "binance-200-1",
        **_collect(monkeypatch),
    }
    client = FirestoreClient(
        project="offline-review", credentials=AnonymousCredentials()
    )
    api = client._firestore_api
    transport = api.transport
    original = transport._wrapped_methods[transport.commit]
    retry_policy = original._retry
    attempts = []

    def fake_commit(request, **_kwargs):
        attempts.append(bytes(request.transaction))
        refs["control_ref"].snapshot = Snapshot(
            _helpers.decode_dict(request.writes[0].update.fields, client)
        )
        if outcome == "control_readback_mismatch":
            refs["control_ref"].snapshot.value["recovery_id"] = "binance-tampered-1"
        if outcome == "response_lost_after_commit" and len(attempts) == 1:
            raise exceptions.ServiceUnavailable("simulated response lost after durable commit")
        return firestore_types.CommitResponse()

    transport._wrapped_methods[transport.commit] = gapic_v1.method.wrap_method(
        fake_commit,
        default_retry=retry_policy.with_delay(initial=0, maximum=0),
        default_timeout=60,
    )
    api.begin_transaction = lambda **_kwargs: firestore_types.BeginTransactionResponse(
        transaction=b"offline-transaction"
    )
    api.rollback = lambda **_kwargs: None
    monkeypatch.setattr(
        controller.firestore, "transactional", sdk_transactional, raising=False
    )

    if outcome in {"response_lost_after_commit", "control_readback_mismatch"}:
        with pytest.raises(controller.RecoveryWriteUncertain):
            controller.save_post_rebase_control(
                client,
                refs,
                previous=control,
                next_value=next_value,
                ledger_sha256=digest(ledger),
                archive_sha256=digest(archive),
            )
    else:
        controller.save_post_rebase_control(
            client,
            refs,
            previous=control,
            next_value=next_value,
            ledger_sha256=digest(ledger),
            archive_sha256=digest(archive),
        )
    assert len(attempts) == 1
    assert attempts[0] == b"offline-transaction"


def _active_post_rebase_control(monkeypatch):
    from quant_platform_kit.common.reconciliation_recovery import (
        ReconciliationRecoveryTransitionPlan,
        calculate_reconciliation_recovery_confirmation_sha256,
    )

    package = _collect(monkeypatch)
    candidate = package["candidate"]
    recovery_id = "binance-200-1"
    confirmation = {
        "schema_version": "qsl_reconciliation_recovery_confirmation.v1",
        "recovery_id": recovery_id,
        "candidate_sha256": candidate["candidate_sha256"],
        "dual_review_binding_sha256": candidate["candidate_sha256"],
        "confirmed_at": (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirmed_by": "test-operator",
        "no_order": True,
        "execution_authority_granted": False,
    }
    confirmation["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(confirmation)
    plan = ReconciliationRecoveryTransitionPlan(
        recovery_id=recovery_id,
        candidate_sha256=candidate["candidate_sha256"],
        confirmation_sha256=confirmation["confirmation_sha256"],
        baseline_id=candidate["baseline_id"],
        baseline_target_sha256=candidate["baseline_target_sha256"],
        expected_digests={
            key: candidate[key]
            for key in (
                "positions_sha256",
                "cash_sha256",
                "open_orders_sha256",
                "recent_executions_sha256",
                "local_execution_ledger_sha256",
            )
        },
        verified_at=NOW + timedelta(seconds=2),
    )
    return {
        "state": "ACTIVE_LKG",
        "recovery_id": recovery_id,
        **package,
        "confirmation": confirmation,
        "transition_plan": plan.to_dict(),
    }


def test_active_post_rebase_control_remains_valid_after_candidate_freshness_window(monkeypatch):
    from application.reconciliation_recovery import activated_target

    control = _active_post_rebase_control(monkeypatch)
    result = activated_target(_target(), control, expected=_legacy_expected())

    assert result.live_continuity.state == "ACTIVE_LKG"


def test_active_post_rebase_control_still_rejects_source_semantic_tampering(monkeypatch):
    from application.reconciliation_recovery import activated_target

    control = _active_post_rebase_control(monkeypatch)
    control["source"]["proof"]["history_counts"]["deposits"] = 1

    with pytest.raises(ValueError, match="post_rebase_source_binding_mismatch"):
        activated_target(_target(), control, expected=_legacy_expected())


def test_post_rebase_unknown_cli_outcome_is_sanitized_and_no_retry(monkeypatch, capsys):
    from scripts import binance_recovery_controller as controller

    monkeypatch.setattr(
        controller,
        "run",
        lambda *_args: (_ for _ in ()).throw(
            controller.RecoveryWriteUncertain("private provider detail")
        ),
    )

    assert controller.main(["prepare"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "status": "uncertain",
        "stage": controller.STAGE,
        "reason_code": "post_rebase_outcome_unknown",
        "no_retry": True,
        "no_order": True,
    }


@pytest.mark.parametrize("action,publication_unknown", [("prepare", False), ("prepare", True), ("diagnose", False)])
def test_controller_prepare_routes_real_post_rebase_archive_and_returns_unresolved_status(
    monkeypatch, action, publication_unknown
):
    from scripts import binance_recovery_controller as controller

    ledger, archive, control, opening = _material()
    _configure_anchors(monkeypatch, ledger, archive, control, opening)
    expected = _legacy_expected()
    target = _target()
    current_run = {
        "id": 200,
        "head_sha": "b" * 40,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "path": ".github/workflows/main.yml",
    }
    migration_run = {
        "id": 34606795875,
        "head_sha": "ed7ee6e96cea0addb292f3f45338652095d0da58",
        "head_branch": "main",
        "event": "workflow_dispatch",
        "path": ".github/workflows/main.yml",
    }
    for name, value in {
        "GITHUB_REPOSITORY": controller.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false",
        "RECONCILE_ONLY": "true",
        "GITHUB_RUN_ID": "200",
        "GITHUB_SHA": "b" * 40,
        "GITHUB_RUN_ATTEMPT": "1",
        "BINANCE_API_KEY": "synthetic",
        "BINANCE_API_SECRET": "synthetic",
        "RECONCILIATION_RECOVERY_SYNC_TOKEN": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", digest(expected))
    monkeypatch.setattr(controller, "BASELINE_ID", target.live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256", target.live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: target)
    monkeypatch.setattr(controller, "datetime", SimpleNamespace(now=lambda _tz: NOW))
    monkeypatch.setattr(controller, "_expected_digests", lambda: expected)
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BTCUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", digest(["BTCUSDT"]))
    passive_client = Client()
    passive_client.balances.append({"asset": "PASSIVE", "free": "2", "locked": "0"})
    monkeypatch.setattr(controller, "connect_client", lambda *_args, **_kwargs: passive_client)
    monkeypatch.setattr(
        controller,
        "verified_run",
        lambda run_id, **_kwargs: migration_run if int(run_id) == 34606795875 else current_run,
    )

    docs = {
        controller.CONTROL_DOCUMENT: Ref(control),
        "MULTI_ASSET_STATE": Ref(ledger),
        "MULTI_ASSET_STATE__owner": Ref(None),
        controller.ARCHIVE_DOCUMENT: Ref(archive),
    }

    class Transaction:
        def set(self, ref, value):
            ref.snapshot = Snapshot(value)

    db = SimpleNamespace(
        collection=lambda _name: SimpleNamespace(document=lambda name: docs[name]),
        transaction=lambda *, max_attempts: Transaction(),
    )
    monkeypatch.setattr(controller, "get_firestore_client", lambda: db)
    monkeypatch.setattr(controller.firestore, "transactional", lambda fn: fn, raising=False)
    monkeypatch.setattr(
        controller,
        "_post_rebase_transaction",
        lambda _db: db.transaction(max_attempts=1),
    )

    requests = []

    def request(_url, _token, *, payload=None):
        assert payload is not None
        requests.append(payload)
        if publication_unknown:
            raise TimeoutError("publication may already be durable")
        return {
            "ok": True,
            "source_id": payload["source_id"],
            "recovery_count": 1,
            "generated_at": payload["generated_at"],
        }

    monkeypatch.setattr(controller, "request_json", request)

    if action == "diagnose":
        monkeypatch.setattr(controller, "save_post_rebase_control", lambda *_args, **_kwargs: pytest.fail("diagnosis wrote control"))
        monkeypatch.setattr(controller, "_save_control", lambda *_args, **_kwargs: pytest.fail("diagnosis wrote legacy control"))
        result = controller.run(action)
        assert result == {"status": "diagnosed", "source_kind": "post_rebase", "historical_difference_unresolved": True,
                          "non_managed_spot_policy": "observe_only", "observed_non_managed_asset_count": 1,
                          "no_order": True, "write_performed": False, "execution_authority_granted": False}
        assert not requests
        assert docs[controller.CONTROL_DOCUMENT].snapshot.value == control
        return

    if publication_unknown:
        with pytest.raises(controller.RecoveryWriteUncertain):
            controller.run("prepare")
    else:
        result = controller.run("prepare")
        assert result["status"] == "awaiting_human_confirmation"
        assert result["source_kind"] == "post_rebase"
        assert result["historical_difference_unresolved"] is True
    assert len(requests) == 1
    assert docs[controller.CONTROL_DOCUMENT].snapshot.value["source"]["kind"] == "post_rebase"


@pytest.mark.parametrize("error,expected", [
    (ValueError("post_rebase_quantity_mismatch"), "post_rebase_quantity_mismatch"),
    (ValueError("post_rebase_history_incomplete"), "post_rebase_history_incomplete"),
    (ValueError("post_rebase_quantity_mismatch private account payload"), "recovery_operation_failed"),
    (RuntimeError("private provider payload"), "recovery_operation_failed"),
])
def test_diagnose_cli_reports_only_exact_safe_reason_codes(monkeypatch, capsys, error, expected):
    from scripts import binance_recovery_controller as controller

    def fail(*_args):
        raise error

    monkeypatch.setattr(controller, "run", fail)
    assert controller.main(["diagnose"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == expected
    assert result["no_order"] is True
    assert "private" not in json.dumps(result)


def test_controller_verify_rejects_archive_metadata_change_before_confirmation_or_broker_read(monkeypatch):
    from scripts import binance_recovery_controller as controller

    ledger, archive, control, opening = _material()
    _configure_anchors(monkeypatch, ledger, archive, control, opening)
    package = _collect(monkeypatch, current=ledger, archive=archive)
    recovery_id = "binance-200-1"
    previous = {"state": "RECONCILE_ONLY", "recovery_id": recovery_id, **package}
    changed_archive = {**archive, "unapproved_metadata": "changed"}
    expected = _legacy_expected()
    target = _target()
    for name, value in {
        "GITHUB_REPOSITORY": controller.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false",
        "RECONCILE_ONLY": "true",
        "GITHUB_RUN_ID": "201",
        "GITHUB_SHA": "c" * 40,
        "BINANCE_API_KEY": "synthetic",
        "BINANCE_API_SECRET": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", digest(expected))
    monkeypatch.setattr(controller, "BASELINE_ID", target.live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256", target.live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: target)
    monkeypatch.setattr(controller, "_expected_digests", lambda: expected)
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BTCUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", digest(["BTCUSDT"]))
    runs = {
        201: {"id": 201, "head_sha": "c" * 40, "head_branch": "main", "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        200: previous["source"]["run"],
        34606795875: previous["source"]["migration_run"],
    }
    monkeypatch.setattr(controller, "verified_run", lambda run_id, **_kwargs: runs[int(run_id)])
    docs = {
        controller.CONTROL_DOCUMENT: Ref(previous),
        "MULTI_ASSET_STATE": Ref(ledger),
        "MULTI_ASSET_STATE__owner": Ref(None),
        controller.ARCHIVE_DOCUMENT: Ref(changed_archive),
    }
    db = SimpleNamespace(
        collection=lambda _name: SimpleNamespace(document=lambda name: docs[name]),
    )
    monkeypatch.setattr(controller, "get_firestore_client", lambda: db)
    monkeypatch.setattr(controller, "request_json", lambda *_args, **_kwargs: pytest.fail("confirmation read after archive changed"))
    monkeypatch.setattr(controller, "connect_client", lambda *_args, **_kwargs: pytest.fail("broker read after archive changed"))

    with pytest.raises(ValueError, match="post_rebase_archive_changed"):
        controller.run("verify", recovery_id)


def _confirmation_response(candidate, recovery_id, confirmed_at):
    from quant_platform_kit.common.reconciliation_recovery import (
        calculate_reconciliation_recovery_confirmation_sha256,
    )

    confirmation = {
        "schema_version": "qsl_reconciliation_recovery_confirmation.v1",
        "recovery_id": recovery_id,
        "candidate_sha256": candidate["candidate_sha256"],
        "dual_review_binding_sha256": candidate["candidate_sha256"],
        "confirmed_at": confirmed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirmed_by": "test-operator",
        "no_order": True,
        "execution_authority_granted": False,
    }
    confirmation["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(
        confirmation
    )
    return {
        "ok": True,
        "schema_version": "qsl_reconciliation_recovery_controller_read.v1",
        "policy": {
            "no_order": True,
            "execution_authority_granted": False,
            "controller_must_reverify": True,
        },
        "recovery": {
            "recovery_id": recovery_id,
            "platform": "binance",
            "strategy_profile": candidate["strategy_profile"],
            "environment": "live",
            "reconciliation_state": "RECONCILE_ONLY",
            "candidate_sha256": candidate["candidate_sha256"],
            "dual_review_binding_sha256": candidate["candidate_sha256"],
            "evidence_sample_count": len(candidate["source_evidence_sha256"]),
        },
        "confirmation": confirmation,
    }


def _setup_post_rebase_controller_confirmation(monkeypatch, *, response_change=None, passive=False, passive_changed=False):
    from scripts import binance_recovery_controller as controller

    prepared_at = NOW
    fresh_at = NOW + timedelta(minutes=2)
    ledger, archive, control, opening = _material()
    _configure_anchors(monkeypatch, ledger, archive, control, opening)
    prepared_client = Client()
    if passive:
        prepared_client.balances.append({"asset": "PASSIVE", "free": "2", "locked": "0"})
    package = _collect(
        monkeypatch, client=prepared_client, current=ledger, archive=archive, now=prepared_at,
        observe_only_non_managed_spot=passive,
    )
    recovery_id = "binance-200-1"
    previous = {"state": "RECONCILE_ONLY", "recovery_id": recovery_id, **package}
    expected = _legacy_expected()
    target = _target()
    response = _confirmation_response(
        package["candidate"], recovery_id, prepared_at + timedelta(minutes=1)
    )
    if response_change == "missing":
        response = {}
    elif response_change == "wrong":
        response["recovery"]["candidate_sha256"] = "f" * 64

    for name, value in {
        "GITHUB_REPOSITORY": controller.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false",
        "RECONCILE_ONLY": "true",
        "GITHUB_RUN_ID": "201",
        "GITHUB_SHA": "c" * 40,
        "BINANCE_API_KEY": "synthetic",
        "BINANCE_API_SECRET": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", digest(expected))
    monkeypatch.setattr(controller, "BASELINE_ID", target.live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256", target.live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: target)
    monkeypatch.setattr(controller, "datetime", SimpleNamespace(now=lambda _tz: fresh_at))
    real_activation_evaluation = controller.evaluate_reconciliation_recovery_activation
    monkeypatch.setattr(
        controller,
        "evaluate_reconciliation_recovery_activation",
        lambda **kwargs: real_activation_evaluation(**kwargs, now=fresh_at),
    )
    monkeypatch.setattr(controller, "_expected_digests", lambda: expected)
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BTCUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", digest(["BTCUSDT"]))
    current_run = {
        "id": 201,
        "head_sha": "c" * 40,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "path": ".github/workflows/main.yml",
    }
    runs = {
        201: current_run,
        200: previous["source"]["run"],
        34606795875: previous["source"]["migration_run"],
    }
    monkeypatch.setattr(controller, "verified_run", lambda run_id, **_kwargs: runs[int(run_id)])
    broker_reads = []

    def connect(*_args, **_kwargs):
        broker_reads.append(True)
        client = Client()
        if passive:
            client.balances.append({"asset": "PASSIVE", "free": "3" if passive_changed else "2", "locked": "0"})
        return client

    monkeypatch.setattr(controller, "connect_client", connect)
    monkeypatch.setattr(controller, "request_json", lambda *_args, **_kwargs: response)
    docs = {
        controller.CONTROL_DOCUMENT: Ref(previous),
        "MULTI_ASSET_STATE": Ref(ledger),
        "MULTI_ASSET_STATE__owner": Ref(None),
        controller.ARCHIVE_DOCUMENT: Ref(archive),
    }
    writes = []
    attempts = []

    class Transaction:
        def set(self, ref, value):
            writes.append((ref, copy.deepcopy(value)))
            ref.snapshot = Snapshot(value)

    def transaction(*, max_attempts):
        attempts.append(max_attempts)
        return Transaction()

    db = SimpleNamespace(
        collection=lambda _name: SimpleNamespace(document=lambda name: docs[name]),
        transaction=transaction,
    )
    monkeypatch.setattr(controller, "get_firestore_client", lambda: db)
    monkeypatch.setattr(controller.firestore, "transactional", lambda fn: fn, raising=False)
    monkeypatch.setattr(
        controller,
        "_post_rebase_transaction",
        lambda _db: db.transaction(max_attempts=1),
    )
    return controller, recovery_id, docs, writes, attempts, broker_reads


@pytest.mark.parametrize("passive", [False, True])
@pytest.mark.parametrize("action", ["verify", "activate"])
def test_controller_post_rebase_confirmation_rechecks_fresh_evidence_and_only_activate_writes(
    monkeypatch, action, passive
):
    controller, recovery_id, docs, writes, attempts, broker_reads = (
        _setup_post_rebase_controller_confirmation(monkeypatch, passive=passive)
    )

    result = controller.run(action, recovery_id)

    assert result["status"] == ("verified" if action == "verify" else "active_lkg")
    assert result["source_kind"] == "post_rebase"
    assert result["historical_difference_unresolved"] is True
    assert result["runtime_target_enabled"] is False
    assert result["no_order"] is True
    assert broker_reads == [True]
    if action == "verify":
        assert writes == [] and attempts == []
        assert docs[controller.CONTROL_DOCUMENT].snapshot.value["state"] == "RECONCILE_ONLY"
    else:
        assert len(writes) == 1 and attempts == [1]
        assert docs[controller.CONTROL_DOCUMENT].snapshot.value["state"] == "ACTIVE_LKG"


@pytest.mark.parametrize("response_change", ["missing", "wrong"])
def test_controller_post_rebase_bad_confirmation_stops_before_broker_and_write(
    monkeypatch, response_change
):
    controller, recovery_id, docs, writes, attempts, broker_reads = (
        _setup_post_rebase_controller_confirmation(
            monkeypatch, response_change=response_change
        )
    )

    with pytest.raises(ValueError, match="confirmation"):
        controller.run("activate", recovery_id)

    assert broker_reads == []
    assert writes == [] and attempts == []
    assert docs[controller.CONTROL_DOCUMENT].snapshot.value["state"] == "RECONCILE_ONLY"


@pytest.mark.parametrize("change", [None, "second_read", "locked", "managed_quantity"])
def test_observed_assets_remain_in_full_account_evidence(monkeypatch, change):
    from application.rebased_recovery import validate_post_rebase_source

    client = Client()
    client.balances.append({"asset": "PASSIVE", "free": "2", "locked": "0"})
    if change == "second_read":
        client.mutate_after_first_read = lambda obj: obj.balances[-1].update(free="3")
    elif change == "locked":
        client.balances[-1]["locked"] = "1"
    elif change == "managed_quantity":
        client.balances[0]["free"] = "0.2"
    errors = {
        "second_read": "post_rebase_quantity_changed_during_read",
        "locked": "post_rebase_locked_balance_present",
        "managed_quantity": "post_rebase_quantity_mismatch",
    }
    if change:
        with pytest.raises(ValueError, match=errors[change]):
            _collect(monkeypatch, client=client, observe_only_non_managed_spot=True)
        return
    package = _collect(monkeypatch, client=client, observe_only_non_managed_spot=True)
    validate_post_rebase_source(package, runtime_target=_target(), legacy_expected=_legacy_expected(), now=NOW)
    assert package["source"]["proof"]["non_managed_spot_policy"] == "observe_only"
    assert package["source"]["proof"]["observed_non_managed_asset_count"] == 1
    without_passive = _collect(monkeypatch, observe_only_non_managed_spot=True)
    for key in ("positions_sha256", "cash_sha256"):
        assert package["candidate"][key] != without_passive["candidate"][key]
    assert "PASSIVE" not in json.dumps(package)


@pytest.mark.parametrize("policy,count", [("ignore", 1), ("reject", 1), ("observe_only", -1), ("observe_only", True)])
def test_source_rejects_invalid_passive_scope_proof(monkeypatch, policy, count):
    from application.rebased_recovery import validate_post_rebase_source

    package = _collect(monkeypatch)
    package["source"]["proof"].update(
        non_managed_spot_policy=policy, observed_non_managed_asset_count=count,
    )
    package["candidate"]["source_receipts_sha256"] = digest(package["source"])
    with pytest.raises(ValueError, match="post_rebase_source_binding_mismatch"):
        validate_post_rebase_source(package, runtime_target=_target(), legacy_expected=_legacy_expected(), now=NOW)



def test_passive_change_after_confirmation_blocks_activation_without_write(monkeypatch):
    controller, recovery_id, docs, writes, attempts, broker_reads = (
        _setup_post_rebase_controller_confirmation(monkeypatch, passive=True, passive_changed=True)
    )
    with pytest.raises(ValueError, match="post_rebase_fresh_candidate_changed"):
        controller.run("activate", recovery_id)
    assert broker_reads == [True]
    assert writes == [] and attempts == []
    assert docs[controller.CONTROL_DOCUMENT].snapshot.value["state"] == "RECONCILE_ONLY"
