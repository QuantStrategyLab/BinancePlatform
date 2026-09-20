"""One-shot historical continuity from approved opening through chunked Binance history."""

from __future__ import annotations

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256 as digest,
)
from tests.test_prospective_recovery_diagnosis import (
    OPENING_AT,
    Client as ProspectiveClient,
    _material,
)
from tests.test_broker_reconciliation import _target


NOW_BEYOND = OPENING_AT + timedelta(days=8)
LATER_BEYOND = NOW_BEYOND + timedelta(seconds=1)


def _evolve_ledger_without_daily_loss(ledger, *, observed_at):
    """Advance only cash-flow cursor time; leave daily-loss semantic fields untouched."""
    evolved = copy.deepcopy(ledger)
    cursor = copy.deepcopy(evolved["external_cash_flow_cursor"])
    cursor["observed_at"] = (
        observed_at.isoformat() if hasattr(observed_at, "isoformat") else observed_at
    )
    evolved["external_cash_flow_cursor"] = cursor
    return evolved


def _bnb_earn_row(*, total="2", rewards="0.1", collateral="0"):
    return {
        "asset": "BNB",
        "productId": "BNB001",
        "totalAmount": total,
        "cumulativeRealTimeRewards": rewards,
        "collateralAmount": collateral,
        "autoSubscribe": True,
        "canRedeem": True,
    }


def _earn_page_for_asset(asset, rows_when_bnb):
    """Match collect_earn_checkpoint (no asset) and per-asset readers."""
    if asset is None or str(asset).upper() == "BNB":
        return {"total": len(rows_when_bnb), "rows": list(rows_when_bnb)}
    return {"total": 0, "rows": []}


def _empty_history_client(*, reward_rows_by_window=None, page_full_surface=None):
    """Complete empty pages for every diagnose_balance_flows surface."""
    reward_rows_by_window = reward_rows_by_window or {}
    calls = []

    def read(method, path, **kwargs):
        assert method == "get" and kwargs["signed"] is True
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        calls.append((path, start_ms, end_ms))
        if page_full_surface and path.endswith(page_full_surface):
            if path.startswith("capital/"):
                return [{"id": f"row-{i}", "status": 1} for i in range(1000)]
            return {"rows": [{"asset": "BNB", "type": "BONUS", "projectId": "x",
                              "time": start_ms, "rewards": "0"} for _ in range(100)],
                    "total": 100}
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            rows = list(reward_rows_by_window.get((start_ms, end_ms), []))
            return {"rows": rows, "total": len(rows)}
        return {"rows": [], "total": 0}

    return SimpleNamespace(_request_margin_api=read), calls


def test_chunked_balance_flows_accepts_explicit_span_beyond_max_history_when_complete():
    from application.broker_reconciliation import diagnose_chunked_balance_flows
    from application.rebased_recovery import MAX_HISTORY

    assert NOW_BEYOND - OPENING_AT > MAX_HISTORY
    client, calls = _empty_history_client()

    result = diagnose_chunked_balance_flows(
        client, start=OPENING_AT, end=NOW_BEYOND, now=NOW_BEYOND
    )

    assert result["history_complete_for_requested_surfaces"] is True
    assert result["chunk_count"] == 2
    assert result["history_counts"]["deposits"] == 0
    assert result["history_counts"]["earn_rewards"] == 0
    assert result["execution_authority_granted"] is False
    assert len(calls) >= 17 * 2
    spans = sorted({(start, end) for _path, start, end in calls})
    assert spans[0][0] == int(OPENING_AT.timestamp() * 1000)
    assert spans[-1][1] == int(NOW_BEYOND.timestamp() * 1000)
    assert spans[0][1] + 1 == spans[1][0]


def test_chunked_balance_flows_accepts_exact_full_single_page_when_total_matches():
    """A page at the API size limit is complete when total equals the row count."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    end = OPENING_AT + timedelta(days=1)

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            rows = [
                {
                    "asset": "BNB",
                    "type": "REALTIME",
                    "projectId": "BNB001",
                    "time": start_ms + i,
                    "rewards": "0",
                }
                for i in range(100)
            ]
            return {"rows": rows, "total": 100}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=end,
        now=end,
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is True
    assert result["history_counts"]["earn_rewards"] == 100


def test_chunked_balance_flows_paginates_full_reward_pages():
    """Full pages must continue until the final short page; identities stay unique."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    end = OPENING_AT + timedelta(days=1)
    start_ms = int(OPENING_AT.timestamp() * 1000)
    pages_seen = []

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            current = int(data.get("current") or 1)
            pages_seen.append(current)
            if current == 1:
                rows = [
                    {
                        "asset": "BNB",
                        "type": "REALTIME",
                        "projectId": "BNB001",
                        "time": start_ms + i,
                        "rewards": "0.001",
                    }
                    for i in range(100)
                ]
                return {"rows": rows, "total": 150}
            if current == 2:
                rows = [
                    {
                        "asset": "BNB",
                        "type": "REALTIME",
                        "projectId": "BNB001",
                        "time": start_ms + 100 + i,
                        "rewards": "0.001",
                    }
                    for i in range(50)
                ]
                return {"rows": rows, "total": 150}
            return {"rows": [], "total": 150}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=end,
        now=end,
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert pages_seen == [1, 2]
    assert result["history_complete_for_requested_surfaces"] is True
    assert result["history_counts"]["earn_rewards"] == 150
    assert result["reward_quantity_totals"]["BNB"]["REALTIME"] == "0.150"


