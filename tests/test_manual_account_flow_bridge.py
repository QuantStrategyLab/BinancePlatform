import copy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from binance_platform.application.broker_reconciliation import collect_manual_account_flows
from binance_platform.application.earn_accrual import prepare_forward_earn_state
from binance_platform.application.interval_source_receipt_candidate import _payload_material
from binance_platform.application.portfolio_service import maybe_rebase_daily_state_for_balance_change
from binance_platform.runtime_support import ExecutionIntegrityError, reconcile_runtime_cash_effects
from tests.test_forward_earn_accounting import NOW, consume, materials


START = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 12, 14, 1, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)


class _ManualClient:
    def __init__(self, *, convert=None, transfers=None, dribblet=None, dividends=None):
        self.convert = convert if convert is not None else {"list": [], "moreData": False}
        self.transfers = transfers or {}
        self.dribblet = dribblet if dribblet is not None else {"userAssetDribblets": []}
        self.dividends = dividends if dividends is not None else {"total": 0, "rows": []}
        self.calls = []

    def _request_margin_api(self, method, path, **kwargs):
        assert method == "get"
        assert kwargs["signed"] is True
        data = kwargs.get("data") or {}
        self.calls.append((path, dict(data)))
        if path == "convert/tradeFlow":
            return self.convert
        if path == "asset/transfer":
            return self.transfers.get(data["type"], {"total": 0})
        if path == "asset/dribblet":
            return self.dribblet
        if path == "asset/assetDividend":
            if data.get("asset") == "BNB":
                bnb_rows = [row for row in self.dividends.get("rows", []) if row.get("asset") == "BNB"]
                return {"total": len(bnb_rows), "rows": bnb_rows}
            return self.dividends
        raise AssertionError(f"unexpected path: {path}")


def test_collect_manual_flows_convert_usdt_boundary_and_out_of_scope_cases():
    client = _ManualClient(
        convert={
            "list": [
                {
                    "orderId": "c1",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "USDC",
                    "fromAmount": "50",
                    "toAsset": "USDT",
                    "toAmount": "50",
                    "createTime": END_MS,
                },
                {
                    "orderId": "c2",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "USDT",
                    "fromAmount": "10",
                    "toAsset": "FDUSD",
                    "toAmount": "10",
                    "createTime": END_MS,
                },
                {
                    "orderId": "c4",
                    "orderStatus": "FAIL",
                    "fromAsset": "USDC",
                    "fromAmount": "999",
                    "toAsset": "USDT",
                    "toAmount": "999",
                    "createTime": END_MS,
                },
                {
                    "orderId": "c5",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "SOL",
                    "fromAmount": "1",
                    "toAsset": "XRP",
                    "toAmount": "100",
                    "createTime": END_MS,
                },
            ],
            "moreData": False,
        }
    )

    result = collect_manual_account_flows(
        client, start=START, end=END, tracked_assets=("USDT", "BTC", "BNB")
    )

    assert result["asset_deltas"] == {"USDT": Decimal("40")}
    assert result["usdt_external_principal"] == Decimal("40")
    assert result["convert_count"] == 2
    assert len(result["identities"]) == 2


def test_collect_manual_flows_transfers_dust_and_usdt_bnb_dividends():
    client = _ManualClient(
        transfers={
            "FUNDING_MAIN": {
                "total": 2,
                "rows": [
                    {"tranId": 11, "asset": "USDT", "amount": "25", "timestamp": END_MS, "status": "CONFIRMED"},
                    {"tranId": 12, "asset": "USDT", "amount": "9", "timestamp": END_MS, "status": "FAILED"},
                ],
            },
            "MAIN_FUNDING": {
                "total": 1,
                "rows": [
                    {"tranId": 13, "asset": "USDT", "amount": "5", "timestamp": END_MS, "status": "CONFIRMED"},
                ],
            },
            "UMFUTURE_MAIN": {
                "total": 1,
                "rows": [
                    {"tranId": 14, "asset": "SOL", "amount": "1", "timestamp": END_MS, "status": "CONFIRMED"},
                ],
            },
        },
        dribblet={
            "total": 1,
            "userAssetDribblets": [
                {
                    "transId": 21,
                    "operateTime": END_MS,
                    "totalTransferedAmount": "0.005",
                    "userAssetDribbletDetails": [
                        {"fromAsset": "USDT", "targetAsset": "BNB", "amount": "0.5"},
                    ],
                }
            ],
        },
        dividends={
            "total": 3,
            "rows": [
                {"id": 1, "tranId": 101, "asset": "USDT", "divTime": END_MS, "amount": "1.25", "direction": 1},
                {"id": 2, "tranId": 102, "asset": "BNB", "divTime": END_MS, "amount": "0.001", "direction": 1},
                {"id": 3, "tranId": 103, "asset": "SOL", "divTime": END_MS, "amount": "2", "direction": 1},
            ],
        },
    )

    result = collect_manual_account_flows(
        client, start=START, end=END, tracked_assets=("USDT", "BTC", "BNB")
    )

    assert result["asset_deltas"] == {
        "USDT": Decimal("20.75"),
        "BNB": Decimal("0.006"),
    }
    assert result["usdt_external_principal"] == Decimal("20")
    assert result["transfer_count"] == 2
    assert result["dribblet_count"] == 1
    assert result["dividend_count"] == 2
    assert len(result["identities"]) == 5


