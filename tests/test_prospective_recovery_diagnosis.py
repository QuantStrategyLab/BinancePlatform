"""Read-only diagnosis for the approved prospective Binance opening."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256 as digest,
)
from tests.test_broker_reconciliation import _target
from tests.test_forward_earn_accounting import materials
from tests.test_post_rebase_recovery import Ref


OPENING_AT = datetime(2026, 9, 12, 11, 4, 4, 281392, tzinfo=timezone.utc)
NOW = OPENING_AT + timedelta(minutes=20)
LATER = NOW + timedelta(seconds=1)


def _material(monkeypatch):
    from application import rebased_recovery as recovery
    from scripts.migrate_daily_accounting_state import _ACCOUNTING_FIELDS

    state, _current, _cash = materials()
    checkpoint = copy.deepcopy(state["earn_accrual_checkpoint"])
    checkpoint["observed_at"] = OPENING_AT.isoformat()
    checkpoint["account_scope_sha256"] = digest({"account_uid": "synthetic"})
    state["earn_accrual_checkpoint"] = checkpoint
    state["external_cash_flow_cursor"]["observed_at"] = OPENING_AT.isoformat()
    old = {
        "is_circuit_broken": True,
        "order_submission": {"state": "TERMINAL"},
        "unknown_future_field": {"keep": True},
        "accounting_rebase": {"archive_document": "preserved-old-archive"},
    }
    control = {
        "state": "RECONCILE_ONLY",
        "source": {"original_evidence": {"account_scope_sha256": checkpoint["account_scope_sha256"]}},
    }
    proposed = {key: copy.deepcopy(state.get(key, 0)) for key in _ACCOUNTING_FIELDS}
    proposed.update(
        earn_accrual_checkpoint=copy.deepcopy(state["earn_accrual_checkpoint"]),
        earn_accounted_net_changes=copy.deepcopy(state["earn_accounted_net_changes"]),
        external_cash_flow_cursor=copy.deepcopy(state["external_cash_flow_cursor"]),
    )
    proposal = {
        "source_sha": "c625413dcc601412358efbb5373e38788da95d0e",
        "ledger_sha256": digest(old),
        "control_sha256": digest(control),
        "ledger_update_time": "2026-09-12T11:03:00Z",
        "proposed_fields": proposed,
        "historical_difference_unresolved": True,
    }
    marker = {
        "archive_document": recovery.PROSPECTIVE_ARCHIVE_DOCUMENT,
        "started_at": "2026-09-12T11:19:09.803040+00:00",
        "opening_balance_observed_at": OPENING_AT.isoformat(),
        "historical_difference_unresolved": True,
        "approved_proposal_run_id": recovery.PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID,
        "approved_proposal_sha256": digest(proposal),
    }
    ledger = {**old, **proposed, "accounting_rebase": marker}
    archive = {
        "ledger": copy.deepcopy(old),
        "recovery_control": copy.deepcopy(control),
        "ledger_update_time": proposal["ledger_update_time"],
        **marker,
        "approved_proposal": copy.deepcopy(proposal),
        "new_ledger_sha256": digest(ledger),
        "valuation_price_source": "binance_get_avg_price_estimate",
    }
    monkeypatch.setattr(recovery, "APPROVED_PROSPECTIVE_SHA256", digest(proposal))
    monkeypatch.setattr(recovery, "PROSPECTIVE_LEDGER_SHA256", digest(ledger))
    monkeypatch.setattr(recovery, "PROSPECTIVE_ARCHIVE_SHA256", digest(archive))
    return ledger, archive, control


class Client:
    def __init__(self, change=None):
        self.change = change
        self.earn_reads = 0
        self.flow_reads = 0

    def get_account(self):
        uid = "changed" if self.change == "account" else "synthetic"
        return {
            "uid": uid,
            "balances": [
                {"asset": "USDT", "free": "100", "locked": "0"},
                {"asset": "BNB", "free": "1", "locked": "0"},
            ],
        }

    def get_simple_earn_flexible_product_position(self, *, current, size):
        assert current == 1 and size == 100
        self.earn_reads += 1
        increment = (
            3 - self.earn_reads if self.change == "counter_between_reads" else self.earn_reads
        )
        reward = "0" if self.change == "counter" else f"0.1000000{increment}"
        total = f"2.0000000{increment}"
        return {
            "total": 1,
            "rows": [{
                "asset": "BNB",
                "productId": "BNB001",
                "totalAmount": total,
                "cumulativeRealTimeRewards": reward,
                "collateralAmount": "0",
                "autoSubscribe": True,
                "canRedeem": True,
            }],
        }

    def get_open_orders(self):
        if self.change != "order":
            return []
        return [{"orderId": 1, "symbol": "BNBUSDT", "status": "NEW", "side": "BUY",
                 "type": "LIMIT", "origQty": "1", "executedQty": "0", "updateTime": 1}]

    def get_my_trades(self, **_kwargs):
        if self.change != "trade":
            return []
        return [{"id": 1, "orderId": 1, "symbol": "BNBUSDT", "qty": "1", "price": "1",
                 "commission": "0", "commissionAsset": "BNB", "time": 1, "isBuyer": True}]

    def _request_margin_api(self, method, path, **kwargs):
        assert method == "get" and kwargs["signed"] is True
        if path == "capital/deposit/hisrec":
            return []
        if path == "capital/withdraw/history":
            self.flow_reads += 1
            if self.change == "flow":
                return [{"id": "new-withdrawal", "status": 6}]
            return []
        raise AssertionError(f"unexpected path {path}")


def _run(monkeypatch, *, change=None, ledger_change=None, archive_change=None):
    from application.rebased_recovery import collect_prospective_rebase_diagnosis

    ledger, archive, control = _material(monkeypatch)
    if ledger_change:
        ledger[ledger_change] = "tampered"
    if archive_change:
        archive[archive_change] = "tampered"
    before = copy.deepcopy(ledger)
    result = collect_prospective_rebase_diagnosis(
        client=Client(change),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        recovery_control=control,
        symbols=("BNBUSDT",),
        source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                    "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                       "head_branch": "main", "event": "workflow_dispatch",
                       "path": ".github/workflows/main.yml"},
        now=NOW,
        clock=lambda: LATER,
    )
    assert ledger == before
    return result


def test_prospective_diagnosis_accepts_income_growth_without_mutating_ledger(monkeypatch):
    result = _run(monkeypatch)

    assert result == {
        "status": "diagnosed",
        "source_kind": "prospective_rebase",
        "historical_difference_unresolved": True,
        "forward_accounting_conserved": True,
        "checkpoint_samples": 2,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"ledger_change": "unknown_future_field"}, "prospective_rebase_ledger_invalid"),
        ({"archive_change": "unexpected"}, "prospective_rebase_archive_invalid"),
        ({"change": "account"}, "prospective_rebase_checkpoint_unavailable"),
        ({"change": "counter"}, "prospective_rebase_conservation_unverified"),
        ({"change": "counter_between_reads"}, "prospective_rebase_conservation_unverified"),
        ({"change": "flow"}, "prospective_rebase_conservation_unverified"),
        ({"change": "order"}, "prospective_rebase_open_orders_present"),
        ({"change": "trade"}, "prospective_rebase_recent_executions_present"),
    ],
)
def test_prospective_diagnosis_fails_closed_on_material_or_activity_change(monkeypatch, kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        _run(monkeypatch, **kwargs)


@pytest.mark.parametrize("changed_during_read", [False, True])
def test_controller_prospective_diagnosis_readback_is_stable_and_never_writes(
    monkeypatch, changed_during_read
):
    from scripts import binance_recovery_controller as controller

    ledger, archive, control = _material(monkeypatch)
    target = _target()
    expected = {"account_scope_sha256": digest({"account_uid": "synthetic"})}
    for name, value in {
        "GITHUB_REPOSITORY": controller.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false",
        "RECONCILE_ONLY": "true",
        "GITHUB_RUN_ID": "400",
        "GITHUB_SHA": "b" * 40,
        "BINANCE_API_KEY": "synthetic",
        "BINANCE_API_SECRET": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", digest(expected))
    monkeypatch.setattr(controller, "BASELINE_ID", target.live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256", target.live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: target)
    monkeypatch.setattr(controller, "_expected_digests", lambda: expected)
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BNBUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", digest(["BNBUSDT"]))
    monkeypatch.setattr(controller, "datetime", SimpleNamespace(now=lambda _tz: NOW))
    current_run = {"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                   "event": "workflow_dispatch", "path": ".github/workflows/main.yml"}
    migration_run = {"id": controller.PROSPECTIVE_MIGRATION_RUN_ID,
                     "head_sha": controller.PROSPECTIVE_MIGRATION_RUN_SHA,
                     "head_branch": "main", "event": "workflow_dispatch",
                     "path": ".github/workflows/main.yml"}
    monkeypatch.setattr(controller, "verified_run",
                        lambda run_id, **_kwargs: migration_run if int(run_id) == migration_run["id"] else current_run)
    monkeypatch.setattr(controller, "connect_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(controller, "prospective_clock", lambda: LATER)
    class ArchiveRef(Ref):
        def __init__(self, value):
            super().__init__(value)
            self.reads = 0

        def get(self, **kwargs):
            self.reads += 1
            if changed_during_read and self.reads > 1:
                changed = self.snapshot.to_dict()
                changed["changed"] = True
                return SimpleNamespace(exists=True, to_dict=lambda: changed)
            return super().get(**kwargs)

    docs = {
        controller.CONTROL_DOCUMENT: Ref(control),
        "MULTI_ASSET_STATE": Ref(ledger),
        "MULTI_ASSET_STATE__owner": Ref(None),
        controller.ARCHIVE_DOCUMENT: Ref(None),
        controller.PROSPECTIVE_ARCHIVE_DOCUMENT: ArchiveRef(archive),
    }
    db = SimpleNamespace(collection=lambda _name: SimpleNamespace(document=lambda name: docs[name]))
    monkeypatch.setattr(controller, "get_firestore_client", lambda: db)
    monkeypatch.setattr(controller, "save_post_rebase_control",
                        lambda *_args, **_kwargs: pytest.fail("diagnosis wrote control"))

    if changed_during_read:
        with pytest.raises(ValueError, match="prospective_rebase_state_changed_during_read"):
            controller.run("diagnose")
        return

    result = controller.run("diagnose")

    assert result["status"] == "diagnosed"
    assert result["ledger_unchanged"] is True
    assert result["control_unchanged"] is True
    assert result["owner_absent"] is True
    assert result["archive_unchanged"] is True
    assert result["write_performed"] is False