def test_chunked_balance_flows_rejects_full_page_when_total_exceeds_visible_rows():
    """Full page with a larger total and no further pages must fail closed."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            current = int(data.get("current") or 1)
            if current == 1:
                return {
                    "rows": [
                        {
                            "asset": "BNB",
                            "type": "REALTIME",
                            "projectId": "BNB001",
                            "time": start_ms + i,
                            "rewards": "0",
                        }
                        for i in range(100)
                    ],
                    "total": 150,
                }
            return {"rows": [], "total": 150}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_incomplete"


def test_chunked_balance_flows_rejects_duplicate_boundary_reward_identity():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(days=1)).timestamp() * 1000)
    row = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB001",
        "time": stamp,
    }
    calls = []

    def read(method, path, **kwargs):
        assert method == "get" and kwargs["signed"] is True
        data = kwargs.get("data") or {}
        calls.append((path, data.get("startTime"), data.get("endTime")))
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            # Same identity in every chunk — must fail closed on aggregation.
            return {"rows": [row], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=NOW_BEYOND,
        now=NOW_BEYOND,
    )

    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] in {
        "balance_history_duplicate_event",
        "balance_history_reward_validation_failed",
        "balance_history_incomplete",
    }
    assert len({(s, e) for _p, s, e in calls}) >= 2


def test_historical_continuity_diagnosis_accepts_earn_only_beyond_max_history(monkeypatch):
    from application.rebased_recovery import (
        MAX_HISTORY,
        collect_historical_continuity_diagnosis,
    )

    ledger, archive, _control = _material(monkeypatch)
    assert NOW_BEYOND - OPENING_AT > MAX_HISTORY
    # Evolved ledger: digest no longer equals opening; must not use earn checkpoint as bridge.
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    assert digest(ledger) != digest(
        {**archive["ledger"], **archive["approved_proposal"]["proposed_fields"],
         "accounting_rebase": ledger["accounting_rebase"]}
    )

    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            assert current == 1 and size == 100
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    result = collect_historical_continuity_diagnosis(
        client=StableClient(),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                    "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                       "head_branch": "main", "event": "workflow_dispatch",
                       "path": ".github/workflows/main.yml"},
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )

    assert result["status"] == "diagnosed"
    assert result["source_kind"] == "historical_continuity"
    assert result["current_ledger_sha256"] == digest(ledger)
    assert result["historical_difference_unresolved"] is True
    assert result["no_order"] is True
    assert result["write_performed"] is False
    assert result["execution_authority_granted"] is False
    assert result["chunk_count"] >= 2


def test_historical_continuity_rejects_ledger_checkpoint_as_continuity_bridge(monkeypatch):
    from application.earn_accrual import prepare_forward_earn_state
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    opening, archive, _control = _material(monkeypatch)
    mid_at = OPENING_AT + timedelta(minutes=10)
    mid_checkpoint = copy.deepcopy(opening["earn_accrual_checkpoint"])
    mid_checkpoint["observed_at"] = mid_at.isoformat()
    mid_checkpoint["assets"]["BNB"]["products"]["BNB001"].update(
        total="2.00000001", realtime_rewards="0.10000001"
    )
    mid_checkpoint["assets"]["BNB"]["quantity"] = "3.00000001"
    evolved = prepare_forward_earn_state(
        opening,
        mid_checkpoint,
        {
            "new_deposit_principal_usdt": "0",
            "new_deposit_completed_at": [],
            "new_confirmed_deposit_count": 0,
            "new_unsupported_deposit_count": 0,
            "new_or_changed_withdrawal_count": 0,
            "bnb_dividend_quantity": "0",
            "cursor": {"version": 1, "observed_at": mid_at.isoformat(), "records": {}},
        },
    )
    # No broker history proving the earn delta — must reject.
    client = ProspectiveClient()
    history_client, _calls = _empty_history_client()
    client._request_margin_api = history_client._request_margin_api

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=client,
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=evolved,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("trade", "historical_continuity_unsupported_activity"),
        ("order", "historical_continuity_open_orders_present"),
        ("deposit", "historical_continuity_unsupported_activity"),
    ],
)
def test_historical_continuity_fails_closed_on_unsupported_or_open_activity(
    monkeypatch, change, reason
):
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    class BusyClient(ProspectiveClient):
        def get_my_trades(self, **_kwargs):
            if change != "trade":
                return []
            return [{"id": 1, "orderId": 1, "symbol": "BNBUSDT", "qty": "1", "price": "1",
                     "commission": "0", "commissionAsset": "BNB",
                     "time": int((OPENING_AT + timedelta(days=1)).timestamp() * 1000),
                     "isBuyer": True}]

        def get_open_orders(self):
            if change != "order":
                return []
            return [{"orderId": 1, "symbol": "BNBUSDT", "status": "NEW", "side": "BUY",
                     "type": "LIMIT", "origQty": "1", "executedQty": "0", "updateTime": 1}]

        def _request_margin_api(self, method, path, **kwargs):
            if change == "deposit" and path == "capital/deposit/hisrec":
                stamp = int((OPENING_AT + timedelta(days=1)).timestamp() * 1000)
                data = kwargs.get("data") or {}
                start_ms = data.get("startTime")
                end_ms = data.get("endTime")
                if type(start_ms) is int and type(end_ms) is int and start_ms <= stamp <= end_ms:
                    return [{
                        "id": "dep-1", "amount": "10", "coin": "USDT", "status": 1,
                        "insertTime": stamp, "completeTime": stamp, "walletType": 0,
                        "transferType": 0, "txId": "tx-1",
                    }]
                return []
            if path.startswith("capital/"):
                return []
            if path == "asset/assetDividend":
                return {"total": 0, "rows": []}
            return {"rows": [], "total": 0}

    with pytest.raises(ValueError, match=reason):
        collect_historical_continuity_diagnosis(
            client=BusyClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_single_window_max_history_guard_unchanged():
    """Do not enlarge MAX_HISTORY; single-window diagnose still rejects >7d."""
    from application.broker_reconciliation import diagnose_balance_flows
    from application.rebased_recovery import MAX_HISTORY

    client, calls = _empty_history_client()
    with pytest.raises(ValueError, match="balance_history_window_invalid"):
        diagnose_balance_flows(
            client, start=OPENING_AT, end=OPENING_AT + MAX_HISTORY + timedelta(seconds=1),
            now=NOW_BEYOND,
        )
    assert not calls


def test_controller_prepare_allows_verified_quiesced_prior_with_new_history(monkeypatch):
    """Quiesced ACTIVE_LKG retains confirmation/transition; verify then prepare new candidate."""
    from quant_platform_kit.common.reconciliation_recovery import (
        ReconciliationRecoveryTransitionPlan,
        calculate_reconciliation_recovery_confirmation_sha256,
    )
    from tests.test_prospective_recovery_diagnosis import _controller_setup

    controller, target, expected, old_recovery_id, docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True)
    )
    package = docs[controller.CONTROL_DOCUMENT].snapshot.value
    candidate = package["candidate"]
    confirmation = {
        "schema_version": "qsl_reconciliation_recovery_confirmation.v1",
        "recovery_id": old_recovery_id,
        "candidate_sha256": candidate["candidate_sha256"],
        "dual_review_binding_sha256": candidate["candidate_sha256"],
        "confirmed_at": (OPENING_AT + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirmed_by": "test-operator",
        "no_order": True,
        "execution_authority_granted": False,
    }
    confirmation["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(
        confirmation
    )
    plan = ReconciliationRecoveryTransitionPlan(
        recovery_id=old_recovery_id,
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
        verified_at=OPENING_AT + timedelta(minutes=2),
    )
    # Quiesced: state RECONCILE_ONLY but prior confirmation/transition retained.
    package["state"] = "RECONCILE_ONLY"
    package["confirmation"] = confirmation
    package["transition_plan"] = plan.to_dict()
    docs[controller.CONTROL_DOCUMENT].snapshot.value = package

    # Force collection beyond MAX_HISTORY so historical continuity path is used.
    monkeypatch.setattr(
        controller, "datetime",
        SimpleNamespace(now=lambda _tz: NOW_BEYOND),
    )
    monkeypatch.setattr(controller, "prospective_clock", lambda: LATER_BEYOND)
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    monkeypatch.setattr(controller, "connect_client", lambda *_a, **_k: StableClient())

    # Evolved current ledger bound as verification target.
    ledger = docs["MULTI_ASSET_STATE"].snapshot.value
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    docs["MULTI_ASSET_STATE"].snapshot.value = ledger

    result = controller.run("prepare")

    assert result["status"] == "awaiting_human_confirmation"
    assert result["recovery_id"] != old_recovery_id
    assert len(writes) == 1 and len(requests) == 1
    stored = docs[controller.CONTROL_DOCUMENT].snapshot.value
    assert stored["source"]["kind"] == "historical_continuity"
    assert stored["source"]["current_ledger_sha256"] == digest(ledger)
    # Old confirmation was verified as a gate, not deleted to bypass; new candidate replaces control.
    assert "confirmation" not in stored
    assert stored["state"] == "RECONCILE_ONLY"


def test_controller_prepare_still_rejects_forged_quiesced_confirmation(monkeypatch):
    from tests.test_prospective_recovery_diagnosis import _controller_setup

    controller, _target_value, _expected, _recovery_id, docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True)
    )
    docs[controller.CONTROL_DOCUMENT].snapshot.value["confirmation"] = {"present": True}
    docs[controller.CONTROL_DOCUMENT].snapshot.value["transition_plan"] = {"present": True}

    with pytest.raises(ValueError, match="prospective_rebase_prepare_control_changed"):
        controller.run("prepare")
    assert not writes and not requests


def test_chunked_reward_totals_use_first_pass_only_no_second_read():
    """P1-2: summing must not re-fetch rewards and double-count across chunks."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows
    from application.rebased_recovery import MAX_HISTORY

    stamp = int((OPENING_AT + timedelta(days=1)).timestamp() * 1000)
    row = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB001",
        "time": stamp,
    }
    reward_reads = []
    end = OPENING_AT + MAX_HISTORY + timedelta(days=1)

    def read(method, path, **kwargs):
        assert method == "get" and kwargs["signed"] is True
        data = kwargs.get("data") or {}
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            start_ms = data.get("startTime")
            end_ms = data.get("endTime")
            reward_reads.append((start_ms, end_ms))
            if type(start_ms) is int and type(end_ms) is int and start_ms <= stamp <= end_ms:
                return {"rows": [row], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=end,
        now=end,
        managed_reward_assets=("BNB", "USDT"),
    )

    assert result["history_complete_for_requested_surfaces"] is True
    assert result["chunk_count"] == 2
    # One rewardsRecord read per chunk surface pass — never a second summing pass.
    assert len(reward_reads) == 2
    assert result["reward_quantity_totals"]["BNB"]["total"] == "0.01"