@pytest.mark.parametrize(
    "kind,client_kwargs",
    [
        (
            "convert_tracked_btc_to_usdt",
            {
                "convert": {
                    "list": [
                        {
                            "orderId": "c_btc",
                            "orderStatus": "SUCCESS",
                            "fromAsset": "BTC",
                            "fromAmount": "0.001",
                            "toAsset": "USDT",
                            "toAmount": "60",
                            "createTime": END_MS,
                        }
                    ],
                    "moreData": False,
                }
            },
        ),
        (
            "convert_tracked_eth_to_usdt",
            {
                "convert": {
                    "list": [
                        {
                            "orderId": "c_eth",
                            "orderStatus": "SUCCESS",
                            "fromAsset": "ETH",
                            "fromAmount": "0.5",
                            "toAsset": "USDT",
                            "toAmount": "1000",
                            "createTime": END_MS,
                        }
                    ],
                    "moreData": False,
                }
            },
        ),
        (
            "convert_untracked_to_tracked_bnb",
            {
                "convert": {
                    "list": [
                        {
                            "orderId": "c_bnb",
                            "orderStatus": "SUCCESS",
                            "fromAsset": "USDC",
                            "fromAmount": "10",
                            "toAsset": "BNB",
                            "toAmount": "0.02",
                            "createTime": END_MS,
                        }
                    ],
                    "moreData": False,
                }
            },
        ),
        (
            "transfer_tracked_btc",
            {
                "transfers": {
                    "UMFUTURE_MAIN": {
                        "total": 1,
                        "rows": [
                            {"tranId": 14, "asset": "BTC", "amount": "0.02", "timestamp": END_MS, "status": "CONFIRMED"},
                        ],
                    }
                }
            },
        ),
        (
            "transfer_tracked_bnb",
            {
                "transfers": {
                    "MAIN_FUNDING": {
                        "total": 1,
                        "rows": [
                            {"tranId": 15, "asset": "BNB", "amount": "1", "timestamp": END_MS, "status": "CONFIRMED"},
                        ],
                    }
                }
            },
        ),
        (
            "dribblet_untracked_source_to_bnb",
            {
                "dribblet": {
                    "total": 1,
                    "userAssetDribblets": [
                        {
                            "transId": 22,
                            "operateTime": END_MS,
                            "totalTransferedAmount": "0.005",
                            "userAssetDribbletDetails": [
                                {"fromAsset": "DOGE", "targetAsset": "BNB", "amount": "1"},
                            ],
                        }
                    ],
                }
            },
        ),
        (
            "dribblet_tracked_eth_source_to_bnb",
            {
                "dribblet": {
                    "total": 1,
                    "userAssetDribblets": [
                        {
                            "transId": 23,
                            "operateTime": END_MS,
                            "totalTransferedAmount": "0.005",
                            "userAssetDribbletDetails": [
                                {"fromAsset": "ETH", "targetAsset": "BNB", "amount": "0.001"},
                            ],
                        }
                    ],
                }
            },
        ),
        (
            "dividend_tracked_btc",
            {
                "dividends": {
                    "total": 1,
                    "rows": [
                        {"id": 4, "tranId": 104, "asset": "BTC", "divTime": END_MS, "amount": "0.001", "direction": 1}
                    ],
                }
            },
        ),
    ],
)
def test_collect_manual_flows_rejects_unvalued_non_usdt_and_sleeve_mutating_flows(kind, client_kwargs):
    client = _ManualClient(**client_kwargs)
    with pytest.raises(ValueError, match="manual_flow_scope_unsupported"):
        collect_manual_account_flows(
            client, start=START, end=END, tracked_assets=("USDT", "BTC", "ETH", "BNB")
        )


