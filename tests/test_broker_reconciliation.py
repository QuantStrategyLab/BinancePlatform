from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from application.broker_reconciliation import (
    BinanceReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
from quant_platform_kit.common.runtime_target import build_runtime_target


class _Client:
    def get_account(self):
        return {
            "uid": "123456789",
            "balances": [
                {"asset": "BTC", "free": "0.1", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "1"},
            ],
        }

    def get_open_orders(self):
        return [
            {
                "orderId": 123,
                "symbol": "BTCUSDT",
                "status": "NEW",
                "side": "BUY",
                "type": "LIMIT",
                "origQty": "0.01",
                "executedQty": "0",
                "updateTime": 1,
            }
        ]

    def get_my_trades(self, *, symbol, startTime, endTime, limit):
        assert symbol == "BTCUSDT"
        assert startTime < endTime
        assert limit == 1000
        return [
            {
                "id": 1,
                "orderId": 2,
                "symbol": symbol,
                "qty": "0.02",
                "price": "60000",
                "commission": "0.00001",
                "commissionAsset": "BTC",
                "time": 1,
                "isBuyer": True,
            }
        ]


def _target():
    payload = {
        "platform_id": "binance",
        "strategy_profile": "crypto_live_pool_rotation",
        "dry_run_only": False,
        "deployment_selector": "live",
        "account_selector": ["live"],
        "account_scope": "live",
        "service_name": "binance-platform",
    }
    return build_runtime_target(
        **payload,
        live_continuity={
            "state": "RECONCILE_ONLY",
            "baseline_kind": "legacy_authorized",
            "baseline_id": "binance-lkg-20260830",
            "baseline_target_sha256": runtime_target_fingerprint(payload),
            "captured_at": "2026-08-30",
        },
        continuity_fingerprint_payload=payload,
    )


def _observations():
    return collect_read_only_reconciliation_observations(
        _Client(), strategy_symbols=("BTCUSDT",), local_execution_ledger={"last_cycle": "abc"}
    )


def test_collects_redacted_read_only_exchange_surfaces():
    observations = _observations()

    assert observations.account_scope == {"account_uid": "123456789"}
    assert observations.account_identity_match is True
    assert len(observations.positions) == 2
    assert len(observations.open_orders) == 1
    assert len(observations.recent_executions) == 1


def test_missing_exchange_account_identity_fails_closed():
    class MissingIdentity(_Client):
        def get_account(self):
            payload = super().get_account()
            payload.pop("uid")
            return payload

    with pytest.raises(BinanceReconciliationReadError, match="identity"):
        collect_read_only_reconciliation_observations(
            MissingIdentity(), strategy_symbols=("BTCUSDT",), local_execution_ledger={}
        )


def test_recent_trades_are_partitioned_into_day_windows_and_deduplicated():
    class WindowRecordingClient(_Client):
        def __init__(self):
            self.windows = []

        def get_my_trades(self, *, symbol, startTime, endTime, limit):
            self.windows.append((symbol, startTime, endTime, limit))
            return [
                {
                    "id": 1,
                    "orderId": 2,
                    "symbol": symbol,
                    "qty": "0.02",
                    "price": "60000",
                    "commission": "0.00001",
                    "commissionAsset": "BTC",
                    "time": 1,
                    "isBuyer": True,
                }
            ]

    client = WindowRecordingClient()
    observations = collect_read_only_reconciliation_observations(
        client,
        strategy_symbols=("BTCUSDT",),
        local_execution_ledger={},
        now=datetime(2026, 8, 31, tzinfo=timezone.utc),
        lookback=timedelta(days=2),
    )

    assert [window[2] - window[1] for window in client.windows] == [86_400_000, 86_399_999]
    assert client.windows[1][1] == client.windows[0][2] + 1
    assert len(observations.recent_executions) == 1


def test_full_recent_trades_page_fails_closed_instead_of_guessing_pagination():
    class FullPageClient(_Client):
        def get_my_trades(self, *, symbol, startTime, endTime, limit):
            return [
                {
                    "id": index,
                    "orderId": index,
                    "symbol": symbol,
                    "qty": "0.02",
                    "price": "60000",
                    "commission": "0.00001",
                    "commissionAsset": "BTC",
                    "time": index,
                    "isBuyer": True,
                }
                for index in range(limit)
            ]

    with pytest.raises(BinanceReconciliationReadError, match="page is incomplete"):
        collect_read_only_reconciliation_observations(
            FullPageClient(), strategy_symbols=("BTCUSDT",), local_execution_ledger={}
        )


def test_candidate_stays_frozen_without_private_expected_digests():
    candidate = build_reconciliation_candidate(observations=_observations(), runtime_target=_target())

    assert candidate.permits_active_lkg is False
    assert candidate.expected_digests_configured is False


def test_candidate_can_pass_only_with_all_matching_private_digests():
    seed = build_reconciliation_candidate(observations=_observations(), runtime_target=_target())
    expected = {
        key: seed.evidence.to_dict()[key]
        for key in (
            "positions_sha256",
            "cash_sha256",
            "open_orders_sha256",
            "recent_executions_sha256",
            "local_execution_ledger_sha256",
        )
    }
    candidate = build_reconciliation_candidate(
        observations=_observations(),
        runtime_target=_target(),
        env_reader=lambda name, default=None: json.dumps(expected)
        if name == "BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON"
        else default,
    )

    assert candidate.permits_active_lkg is True