def test_chunked_reward_sum_preserves_sub_default_precision_addends():
    """Default Decimal prec=28 truncates 0.1+1.4e-29; sums must not quietly drop the tiny addend."""
    from decimal import Decimal, getcontext, localcontext

    from application.broker_reconciliation import diagnose_chunked_balance_flows

    assert getcontext().prec == 28
    assert Decimal("0.1") + Decimal("1.4E-29") == Decimal("0.1")

    t0 = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    t1 = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    rows = [
        {"asset": "BNB", "rewards": "0.1", "type": "REALTIME", "projectId": "BNB001", "time": t0},
        {"asset": "BNB", "rewards": "1.4E-29", "type": "REALTIME", "projectId": "BNB001", "time": t1},
    ]

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            return {"rows": rows, "total": 2}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is True
    total = Decimal(result["reward_quantity_totals"]["BNB"]["total"])
    assert total != Decimal("0.1")
    with localcontext() as context:
        context.prec = 50
        assert total == Decimal("0.1") + Decimal("1.4E-29")


def test_historical_continuity_rejects_ledger_when_reward_sum_loses_tiny_addend(monkeypatch):
    """History 0.1+1.4e-29 must not conserve against a ledger advanced by only 0.1."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    t0 = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    t1 = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    reward_rows = [
        {"asset": "BNB", "rewards": "0.1", "type": "REALTIME", "projectId": "BNB001", "time": t0},
        {"asset": "BNB", "rewards": "1.4E-29", "type": "REALTIME", "projectId": "BNB001", "time": t1},
    ]
    # Ledger/broker advanced only by the truncated 0.1 — exact history requires +1.4e-29 more.
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.2"
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.1"
    cp["assets"]["BNB"]["quantity"] = "3.1"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.1"}

    history_client, _calls = _empty_history_client(
        reward_rows_by_window={
            (int(OPENING_AT.timestamp() * 1000),
             int((OPENING_AT + timedelta(days=7)).timestamp() * 1000)): reward_rows,
        }
    )

    class RewardClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row(total="2.1", rewards="0.2")])

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=RewardClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


@pytest.mark.parametrize(
    "bad_row",
    [
        {"asset": "BNB", "rewards": "0.01", "type": "UNKNOWN", "projectId": "BNB001",
         "time": int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)},
        {"asset": "BNB", "rewards": "0.01", "type": "REALTIME", "projectId": "BNB001",
         "time": "not-int"},
        {"asset": "BNB", "rewards": "-1", "type": "REALTIME", "projectId": "BNB001",
         "time": int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)},
    ],
)
def test_chunked_invalid_reward_rows_fail_closed(bad_row):
    """P1-3: unknown type / illegal timestamp / illegal amount must not be skipped."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    good = {
        "asset": "BNB", "rewards": "0.01", "type": "REALTIME", "projectId": "BNB001",
        "time": stamp,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            return {"rows": [good, bad_row], "total": 2}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] in {
        "balance_history_incomplete",
        "balance_history_reward_validation_failed",
    }
    assert "reward_quantity_totals" not in result