def test_collect_manual_flows_half_open_boundary_excludes_start_and_includes_end():
    client = _ManualClient(
        convert={
            "list": [
                {
                    "orderId": "at_start",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "USDC",
                    "fromAmount": "10",
                    "toAsset": "USDT",
                    "toAmount": "10",
                    "createTime": START_MS,
                },
                {
                    "orderId": "at_end",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "USDC",
                    "fromAmount": "15",
                    "toAsset": "USDT",
                    "toAmount": "15",
                    "createTime": END_MS,
                },
            ],
            "moreData": False,
        },
        transfers={
            "FUNDING_MAIN": {
                "total": 1,
                "rows": [
                    {"tranId": 1, "asset": "USDT", "amount": "99", "timestamp": START_MS, "status": "CONFIRMED"},
                ],
            }
        },
        dribblet={
            "total": 1,
            "userAssetDribblets": [
                {
                    "transId": 2,
                    "operateTime": START_MS,
                    "totalTransferedAmount": "0.1",
                    "userAssetDribbletDetails": [{"fromAsset": "USDT", "amount": "0.2"}],
                }
            ],
        },
        dividends={
            "total": 1,
            "rows": [
                {"id": 3, "tranId": 4, "asset": "USDT", "divTime": START_MS, "amount": "5", "direction": 1}
            ],
        },
    )

    result = collect_manual_account_flows(client, start=START, end=END, tracked_assets=("USDT", "BNB"))

    assert result["asset_deltas"] == {"USDT": Decimal("15")}
    assert result["usdt_external_principal"] == Decimal("15")
    assert result["convert_count"] == 1
    assert result["transfer_count"] == 0
    assert result["dribblet_count"] == 0
    assert result["dividend_count"] == 0


@pytest.mark.parametrize(
    "kind,client_kwargs,expected",
    [
        (
            "convert_unsettled",
            {
                "convert": {
                    "list": [
                        {
                            "orderId": "p1",
                            "orderStatus": "PROCESS",
                            "fromAsset": "USDC",
                            "fromAmount": "10",
                            "toAsset": "USDT",
                            "toAmount": "10",
                            "createTime": END_MS,
                        }
                    ],
                    "moreData": False,
                }
            },
            "manual_flow_unsettled",
        ),
        (
            "transfer_pending",
            {
                "transfers": {
                    "FUNDING_MAIN": {
                        "total": 1,
                        "rows": [
                            {"tranId": 1, "asset": "USDT", "amount": "10", "timestamp": END_MS, "status": "PENDING"}
                        ],
                    }
                }
            },
            "manual_flow_unsettled",
        ),
        ("convert_more_data", {"convert": {"list": [], "moreData": True}}, "manual_flow_history_incomplete"),
        (
            "transfer_full",
            {
                "transfers": {
                    "FUNDING_MAIN": {
                        "total": 100,
                        "rows": [
                            {"tranId": i, "asset": "USDT", "amount": "1", "timestamp": END_MS, "status": "CONFIRMED"}
                            for i in range(100)
                        ],
                    }
                }
            },
            "manual_flow_history_incomplete",
        ),
        (
            "dribblet_missing_total",
            {
                "dribblet": {
                    "userAssetDribblets": [
                        {
                            "transId": 1,
                            "operateTime": END_MS,
                            "totalTransferedAmount": "0.1",
                            "userAssetDribbletDetails": [{"fromAsset": "USDT", "amount": "0.2"}],
                        }
                    ]
                }
            },
            "manual_flow_history_incomplete",
        ),
        ("dividend_missing_total", {"dividends": {"rows": []}}, "manual_flow_history_incomplete"),
        (
            "out_of_window",
            {
                "dividends": {
                    "total": 1,
                    "rows": [
                        {"id": 1, "tranId": 2, "asset": "USDT", "divTime": END_MS + 1, "amount": "1", "direction": 1}
                    ],
                }
            },
            "manual_flow_row_invalid",
        ),
        (
            "duplicate_convert",
            {
                "convert": {
                    "list": [
                        {
                            "orderId": "dup",
                            "orderStatus": "SUCCESS",
                            "fromAsset": "USDC",
                            "fromAmount": "1",
                            "toAsset": "USDT",
                            "toAmount": "1",
                            "createTime": END_MS,
                        },
                        {
                            "orderId": "dup",
                            "orderStatus": "SUCCESS",
                            "fromAsset": "USDC",
                            "fromAmount": "1",
                            "toAsset": "USDT",
                            "toAmount": "1",
                            "createTime": END_MS,
                        },
                    ],
                    "moreData": False,
                }
            },
            "manual_flow_row_invalid",
        ),
    ],
)
def test_collect_manual_flows_fail_closed_guards(kind, client_kwargs, expected):
    client = _ManualClient(**client_kwargs)
    with pytest.raises(ValueError, match=expected):
        collect_manual_account_flows(client, start=START, end=END, tracked_assets=("USDT", "BNB"))