def test_historical_continuity_rejects_tail_activity_between_observed_and_final(
    monkeypatch,
):
    """P1-5: history must cover through final_at so late deposits cannot slip past."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    history_client, _calls = _empty_history_client()
    tail_ms = int(LATER_BEYOND.timestamp() * 1000)

    class TailClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            data = kwargs.get("data") or {}
            end_ms = data.get("endTime")
            if (
                path == "capital/deposit/hisrec"
                and type(end_ms) is int
                and end_ms >= tail_ms
            ):
                return [{
                    "id": "late-dep", "amount": "1", "coin": "USDT", "status": 1,
                    "insertTime": tail_ms, "completeTime": tail_ms,
                    "walletType": 0, "transferType": 0, "txId": "tx-late",
                }]
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_unsupported_activity"):
        collect_historical_continuity_diagnosis(
            client=TailClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


@pytest.mark.parametrize(
    "tamper",
    ["principal", "cursor", "scope", "snapshot"],
)
def test_historical_continuity_rejects_unproven_ledger_fields_when_rewards_nonzero(
    monkeypatch, tamper
):
    """P1-1: reward-nonzero path must verify principal/cursor/scope/snapshot, not digest alone."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    reward_stamp = int((OPENING_AT + timedelta(days=1)).timestamp() * 1000)
    reward_row = {
        "asset": "BNB", "rewards": "0.00000001", "type": "REALTIME",
        "projectId": "BNB001", "time": reward_stamp,
    }
    # Advance ledger checkpoint/snapshot as if the tiny reward was applied.
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.10000001"
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.00000001"
    cp["assets"]["BNB"]["quantity"] = "3.00000001"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": 100.0, "BNB": 3.00000001}
    if tamper == "principal":
        ledger["daily_external_principal_usdt"] = 99.0
    elif tamper == "cursor":
        ledger["external_cash_flow_cursor"] = {
            "version": 1,
            "observed_at": NOW_BEYOND.isoformat(),
            "records": {"a" * 64: {
                "kind": "deposit", "status": "final", "payload_sha256": "b" * 64,
            }},
        }
    elif tamper == "scope":
        ledger["last_balance_snapshot"]["ETH"] = 0.0
        ledger["earn_accrual_checkpoint"]["assets"]["ETH"] = {
            "spot_free": "0", "spot_locked": "0", "quantity": "0", "products": {},
        }
        ledger["earn_accounted_net_changes"]["ETH"] = "0"
    elif tamper == "snapshot":
        ledger["last_balance_snapshot"]["BNB"] = 9.0

    history_client, _calls = _empty_history_client(
        reward_rows_by_window={
            (int(OPENING_AT.timestamp() * 1000),
             int((OPENING_AT + timedelta(days=7)).timestamp() * 1000)): [reward_row],
        }
    )

    class RewardClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(
                asset,
                [_bnb_earn_row(total="2.00000001", rewards="0.10000001")],
            )

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=RewardClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_historical_continuity_rejects_earn_product_or_collateral_change(monkeypatch):
    """P1-4: must reuse strict Earn checkpoint semantics, not totalAmount alone."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    history_client, _calls = _empty_history_client()

    class BrokenEarnClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row(collateral="0.5")])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(
        ValueError,
        match="historical_continuity_(checkpoint_unavailable|conservation_unverified)",
    ):
        collect_historical_continuity_diagnosis(
            client=BrokenEarnClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_validate_historical_continuity_source_require_fresh_false_ignores_age(
    monkeypatch,
):
    """P1-6: require_fresh=False keeps binding checks but skips 30-minute window."""
    from application.rebased_recovery import (
        collect_historical_continuity_source,
        validate_historical_continuity_source,
    )

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    package = collect_historical_continuity_source(
        client=StableClient(),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                    "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                       "head_branch": "main", "event": "workflow_dispatch",
                       "path": ".github/workflows/main.yml"},
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )
    stale_now = LATER_BEYOND + timedelta(minutes=31)
    with pytest.raises(ValueError, match="historical_continuity_candidate_stale"):
        validate_historical_continuity_source(
            package,
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            now=stale_now,
            require_fresh=True,
        )
    candidate = validate_historical_continuity_source(
        package,
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        now=stale_now,
        require_fresh=False,
    )
    assert candidate.candidate_sha256 == package["candidate"]["candidate_sha256"]


def test_historical_continuity_rejects_float_truncated_reward_delta(monkeypatch):
    """A: Decimal exactness — 0.000000014 must not round-accept as 0.00000001."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    reward_stamp = int((OPENING_AT + timedelta(days=1)).timestamp() * 1000)
    reward_row = {
        "asset": "BNB", "rewards": "0.000000014", "type": "REALTIME",
        "projectId": "BNB001", "time": reward_stamp,
    }
    # Truncated 8dp float semantics would wrongly equal opening+reward.
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.10000001"
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.00000001"
    cp["assets"]["BNB"]["quantity"] = "3.00000001"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.00000001"}

    history_client, _calls = _empty_history_client(
        reward_rows_by_window={
            (int(OPENING_AT.timestamp() * 1000),
             int((OPENING_AT + timedelta(days=7)).timestamp() * 1000)): [reward_row],
        }
    )

    class RewardClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(
                asset, [_bnb_earn_row(total="2.00000001", rewards="0.10000001")]
            )

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=RewardClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_historical_continuity_rejects_checkpoint_after_final_at(monkeypatch):
    """A: current checkpoint/evidence must not observe after final_at."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    late = LATER_BEYOND + timedelta(seconds=5)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = late.isoformat()
    ledger["earn_accrual_checkpoint"] = cp
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_(conservation_unverified|observation_timeline_invalid)"):
        collect_historical_continuity_diagnosis(
            client=StableClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                        "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
            migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                           "head_branch": "main", "event": "workflow_dispatch",
                           "path": ".github/workflows/main.yml"},
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_chunked_rejects_reward_product_without_lifecycle_evidence():
    """Reward projectId outside opening set still fails without subscription proof."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    row = {
        "asset": "BNB", "rewards": "0.01", "type": "REALTIME",
        "projectId": "UNAPPROVED_PRODUCT", "time": stamp,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            return {"rows": [row], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"
    assert "reward_quantity_totals" not in result
    counts = result.get("reward_product_classification_counts")
    assert isinstance(counts, dict)
    assert counts.get("lifecycle_evidence_incomplete", 0) >= 1
    assert "UNAPPROVED_PRODUCT" not in str(result)


def test_chunked_accepts_reward_product_proven_by_subscription_lifecycle():
    """Managed asset + SUCCESS subscription evidence may admit a post-opening product."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    sub_stamp = int((OPENING_AT + timedelta(minutes=30)).timestamp() * 1000)
    reward_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 42,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/rewardsRecord"):
            return {"rows": [reward], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is True
    assert result["reward_quantity_totals"]["BNB"]["REALTIME"] == "0.01"
    counts = result["reward_product_classification_counts"]
    assert counts["lifecycle_subscription_proven"] == 1
    assert counts.get("lifecycle_evidence_incomplete", 0) == 0
    public = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_private_")
    }
    assert "BNB002" not in str(public)


def test_chunked_rejects_out_of_scope_reward_product_even_with_subscription():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    subscription = {
        "asset": "ETH",
        "productId": "ETH001",
        "amount": "1",
        "time": stamp - 1,
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "type": "AUTO",
        "purchaseId": 7,
    }
    reward = {
        "asset": "ETH",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "ETH001",
        "time": stamp,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/rewardsRecord"):
            return {"rows": [reward], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] in {
        "balance_history_unapproved_reward_product",
        "balance_history_incomplete",
        "balance_history_reward_validation_failed",
    }
    assert "ETH001" not in str(result)


def test_chunked_covers_final_millisecond_when_span_is_max_chunk_plus_one_ms():
    """C: end = start + 7d + 1ms must still query the inclusive final millisecond."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows
    from application.rebased_recovery import MAX_HISTORY

    end = OPENING_AT + MAX_HISTORY + timedelta(milliseconds=1)
    end_ms = int(end.timestamp() * 1000)
    boundary_ms = int((OPENING_AT + MAX_HISTORY).timestamp() * 1000)
    assert end_ms == boundary_ms + 1
    calls = []
    seen_end = {"hit": False}

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        stop_ms = data.get("endTime")
        calls.append((path, start_ms, stop_ms))
        if path.startswith("capital/"):
            if path.endswith("deposit/hisrec") and type(stop_ms) is int and stop_ms >= end_ms:
                if type(start_ms) is int and start_ms <= end_ms <= stop_ms:
                    seen_end["hit"] = True
                    return [{
                        "id": "edge", "amount": "1", "coin": "USDT", "status": 1,
                        "insertTime": end_ms, "completeTime": end_ms,
                        "walletType": 0, "transferType": 0, "txId": "tx-edge",
                    }]
            return []
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=end,
        now=end,
    )
    assert seen_end["hit"] is True
    assert result["chunk_count"] >= 2
    assert any(
        path.endswith("deposit/hisrec") and stop == end_ms
        for path, _start, stop in calls
    )
    assert result["history_counts"]["deposits"] == 1
    assert result["history_complete_for_requested_surfaces"] is True


@pytest.mark.parametrize(
    "tamper",
    [
        "account_scope",
        "broker_connected",
        "positions_match",
        "runtime_target",
        "observed_at",
        "expected_digest",
    ],
)
def test_validate_require_fresh_false_still_rejects_binding_tamper(monkeypatch, tamper):
    """D: require_fresh=False skips age only — binding/evidence still fail-closed."""
    from quant_platform_kit.common.broker_reconciliation import (
        calculate_broker_reconciliation_evidence_sha256,
    )
    from quant_platform_kit.common.broker_reconciliation_enrollment import (
        calculate_broker_reconciliation_baseline_candidate_sha256,
    )
    from application.rebased_recovery import (
        collect_historical_continuity_source,
        validate_historical_continuity_source,
    )

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    legacy = {"account_scope_sha256": digest({"account_uid": "synthetic"})}
    target = _target()
    package = collect_historical_continuity_source(
        client=StableClient(),
        runtime_target=target,
        legacy_expected=legacy,
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                    "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                       "head_branch": "main", "event": "workflow_dispatch",
                       "path": ".github/workflows/main.yml"},
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )
    broken = copy.deepcopy(package)
    evidence = broken["source"]["reconciled_evidence"]
    candidate = broken["candidate"]

    if tamper == "account_scope":
        evidence["account_scope_sha256"] = "a" * 64
        candidate["account_scope_sha256"] = "a" * 64
    elif tamper == "broker_connected":
        evidence["broker_connected"] = False
    elif tamper == "positions_match":
        evidence["positions_match"] = False
    elif tamper == "runtime_target":
        evidence["runtime_target_sha256"] = "b" * 64
        evidence["baseline_target_sha256"] = "b" * 64
        candidate["baseline_target_sha256"] = "b" * 64
    elif tamper == "observed_at":
        # Desync evidence clock from candidate enrollment timestamps.
        evidence["observed_at"] = (LATER_BEYOND + timedelta(minutes=1)).isoformat()
    elif tamper == "expected_digest":
        evidence["positions_sha256"] = "c" * 64
        # Leave candidate.positions_sha256 unchanged so expected_digests diverge.
    evidence["evidence_sha256"] = "0" * 64
    evidence["evidence_sha256"] = calculate_broker_reconciliation_evidence_sha256(evidence)
    candidate["source_evidence_sha256"] = [evidence["evidence_sha256"]]
    candidate["candidate_sha256"] = "0" * 64
    candidate["candidate_sha256"] = calculate_broker_reconciliation_baseline_candidate_sha256(
        candidate
    )
    broken["source"]["reconciled_evidence"] = evidence
    broken["candidate"] = candidate
    # Keep source_receipts bound to (possibly retargeted) source digest.
    broken["candidate"]["source_receipts_sha256"] = digest(broken["source"])
    broken["candidate"]["candidate_sha256"] = "0" * 64
    broken["candidate"]["candidate_sha256"] = (
        calculate_broker_reconciliation_baseline_candidate_sha256(broken["candidate"])
    )

    with pytest.raises(
        ValueError,
        match="historical_continuity_(source_binding_mismatch|candidate_source_mismatch)",
    ):
        validate_historical_continuity_source(
            broken,
            runtime_target=target,
            legacy_expected=legacy,
            now=LATER_BEYOND + timedelta(minutes=31),
            require_fresh=False,
        )


def test_historical_continuity_accepts_evidenced_cursor_advancement(monkeypatch):
    """Cursor observed_at may advance with identical records when rewards conserve."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    reward_stamp = int((OPENING_AT + timedelta(days=1)).timestamp() * 1000)
    reward_row = {
        "asset": "BNB",
        "rewards": "0.00000001",
        "type": "REALTIME",
        "projectId": "BNB001",
        "time": reward_stamp,
    }
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.10000001"
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.00000001"
    cp["assets"]["BNB"]["quantity"] = "3.00000001"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": 100.0, "BNB": 3.00000001}
    ledger["external_cash_flow_cursor"] = {
        "version": 1,
        "observed_at": NOW_BEYOND.isoformat(),
        "records": copy.deepcopy(
            archive["approved_proposal"]["proposed_fields"]["external_cash_flow_cursor"][
                "records"
            ]
        ),
    }

    history_client, _calls = _empty_history_client(
        reward_rows_by_window={
            (
                int(OPENING_AT.timestamp() * 1000),
                int((OPENING_AT + timedelta(days=7)).timestamp() * 1000),
            ): [reward_row],
        }
    )

    class RewardClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(
                asset,
                [_bnb_earn_row(total="2.00000001", rewards="0.10000001")],
            )

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    result = collect_historical_continuity_diagnosis(
        client=RewardClient(),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={
            "id": 400,
            "head_sha": "b" * 40,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        migration_run={
            "id": 34690695663,
            "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )
    assert result["status"] == "diagnosed"
    assert result["no_order"] is True
    assert result["execution_authority_granted"] is False


def test_historical_continuity_bonus_and_realtime_conserve_separately(monkeypatch):
    """BONUS credits Spot; REALTIME accrues in Earn — both raise total once."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    t_bonus = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    t_real = int((OPENING_AT + timedelta(hours=3)).timestamp() * 1000)
    reward_rows = [
        {
            "asset": "BNB",
            "rewards": "0.01",
            "type": "BONUS",
            "projectId": "BNB001",
            "time": t_bonus,
        },
        {
            "asset": "BNB",
            "rewards": "0.02",
            "type": "REALTIME",
            "projectId": "BNB001",
            "time": t_real,
        },
    ]
    # Spot +0.01 BONUS, Earn total/counter +0.02 REALTIME.
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "1.01"
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.12"
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.02"
    cp["assets"]["BNB"]["quantity"] = "3.03"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.03"}
    ledger["external_cash_flow_cursor"] = {
        "version": 1,
        "observed_at": NOW_BEYOND.isoformat(),
        "records": {},
    }

    history_client, _calls = _empty_history_client(
        reward_rows_by_window={
            (
                int(OPENING_AT.timestamp() * 1000),
                int((OPENING_AT + timedelta(days=7)).timestamp() * 1000),
            ): reward_rows,
        }
    )

    class RewardClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(
                asset, [_bnb_earn_row(total="2.02", rewards="0.12")]
            )

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1.01", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    result = collect_historical_continuity_diagnosis(
        client=RewardClient(),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={
            "id": 400,
            "head_sha": "b" * 40,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        migration_run={
            "id": 34690695663,
            "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )
    assert result["status"] == "diagnosed"
    assert result["no_order"] is True


def test_historical_continuity_rejects_treating_bonus_as_realtime_earn(monkeypatch):
    """Applying BONUS into Earn totals (and not Spot) must not conserve."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    t_bonus = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    reward_rows = [
        {
            "asset": "BNB",
            "rewards": "0.01",
            "type": "BONUS",
            "projectId": "BNB001",
            "time": t_bonus,
        },
    ]
    # Wrong: BONUS applied to Earn product instead of Spot.
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.11"
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.01"
    cp["assets"]["BNB"]["quantity"] = "3.01"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.01"}

    history_client, _calls = _empty_history_client(
        reward_rows_by_window={
            (
                int(OPENING_AT.timestamp() * 1000),
                int((OPENING_AT + timedelta(days=7)).timestamp() * 1000),
            ): reward_rows,
        }
    )

    class RewardClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(
                asset, [_bnb_earn_row(total="2.01", rewards="0.11")]
            )

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=RewardClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_historical_continuity_accepts_lifecycle_product_with_subscription(monkeypatch):
    """Post-opening subscribed product rewards may conserve when evidence is complete."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    sub_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    reward_stamp = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    # Move 1 BNB from spot into new product BNB002, then +0.01 REALTIME on BNB002.
    # Opening: spot 1 + BNB001 2 = 3. After: spot 0 + BNB001 2 + BNB002 1.01 = 3.01
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "0"
    cp["assets"]["BNB"]["products"]["BNB002"] = {
        "total": "1.01",
        "realtime_rewards": "0.01",
        "auto_subscribe": True,
        "can_redeem": True,
    }
    cp["assets"]["BNB"]["quantity"] = "3.01"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.01"}
    ledger["external_cash_flow_cursor"] = {
        "version": 1,
        "observed_at": NOW_BEYOND.isoformat(),
        "records": {},
    }

    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 99,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= sub_stamp <= end_ms:
                return {"rows": [subscription], "total": 1}
            return {"rows": [], "total": 0}
        if path.endswith("/rewardsRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= reward_stamp <= end_ms:
                return {"rows": [reward], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class LifecycleClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            rows = [
                _bnb_earn_row(),
                {
                    "asset": "BNB",
                    "productId": "BNB002",
                    "totalAmount": "1.01",
                    "cumulativeRealTimeRewards": "0.01",
                    "collateralAmount": "0",
                    "autoSubscribe": True,
                    "canRedeem": True,
                },
            ]
            return _earn_page_for_asset(asset, rows)

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "0", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    result = collect_historical_continuity_diagnosis(
        client=LifecycleClient(),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={
            "id": 400,
            "head_sha": "b" * 40,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        migration_run={
            "id": 34690695663,
            "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )
    assert result["status"] == "diagnosed"
    assert result["no_order"] is True
    assert result["execution_authority_granted"] is False


@pytest.mark.parametrize(
    "bad_subscription",
    [
        {"asset": "BNB", "productId": "BNB002", "time": None, "status": "SUCCESS",
         "sourceAccount": "SPOT", "purchaseId": 1, "amount": "1", "type": "AUTO"},
        {"asset": "BNB", "productId": "BNB002",
         "time": int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000),
         "status": "SUCCESS", "sourceAccount": "SPOT", "purchaseId": 1, "type": "AUTO"},
        {"asset": "BNB", "productId": "BNB002", "amount": "-1",
         "time": int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000),
         "status": "SUCCESS", "sourceAccount": "SPOT", "purchaseId": 1, "type": "AUTO"},
        {"asset": "BNB", "productId": "BNB002", "amount": "1",
         "time": int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000),
         "status": "PENDING", "sourceAccount": "SPOT", "purchaseId": 1, "type": "AUTO"},
        {"asset": "BNB", "productId": "BNB002", "amount": "1",
         "time": int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000),
         "status": "SUCCESS", "sourceAccount": "SPOT", "type": "AUTO"},
        {},
    ],
)
def test_chunked_rejects_invalid_subscription_lifecycle_rows(bad_subscription):
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [bad_subscription], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] in {
        "balance_history_incomplete",
        "balance_history_reward_validation_failed",
        "balance_history_duplicate_event",
    }


def test_chunked_rejects_duplicate_subscription_identity():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    row = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": stamp,
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "type": "AUTO",
        "purchaseId": 42,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [row, dict(row)], "total": 2}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] in {
        "balance_history_duplicate_event",
        "balance_history_incomplete",
        "balance_history_reward_validation_failed",
    }