def test_collect_manual_flows_masks_provider_errors():
    class BrokenClient:
        def _request_margin_api(self, *_args, **_kwargs):
            raise RuntimeError("secret-provider-key")

    with pytest.raises(ValueError) as exc_info:
        collect_manual_account_flows(BrokenClient(), start=START, end=END, tracked_assets=("USDT",))

    assert str(exc_info.value) == "manual_flow_read_failed"
    assert exc_info.value.__suppress_context__


def test_prepare_forward_earn_state_consumes_verified_manual_flows_and_rejects_unmatched():
    state, current, cash = materials()
    state["daily_external_principal_usdt"] = 10.0
    current["assets"]["USDT"].update(spot_free="150", quantity="150")
    cash["manual_asset_deltas"] = {"USDT": "50"}
    cash["manual_external_principal_usdt"] = "50"

    updated = prepare_forward_earn_state(state, current, cash)
    assert updated["daily_external_principal_usdt"] == 60.0
    assert updated["earn_accounted_net_changes"] == {"USDT": "0", "BNB": "0"}

    # Unmatched delta must still raise and never mutate state.
    bad_cash = copy.deepcopy(cash)
    bad_cash["manual_asset_deltas"] = {"USDT": "49"}
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match="earn_quantity_change_unexplained"):
        prepare_forward_earn_state(state, current, bad_cash)
    assert state == before