def test_chunked_rejects_reward_before_subscription_for_new_product():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    reward_stamp = int((OPENING_AT + timedelta(minutes=10)).timestamp() * 1000)
    sub_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 42,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/rewardsRecord"):
            return {"rows": [reward], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"


def test_chunked_rejects_later_page_total_disagreement():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    start_ms = int(OPENING_AT.timestamp() * 1000)

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        if path.startswith("capital/"):
            return []
        if path.endswith("/rewardsRecord"):
            current = int(data.get("current") or 1)
            if current == 1:
                return {
                    "rows": [
                        {
                            "asset": "BNB",
                            "type": "REALTIME",
                            "projectId": "BNB001",
                            "time": start_ms + i,
                            "rewards": "0",
                        }
                        for i in range(100)
                    ],
                    "total": 150,
                }
            return {
                "rows": [
                    {
                        "asset": "BNB",
                        "type": "REALTIME",
                        "projectId": "BNB001",
                        "time": start_ms + 100 + i,
                        "rewards": "0",
                    }
                    for i in range(50)
                ],
                "total": 200,
            }
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_incomplete"


@pytest.mark.parametrize(
    "field",
    ["daily_equity_base", "daily_trend_cash_flow_usdt", "daily_trend_risk_base_usdt"],
)
def test_historical_continuity_rejects_unverified_daily_loss_field_change(monkeypatch, field):
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    ledger[field] = float(ledger.get(field) or 0) + 0.01
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=StableClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_historical_continuity_allows_checkpoint_observed_at_without_activity(monkeypatch):
    """No earn activity: checkpoint/cursor timestamps may advance; quantities stay fixed."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    ledger["earn_accrual_checkpoint"] = cp
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    result = collect_historical_continuity_diagnosis(
        client=StableClient(),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={
            "id": 400,
            "head_sha": "b" * 40,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        migration_run={
            "id": 34690695663,
            "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml",
        },
        now=NOW_BEYOND,
        clock=lambda: LATER_BEYOND,
    )
    assert result["status"] == "diagnosed"
    assert result["no_order"] is True
    assert result["execution_authority_granted"] is False


def test_historical_continuity_rejects_product_add_with_only_count_not_matching_fact(
    monkeypatch,
):
    """Subscription count alone cannot authorize a new product without matching facts."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    sub_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "0"
    cp["assets"]["BNB"]["products"]["BNB002"] = {
        "total": "1",
        "realtime_rewards": "0",
        "auto_subscribe": True,
        "can_redeem": True,
    }
    cp["assets"]["BNB"]["quantity"] = "3"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    subscription = {
        "asset": "BNB",
        "productId": "BNB003",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 77,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= sub_stamp <= end_ms:
                return {"rows": [subscription], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class Client(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            rows = [
                _bnb_earn_row(),
                {
                    "asset": "BNB",
                    "productId": "BNB002",
                    "totalAmount": "1",
                    "cumulativeRealTimeRewards": "0",
                    "collateralAmount": "0",
                    "autoSubscribe": True,
                    "canRedeem": True,
                },
            ]
            return _earn_page_for_asset(asset, rows)

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "0", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=Client(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )

def test_historical_continuity_rejects_product_removal_with_underfunded_redemption(
    monkeypatch,
):
    """Removing BNB001 total=2 cannot be diagnosed from a 0.001 redemption alone."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    redeem_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "3"
    cp["assets"]["BNB"]["products"] = {}
    cp["assets"]["BNB"]["quantity"] = "3"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    redemption = {
        "asset": "BNB",
        "projectId": "BNB001",
        "amount": "0.001",
        "time": redeem_stamp,
        "status": "PAID",
        "destAccount": "SPOT",
        "redeemId": 501,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/redemptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= redeem_stamp <= end_ms:
                return {"rows": [redemption], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class RemovalClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [])

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "3", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=RemovalClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_historical_continuity_rejects_shared_product_principal_drop_without_redemption(
    monkeypatch,
):
    """Shared BNB001 principal drop is not covered by adding BNB002 alone."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    sub_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    # BNB001 principal silently drops 1 while BNB002 is funded by a subscription.
    cp["assets"]["BNB"]["products"]["BNB001"]["total"] = "1"
    cp["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.1"
    cp["assets"]["BNB"]["products"]["BNB002"] = {
        "total": "1",
        "realtime_rewards": "0",
        "auto_subscribe": True,
        "can_redeem": True,
    }
    cp["assets"]["BNB"]["spot_free"] = "1"
    cp["assets"]["BNB"]["quantity"] = "3"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 802,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= sub_stamp <= end_ms:
                return {"rows": [subscription], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class MigrationClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            rows = [
                {
                    "asset": "BNB",
                    "productId": "BNB001",
                    "totalAmount": "1",
                    "cumulativeRealTimeRewards": "0.1",
                    "collateralAmount": "0",
                    "autoSubscribe": True,
                    "canRedeem": True,
                },
                {
                    "asset": "BNB",
                    "productId": "BNB002",
                    "totalAmount": "1",
                    "cumulativeRealTimeRewards": "0",
                    "collateralAmount": "0",
                    "autoSubscribe": True,
                    "canRedeem": True,
                },
            ]
            return _earn_page_for_asset(asset, rows)

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "1", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=MigrationClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_chunked_rejects_lifecycle_row_with_product_bound_to_other_asset():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    subscription = {
        "asset": "USDT",
        "productId": "BNB001",
        "amount": "1",
        "time": stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 901,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB", "USDT"),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] in {
        "balance_history_incomplete",
        "balance_history_reward_validation_failed",
    }


@pytest.mark.parametrize(
    "tamper",
    ["account_scope_sha256", "execution_authority_granted"],
)
def test_historical_continuity_rejects_no_activity_nontime_checkpoint_field(
    monkeypatch, tamper
):
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    if tamper == "account_scope_sha256":
        cp["account_scope_sha256"] = "a" * 64
    else:
        cp["execution_authority_granted"] = True
    ledger["earn_accrual_checkpoint"] = cp
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    history_client, _calls = _empty_history_client()

    class StableClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [_bnb_earn_row()])

        def _request_margin_api(self, method, path, **kwargs):
            return history_client._request_margin_api(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=StableClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_chunked_rejects_reward_when_subscription_shares_exact_millisecond():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 42,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": stamp,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/rewardsRecord"):
            return {"rows": [reward], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"


def test_chunked_rejects_transfer_page_identity_repeat_even_when_total_matches():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    start_ms = int(OPENING_AT.timestamp() * 1000)

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        if path.startswith("capital/"):
            return []
        if path == "asset/transfer" and data.get("type") == "MAIN_FUNDING":
            current = int(data.get("current") or 1)
            if current == 1:
                return {
                    "rows": [
                        {
                            "tranId": 1000 + i,
                            "asset": "USDT",
                            "amount": "1",
                            "timestamp": start_ms + i,
                            "status": "CONFIRMED",
                        }
                        for i in range(100)
                    ],
                    "total": 150,
                }
            return {
                "rows": [
                    {
                        "tranId": 1000 + i,
                        "asset": "USDT",
                        "amount": "1",
                        "timestamp": start_ms + i,
                        "status": "CONFIRMED",
                    }
                    for i in range(50)
                ],
                "total": 150,
            }
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_incomplete"
    assert result["failed_surface"] == "transfer_main_funding"


def test_historical_continuity_rejects_removal_redeem_rounded_equal_to_total(
    monkeypatch,
):
    """Removal redeem must not equal product total via Decimal context rounding."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    redeem_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    near_two = "1.99999999999999999999999999999"
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "3"
    cp["assets"]["BNB"]["products"] = {}
    cp["assets"]["BNB"]["quantity"] = "3"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    redemption = {
        "asset": "BNB",
        "projectId": "BNB001",
        "amount": near_two,
        "time": redeem_stamp,
        "status": "PAID",
        "destAccount": "SPOT",
        "redeemId": 551,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/redemptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= redeem_stamp <= end_ms:
                return {"rows": [redemption], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class RemovalClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [])

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "3", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=RemovalClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


@pytest.mark.parametrize(
    "tamper",
    ["account_scope_sha256", "execution_authority_granted"],
)
def test_historical_continuity_rejects_lifecycle_nontime_checkpoint_tamper(
    monkeypatch, tamper
):
    """Valid subscription lifecycle still requires nontime checkpoint fields."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    sub_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    reward_stamp = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "0"
    cp["assets"]["BNB"]["products"]["BNB002"] = {
        "total": "1.01",
        "realtime_rewards": "0.01",
        "auto_subscribe": True,
        "can_redeem": True,
    }
    cp["assets"]["BNB"]["quantity"] = "3.01"
    if tamper == "account_scope_sha256":
        cp["account_scope_sha256"] = "b" * 64
    else:
        cp["execution_authority_granted"] = True
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.01"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 99,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= sub_stamp <= end_ms:
                return {"rows": [subscription], "total": 1}
            return {"rows": [], "total": 0}
        if path.endswith("/rewardsRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= reward_stamp <= end_ms:
                return {"rows": [reward], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class LifecycleClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            rows = [
                _bnb_earn_row(),
                {
                    "asset": "BNB",
                    "productId": "BNB002",
                    "totalAmount": "1.01",
                    "cumulativeRealTimeRewards": "0.01",
                    "collateralAmount": "0",
                    "autoSubscribe": True,
                    "canRedeem": True,
                },
            ]
            return _earn_page_for_asset(asset, rows)

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "0", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=LifecycleClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


@pytest.mark.parametrize(
    "bad_row",
    [
        {
            "asset": "USDT",
            "amount": "1",
            "timestamp": int(OPENING_AT.timestamp() * 1000),
            "status": "CONFIRMED",
        },
        {
            "tranId": "1001",
            "asset": "USDT",
            "amount": "1",
            "timestamp": int(OPENING_AT.timestamp() * 1000),
            "status": "CONFIRMED",
        },
    ],
)
def test_chunked_rejects_transfer_row_without_strict_tran_id(bad_row):
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        if path.startswith("capital/"):
            return []
        if path == "asset/transfer" and data.get("type") == "MAIN_FUNDING":
            return {"rows": [bad_row], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_incomplete"
    assert result["failed_surface"] == "transfer_main_funding"


def test_historical_continuity_rejects_removal_redeem_sum_hiding_extreme_tail(
    monkeypatch,
):
    """2 + 1e-1001 must not conserve as product total 2 under capped Decimal prec."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    redeem_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "3"
    cp["assets"]["BNB"]["products"] = {}
    cp["assets"]["BNB"]["quantity"] = "3"
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    redemptions = [
        {
            "asset": "BNB",
            "projectId": "BNB001",
            "amount": "2",
            "time": redeem_stamp,
            "status": "PAID",
            "destAccount": "SPOT",
            "redeemId": 701,
        },
        {
            "asset": "BNB",
            "projectId": "BNB001",
            "amount": "1E-1001",
            "time": redeem_stamp + 1,
            "status": "PAID",
            "destAccount": "SPOT",
            "redeemId": 702,
        },
    ]

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/redemptionRecord"):
            rows = [
                row
                for row in redemptions
                if type(start_ms) is int
                and type(end_ms) is int
                and start_ms <= row["time"] <= end_ms
            ]
            return {"rows": rows, "total": len(rows)}
        return {"rows": [], "total": 0}

    class RemovalClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            return _earn_page_for_asset(asset, [])

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "3", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(
        ValueError,
        match="historical_continuity_(conservation_unverified|history_incomplete|unapproved_reward_product)",
    ):
        collect_historical_continuity_diagnosis(
            client=RemovalClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


def test_historical_continuity_rejects_lifecycle_execution_authority_zero(
    monkeypatch,
):
    """Integer 0 must not satisfy execution_authority_granted is False."""
    from application.rebased_recovery import collect_historical_continuity_diagnosis

    ledger, archive, _control = _material(monkeypatch)
    ledger = copy.deepcopy(ledger)
    sub_stamp = int((OPENING_AT + timedelta(hours=1)).timestamp() * 1000)
    reward_stamp = int((OPENING_AT + timedelta(hours=2)).timestamp() * 1000)
    cp = copy.deepcopy(ledger["earn_accrual_checkpoint"])
    cp["observed_at"] = NOW_BEYOND.isoformat()
    cp["assets"]["BNB"]["spot_free"] = "0"
    cp["assets"]["BNB"]["products"]["BNB002"] = {
        "total": "1.01",
        "realtime_rewards": "0.01",
        "auto_subscribe": True,
        "can_redeem": True,
    }
    cp["assets"]["BNB"]["quantity"] = "3.01"
    cp["execution_authority_granted"] = 0
    ledger["earn_accrual_checkpoint"] = cp
    ledger["last_balance_snapshot"] = {"USDT": "100", "BNB": "3.01"}
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)

    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 99,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }

    def read(method, path, **kwargs):
        data = kwargs.get("data") or {}
        start_ms = data.get("startTime")
        end_ms = data.get("endTime")
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= sub_stamp <= end_ms:
                return {"rows": [subscription], "total": 1}
            return {"rows": [], "total": 0}
        if path.endswith("/rewardsRecord"):
            if type(start_ms) is int and type(end_ms) is int and start_ms <= reward_stamp <= end_ms:
                return {"rows": [reward], "total": 1}
            return {"rows": [], "total": 0}
        return {"rows": [], "total": 0}

    class LifecycleClient(ProspectiveClient):
        def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
            rows = [
                _bnb_earn_row(),
                {
                    "asset": "BNB",
                    "productId": "BNB002",
                    "totalAmount": "1.01",
                    "cumulativeRealTimeRewards": "0.01",
                    "collateralAmount": "0",
                    "autoSubscribe": True,
                    "canRedeem": True,
                },
            ]
            return _earn_page_for_asset(asset, rows)

        def get_account(self):
            return {
                "uid": "synthetic",
                "balances": [
                    {"asset": "USDT", "free": "100", "locked": "0"},
                    {"asset": "BNB", "free": "0", "locked": "0"},
                ],
            }

        def _request_margin_api(self, method, path, **kwargs):
            return read(method, path, **kwargs)

    with pytest.raises(ValueError, match="historical_continuity_conservation_unverified"):
        collect_historical_continuity_diagnosis(
            client=LifecycleClient(),
            runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            ledger=ledger,
            archive=archive,
            symbols=("BNBUSDT",),
            source_run={
                "id": 400,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            migration_run={
                "id": 34690695663,
                "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/main.yml",
            },
            now=NOW_BEYOND,
            clock=lambda: LATER_BEYOND,
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "post_liquidation_reward",
        "redeem_before_subscribe",
        "same_ms_subscribe_reward",
    ],
)
def test_chunked_rejects_lifecycle_order_and_post_liquidation_rewards(scenario):
    """Rewards need a strictly earlier positive product balance; same-ms sub fails."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    sub_stamp = int((OPENING_AT + timedelta(minutes=10)).timestamp() * 1000)
    mid_stamp = int((OPENING_AT + timedelta(minutes=20)).timestamp() * 1000)
    reward_stamp = int((OPENING_AT + timedelta(minutes=30)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 11,
    }
    redemption = {
        "asset": "BNB",
        "projectId": "BNB002",
        "amount": "1",
        "time": mid_stamp,
        "status": "PAID",
        "destAccount": "SPOT",
        "redeemId": 12,
        "type": "FAST",
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }
    rows = {
        "/subscriptionRecord": [subscription],
        "/redemptionRecord": [],
        "/rewardsRecord": [reward],
    }
    if scenario == "post_liquidation_reward":
        rows["/redemptionRecord"] = [redemption]
    elif scenario == "redeem_before_subscribe":
        rows["/redemptionRecord"] = [dict(redemption, time=sub_stamp - 1, redeemId=13)]
    else:
        rows["/subscriptionRecord"] = [dict(subscription, time=reward_stamp)]
        rows["/rewardsRecord"] = [dict(reward, time=reward_stamp)]

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        for suffix, payload in rows.items():
            if path.endswith(suffix):
                return {"rows": payload, "total": len(payload)}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"
    counts = result["reward_product_classification_counts"]
    assert counts["lifecycle_subscription_proven"] == 0
    assert counts.get("lifecycle_evidence_incomplete", 0) >= 1
    public = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_private_")
    }
    assert "BNB002" not in str(public)


def test_chunked_rejects_product_gap_masked_by_sibling_product_balance():
    """Sibling product inventory must not cover another product's post-liquidation reward."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    sub_a = int((OPENING_AT + timedelta(minutes=5)).timestamp() * 1000)
    sub_b = int((OPENING_AT + timedelta(minutes=6)).timestamp() * 1000)
    redeem_a = int((OPENING_AT + timedelta(minutes=15)).timestamp() * 1000)
    reward_a = int((OPENING_AT + timedelta(minutes=25)).timestamp() * 1000)
    subscriptions = [
        {
            "asset": "BNB",
            "productId": "BNB002",
            "amount": "1",
            "time": sub_a,
            "type": "AUTO",
            "status": "SUCCESS",
            "sourceAccount": "SPOT",
            "purchaseId": 21,
        },
        {
            "asset": "BNB",
            "productId": "BNB003",
            "amount": "1",
            "time": sub_b,
            "type": "AUTO",
            "status": "SUCCESS",
            "sourceAccount": "SPOT",
            "purchaseId": 22,
        },
    ]
    redemption = {
        "asset": "BNB",
        "projectId": "BNB002",
        "amount": "1",
        "time": redeem_a,
        "status": "PAID",
        "destAccount": "SPOT",
        "redeemId": 23,
        "type": "FAST",
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_a,
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": subscriptions, "total": 2}
        if path.endswith("/redemptionRecord"):
            return {"rows": [redemption], "total": 1}
        if path.endswith("/rewardsRecord"):
            return {"rows": [reward], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"
    assert result["reward_product_classification_counts"]["lifecycle_subscription_proven"] == 0
    public = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_private_")
    }
    assert "BNB002" not in str(public)
    assert "BNB003" not in str(public)


def test_chunked_rejects_over_redemption_without_reporting_amounts():
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    sub_stamp = int((OPENING_AT + timedelta(minutes=10)).timestamp() * 1000)
    redeem_stamp = int((OPENING_AT + timedelta(minutes=20)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 31,
    }
    redemption = {
        "asset": "BNB",
        "projectId": "BNB002",
        "amount": "2",
        "time": redeem_stamp,
        "status": "PAID",
        "destAccount": "SPOT",
        "redeemId": 32,
        "type": "FAST",
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/redemptionRecord"):
            return {"rows": [redemption], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"
    public = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_private_")
    }
    assert "BNB002" not in str(public)
    assert "purchaseId" not in str(public)
    assert "redeemId" not in str(public)


def test_chunked_rejects_over_redeem_after_tiny_redeem_without_decimal_rounding():
    """Subscribe 1, redeem 1e-40, then redeem 1 must fail; default Decimal prec must not hide remainder."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    sub_stamp = int((OPENING_AT + timedelta(minutes=10)).timestamp() * 1000)
    tiny_stamp = int((OPENING_AT + timedelta(minutes=20)).timestamp() * 1000)
    full_stamp = int((OPENING_AT + timedelta(minutes=30)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 41,
    }
    redemptions = [
        {
            "asset": "BNB",
            "projectId": "BNB002",
            "amount": "1E-40",
            "time": tiny_stamp,
            "status": "PAID",
            "destAccount": "SPOT",
            "redeemId": 42,
            "type": "FAST",
        },
        {
            "asset": "BNB",
            "projectId": "BNB002",
            "amount": "1",
            "time": full_stamp,
            "status": "PAID",
            "destAccount": "SPOT",
            "redeemId": 43,
            "type": "FAST",
        },
    ]

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/redemptionRecord"):
            return {"rows": redemptions, "total": 2}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"
    counts = result["reward_product_classification_counts"]
    assert counts["lifecycle_subscription_proven"] == 0
    assert counts["opening_approved"] == 0
    assert counts.get("lifecycle_evidence_incomplete", 0) >= 1
    public = {
        key: value
        for key, value in result.items()
        if not str(key).startswith("_private_")
    }
    assert "1E-40" not in str(public)
    assert "BNB002" not in str(public)


def test_chunked_failure_classification_excludes_prior_success_counts():
    """A later over-redeem must not leave lifecycle_subscription_proven from earlier rewards."""
    from application.broker_reconciliation import diagnose_chunked_balance_flows

    sub_stamp = int((OPENING_AT + timedelta(minutes=5)).timestamp() * 1000)
    reward_stamp = int((OPENING_AT + timedelta(minutes=15)).timestamp() * 1000)
    redeem_stamp = int((OPENING_AT + timedelta(minutes=25)).timestamp() * 1000)
    subscription = {
        "asset": "BNB",
        "productId": "BNB002",
        "amount": "1",
        "time": sub_stamp,
        "type": "AUTO",
        "status": "SUCCESS",
        "sourceAccount": "SPOT",
        "purchaseId": 51,
    }
    reward = {
        "asset": "BNB",
        "rewards": "0.01",
        "type": "REALTIME",
        "projectId": "BNB002",
        "time": reward_stamp,
    }
    redemption = {
        "asset": "BNB",
        "projectId": "BNB002",
        "amount": "2",
        "time": redeem_stamp,
        "status": "PAID",
        "destAccount": "SPOT",
        "redeemId": 52,
        "type": "FAST",
    }

    def read(method, path, **kwargs):
        if path.startswith("capital/"):
            return []
        if path.endswith("/subscriptionRecord"):
            return {"rows": [subscription], "total": 1}
        if path.endswith("/rewardsRecord"):
            return {"rows": [reward], "total": 1}
        if path.endswith("/redemptionRecord"):
            return {"rows": [redemption], "total": 1}
        return {"rows": [], "total": 0}

    result = diagnose_chunked_balance_flows(
        SimpleNamespace(_request_margin_api=read),
        start=OPENING_AT,
        end=OPENING_AT + timedelta(days=1),
        now=OPENING_AT + timedelta(days=1),
        managed_reward_assets=("BNB",),
        approved_reward_products={("BNB", "BNB001")},
    )
    assert result["history_complete_for_requested_surfaces"] is False
    assert result["reason_code"] == "balance_history_unapproved_reward_product"
    counts = result["reward_product_classification_counts"]
    assert counts["lifecycle_subscription_proven"] == 0
    assert counts["opening_approved"] == 0
    assert counts.get("lifecycle_evidence_incomplete", 0) >= 1
    assert "reward_quantity_totals" not in result