def test_portfolio_fallback_reconciles_convert_plus_bnb_dividend_without_double_counting():
    state, current, cash = materials()
    state["daily_external_principal_usdt"] = 0.0
    # Simultaneous BNB dividend (+0.00000001) and USDC->USDT convert (+25).
    current["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.1"
    current["assets"]["USDT"].update(spot_free="125", quantity="125")
    client = _ManualClient(
        convert={
            "list": [
                {
                    "orderId": "c1",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "USDC",
                    "fromAmount": "25",
                    "toAsset": "USDT",
                    "toAmount": "25",
                    "createTime": int(NOW.timestamp() * 1000),
                }
            ],
            "moreData": False,
        },
        dividends={
            "total": 1,
            "rows": [
                {
                    "id": 7,
                    "tranId": 8,
                    "asset": "BNB",
                    "divTime": int(NOW.timestamp() * 1000),
                    "amount": "0.00000001",
                    "direction": 1,
                }
            ],
        },
    )

    report = {}
    runtime = SimpleNamespace(client=client, now_utc=NOW, earn_accrual_observation=current)
    snapshot = {a: round(float(r["quantity"]), 8) for a, r in current["assets"].items()}

    changed = maybe_rebase_daily_state_for_balance_change(
        state,
        runtime,
        report,
        1625.000005,
        0.0,
        snapshot,
        [],
        collect_external_cash_flows_fn=lambda *_args, **_kwargs: cash,
        runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
        append_log_fn=lambda *_args: None,
        translate_fn=lambda *_args, **_kwargs: "",
    )

    assert changed is True
    assert state["earn_accrual_checkpoint"] == current
    assert state["daily_external_principal_usdt"] == 25.0
    assert report["external_cash_flow_interval"]["net_external_cash_flow"] == "25"
    assert report["diagnostics"]["earn_accrual"] == {"status": "reconciled"}


def test_portfolio_fallback_outbound_transfer_records_negative_principal_and_valid_interval():
    state, current, cash = materials()
    state["daily_external_principal_usdt"] = 100.0
    current["assets"]["USDT"].update(spot_free="60", quantity="60")
    client = _ManualClient(
        transfers={
            "MAIN_FUNDING": {
                "total": 1,
                "rows": [
                    {
                        "tranId": 88,
                        "asset": "USDT",
                        "amount": "40",
                        "timestamp": int(NOW.timestamp() * 1000),
                        "status": "CONFIRMED",
                    }
                ],
            }
        }
    )

    report = {}
    runtime = SimpleNamespace(client=client, now_utc=NOW, earn_accrual_observation=current)
    snapshot = {a: round(float(r["quantity"]), 8) for a, r in current["assets"].items()}
    maybe_rebase_daily_state_for_balance_change(
        state,
        runtime,
        report,
        1560.000005,
        0.0,
        snapshot,
        [],
        collect_external_cash_flows_fn=lambda *_args, **_kwargs: cash,
        runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
        append_log_fn=lambda *_args: None,
        translate_fn=lambda *_args, **_kwargs: "",
    )

    assert state["daily_external_principal_usdt"] == 60.0
    assert report["external_cash_flow_interval"]["net_external_cash_flow"] == "-40"
    _payload_material(report["external_cash_flow_interval"])


def test_portfolio_fallback_unsettled_convert_surfaces_safe_reason_code():
    state, current, cash = materials()
    current["assets"]["USDT"].update(spot_free="120", quantity="120")
    before = copy.deepcopy(state)
    client = _ManualClient(
        convert={
            "list": [
                {
                    "orderId": "c1",
                    "orderStatus": "PROCESS",
                    "fromAsset": "USDC",
                    "fromAmount": "20",
                    "toAsset": "USDT",
                    "toAmount": "20",
                    "createTime": int(NOW.timestamp() * 1000),
                }
            ],
            "moreData": False,
        }
    )
    report = {}
    runtime = SimpleNamespace(client=client, now_utc=NOW, earn_accrual_observation=current)
    snapshot = {a: round(float(r["quantity"]), 8) for a, r in current["assets"].items()}

    with pytest.raises(ExecutionIntegrityError):
        maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            1620.0,
            0.0,
            snapshot,
            [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: cash,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda *_args, **_kwargs: "",
        )

    assert state == before
    assert report["diagnostics"]["earn_accrual"] == {
        "status": "blocked",
        "reason_code": "manual_flow_unsettled",
    }


def test_portfolio_fallback_tracked_non_usdt_convert_blocks_with_scope_unsupported():
    state, current, cash = materials()
    state["earn_accrual_checkpoint"]["assets"]["ETH"] = {
        "spot_free": "1",
        "spot_locked": "0",
        "products": {},
        "quantity": "1",
    }
    state["earn_accounted_net_changes"]["ETH"] = "0"
    state["last_balance_snapshot"]["ETH"] = 1.0
    current["assets"]["ETH"] = {
        "spot_free": "0",
        "spot_locked": "0",
        "products": {},
        "quantity": "0",
    }
    current["assets"]["USDT"].update(spot_free="2100", quantity="2100")
    before = copy.deepcopy(state)
    client = _ManualClient(
        convert={
            "list": [
                {
                    "orderId": "c_eth_sell",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "ETH",
                    "fromAmount": "1",
                    "toAsset": "USDT",
                    "toAmount": "2000",
                    "createTime": int(NOW.timestamp() * 1000),
                }
            ],
            "moreData": False,
        }
    )
    report = {}
    runtime = SimpleNamespace(client=client, now_utc=NOW, earn_accrual_observation=current)
    snapshot = {a: round(float(r["quantity"]), 8) for a, r in current["assets"].items()}

    with pytest.raises(ExecutionIntegrityError):
        maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            1600.0,
            0.0,
            snapshot,
            [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: cash,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda *_args, **_kwargs: "",
        )

    assert state == before
    assert report["diagnostics"]["earn_accrual"] == {
        "status": "blocked",
        "reason_code": "manual_flow_scope_unsupported",
    }


def test_cash_refresh_bridges_manual_convert_without_advancing_checkpoint(monkeypatch):
    state, current, cash = materials()
    current["assets"]["USDT"].update(spot_free="150", quantity="150")
    before_checkpoint = copy.deepcopy(state["earn_accrual_checkpoint"])
    before_cursor = copy.deepcopy(state["external_cash_flow_cursor"])
    client = _ManualClient(
        convert={
            "list": [
                {
                    "orderId": "c1",
                    "orderStatus": "SUCCESS",
                    "fromAsset": "USDC",
                    "fromAmount": "50",
                    "toAsset": "USDT",
                    "toAmount": "50",
                    "createTime": int(NOW.timestamp() * 1000),
                }
            ],
            "moreData": False,
        }
    )
    monkeypatch.setattr("binance_platform.application.earn_accrual.collect_earn_checkpoint", lambda *a, **kw: current)
    runtime = SimpleNamespace(
        client=client,
        state_owner_held=True,
        state_owner_id="synthetic",
        fuel_symbol="BNBUSDT",
        pending_funds=[{"asset": "BNB", "confirmed": True}],
    )

    reconcile_runtime_cash_effects(runtime, state)

    assert state["earn_accrual_checkpoint"] == before_checkpoint
    assert state["external_cash_flow_cursor"] == before_cursor
    assert runtime.cash_balance_observation["USDT"] == 150.0
    consume(state, current, cash, client=client)
    assert state["earn_accrual_checkpoint"] == current
    assert state["daily_external_principal_usdt"] == 50.0
