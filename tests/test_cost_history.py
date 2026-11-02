"""Synthetic trade history only; no credentials, live SDK construction or network."""

import ast
import builtins
import copy
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.cost_history_collector import (
    DAY_MS, PAGE_SIZE, PATH, HistoryReadError, ReadPage, SpotHistoryGET, collect_history,
)
from application.cost_history_coverage import replay_coverage


FIXTURE = Path(__file__).parent / "fixtures/cost-history/trades.json"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source():
    return json.loads(FIXTURE.read_text())


class FixtureReader:
    def __init__(self, rows, replies=()):
        self.rows = rows
        self.replies = list(replies)
        self.calls = []

    def request(self, method, path, params):
        assert method == "GET" and path == PATH
        self.calls.append(copy.deepcopy(params))
        if self.replies:
            return self.replies.pop(0)
        rows = [r for r in self.rows if r["symbol"] == params["symbol"]]
        if "fromId" in params:
            assert "startTime" not in params and "endTime" not in params
            rows = [r for r in rows if r["id"] >= params["fromId"]]
        else:
            assert 0 <= params["endTime"] - params["startTime"] < DAY_MS
            rows = [r for r in rows if params["startTime"] <= r["time"] <= params["endTime"]]
        return ReadPage(200, sorted(rows, key=lambda r: r["id"])[:params["limit"]])


def collect(source, reader=None, **kwargs):
    return collect_history(
        reader or FixtureReader(source["trades"]), universe=source["universe"],
        account_open_ms=source["account_open_ms"], end_ms=source["end_ms"],
        sleep=kwargs.pop("sleep", lambda _: None), **kwargs,
    )


def dense(source, count):
    source["universe"]["symbols"].pop("ETHBTC")
    source["trades"] = [{**source["trades"][0], "id": index} for index in range(count)]
    return source


@pytest.mark.parametrize("count,requests", [(0, 1), (999, 1), (1000, 2), (2507, 3)])
def test_id_pagination_over_cap_including_same_millisecond(source, count, requests):
    source = dense(source, count)
    reader = FixtureReader(source["trades"])
    history = collect(source, reader)
    result = history["symbols"]["BTCUSDT"]
    assert len(result["trades"]) == count
    assert result["gaps"] == []
    assert result["covered_ranges"] == [[0, source["end_ms"]]]
    assert history["request_count"] == requests
    assert [p["fromId"] for p in reader.calls] == list(range(0, requests * PAGE_SIZE, PAGE_SIZE))


def test_time_windows_preserve_boundaries_and_zero_trade_days(source):
    reader = FixtureReader(source["trades"])
    history = collect(source, reader, pagination="time-window")
    assert len(history["symbols"]["BTCUSDT"]["trades"]) == 2
    assert len(history["symbols"]["ETHBTC"]["trades"]) == 1
    for symbol in source["universe"]["symbols"]:
        assert [(p["startTime"], p["endTime"]) for p in reader.calls if p["symbol"] == symbol] == [
            (0, DAY_MS - 1), (DAY_MS, 2 * DAY_MS - 1), (2 * DAY_MS, source["end_ms"]),
        ]


def test_full_window_restarts_at_zero_even_if_provider_returns_newest(source):
    source = dense(source, 2301)
    reader = FixtureReader(source["trades"], [ReadPage(200, source["trades"][-1000:])])
    history = collect(source, reader, pagination="time-window")
    result = history["symbols"]["BTCUSDT"]
    assert len(result["trades"]) == 2301
    assert result["duplicate_count"] == 1000
    assert result["gaps"] == []
    assert reader.calls[1]["fromId"] == 0
    assert result["covered_ranges"] == [[0, source["end_ms"]]]


def test_duplicate_trade_id_is_scoped_to_symbol(source):
    history = collect(source)
    report = replay_coverage(history)
    assert report["counts"]["trades"] == 3
    assert report["coins"]["ETH"]["counts"]["trades"] == 1


def test_exact_duplicate_is_deduplicated_and_conflict_is_gap(source):
    row = source["trades"][0]
    result = collect(source, FixtureReader([], [ReadPage(200, [row, row])]))
    assert result["symbols"]["BTCUSDT"]["duplicate_count"] == 1
    assert len(result["symbols"]["BTCUSDT"]["trades"]) == 1
    result = collect(source, FixtureReader([], [ReadPage(200, [row, {**row, "qty": "99"}])]))
    assert result["symbols"]["BTCUSDT"]["covered_ranges"] == []
    assert result["symbols"]["BTCUSDT"]["gaps"][0]["reason"] == "cost_history_duplicate_conflict"


def test_cursor_stall_stops_and_preserves_unknown(source):
    source = dense(source, 1000)
    page = ReadPage(200, source["trades"])
    reader = FixtureReader([], [page, page])
    history = collect(source, reader)
    result = history["symbols"]["BTCUSDT"]
    assert len(reader.calls) == 2
    assert result["covered_ranges"] == []
    assert result["gaps"][0]["reason"] == "cost_history_cursor_stalled"
    assert replay_coverage(history)["coins"]["BTC"]["status"] == "UNKNOWN"


def test_rate_limit_retry_after_and_weight_backoff(source):
    source = dense(source, 0)
    sleep = Mock()
    reader = FixtureReader([], [ReadPage(429, headers={"Retry-After": "3"}),
                                ReadPage(200, [], {"X-MBX-USED-WEIGHT-1M": "1000"})])
    history = collect(source, reader, sleep=sleep)
    assert history["request_count"] == 2
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 3.0, 1.0, 60]
    assert reader.calls[0] == reader.calls[1]


@pytest.mark.parametrize("page,reason,requests", [
    (ReadPage(418, headers={"Retry-After": "60"}), "ip_banned", 1),
    (ReadPage(429, headers={"Retry-After": "NaN"}), "retry_after_invalid", 1),
    (ReadPage(429, headers={"Retry-After": "61"}), "rate_limited", 1),
    (ReadPage(429, headers={"Retry-After": "1"}), "rate_limited", 4),
    (ReadPage(401), "get_failed", 1), (ReadPage(500), "get_failed", 1),
])
def test_failed_reads_are_not_empty_coverage(source, page, reason, requests):
    source = dense(source, 0)
    reader = FixtureReader([], [page] * 5)
    history = collect(source, reader)
    result = history["symbols"]["BTCUSDT"]
    assert result["covered_ranges"] == []
    assert result["gaps"][0]["reason"] == "cost_history_" + reason
    assert history["request_count"] == requests


def test_partial_windows_and_failed_delisted_pair_are_explicit(source):
    reader = FixtureReader(source["trades"], [ReadPage(200, []), ReadPage(400)])
    history = collect(source, reader, pagination="time-window")
    result = history["symbols"]["BTCUSDT"]
    assert result["covered_ranges"] == [[0, DAY_MS - 1]]
    assert result["gaps"][0]["start_ms"] == DAY_MS
    report = replay_coverage(history)
    assert report["coins"]["BTC"]["covered_ranges"] == [[0, DAY_MS - 1]]
    assert report["coins"]["ETH"]["covered_ranges"] == [[0, source["end_ms"]]]


def test_request_budget_and_malformed_trade_are_gaps(source):
    history = collect(source, max_requests=1)
    assert history["symbols"]["ETHBTC"]["gaps"][0]["reason"].endswith("budget_exhausted")
    row = {**source["trades"][0], "isBuyer": "false"}
    history = collect(source, FixtureReader([], [ReadPage(200, [row])]))
    assert history["symbols"]["BTCUSDT"]["covered_ranges"] == []


def test_all_coins_quote_legs_and_third_coin_fees_remain_unknown(source):
    history = collect(source)
    original = copy.deepcopy(history)
    report = replay_coverage(history)
    assert history == original
    assert set(report["coins"]) == {"BTC", "ETH", "USDT", "DOGE", "BNB"}
    assert all(coin["status"] == "UNKNOWN" for coin in report["coins"].values())
    assert report["coins"]["BTC"]["counts"]["trades"] == 3
    assert report["coins"]["BNB"]["counts"]["fee_events"] == 1
    assert report["coins"]["DOGE"]["gaps"][0]["reason"] == "no_historical_pair"
    assert report["coins"]["BTC"]["covered_ranges"] == [[0, source["end_ms"]]]
    assert any("third_coin_fee_policy_and_valuation_missing" in segment["reasons"]
               for segment in report["coins"]["BNB"]["unknown_segments"])
    serialized = json.dumps(report)
    for forbidden in ("average_cost", "quoteQty", "commissionAsset", "entry_price", '"qty"', '"price"'):
        assert forbidden not in serialized
    assert report["cost_values_produced"] is False


def test_report_recomputes_gaps_and_rejects_conflicting_saved_rows(source):
    history = collect(source)
    history["symbols"]["BTCUSDT"]["covered_ranges"] = [[1, 5]]
    report = replay_coverage(history)
    assert report["coins"]["USDT"]["covered_ranges"] == [[1, 5]]
    assert any(g["start_ms"] == 0 for g in report["coins"]["USDT"]["gaps"])
    row = history["symbols"]["BTCUSDT"]["trades"][0]
    history["symbols"]["BTCUSDT"]["trades"].append({**row, "qty": "5"})
    with pytest.raises(HistoryReadError, match="duplicate_conflict"):
        replay_coverage(history)


@pytest.mark.parametrize("mode", ["apply", "write", "rebuild", None, True])
def test_guard_rejects_writes_before_reading_any_input(mode):
    with pytest.raises(HistoryReadError, match="write_forbidden"):
        replay_coverage(None, mode=mode)
    reader = Mock()
    with pytest.raises(HistoryReadError, match="write_forbidden"):
        collect_history(reader, universe=None, account_open_ms=0, end_ms=1, mode=mode)
    reader.request.assert_not_called()


def test_offline_replay_has_no_io_or_store_side_effects(source, monkeypatch):
    history = collect(source)
    original = copy.deepcopy(history)
    tree = ast.parse((ROOT / "application/cost_history_coverage.py").read_text())

    def forbidden(*args, **kwargs):
        pytest.fail("Offline replay attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    report = replay_coverage(history)
    assert report["mode"] == "coverage-only"
    assert history == original
    # A future storage/runtime dependency must fail this architectural guard.
    allowed = {"__future__", "collections", "decimal", "re", "application.cost_history_collector"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)


def test_sdk_adapter_enforces_get_allowlist_and_redacts_failures():
    sdk = SimpleNamespace(verbose=False, response=SimpleNamespace(status_code=200, headers={}))
    sdk._request_api = Mock(return_value=[])
    reader = SpotHistoryGET(sdk)
    params = {"symbol": "BTCUSDT", "limit": 1000, "fromId": 0}
    assert reader.request("GET", PATH, params).rows == []
    sdk._request_api.assert_called_once_with(
        "get", "myTrades", signed=True, version="v3", data=params, allow_redirects=False,
    )
    for method, path in (("POST", PATH), ("GET", "/api/v3/order"),
                         ("GET", "/sapi/v1/asset/transfer")):
        with pytest.raises(HistoryReadError, match="endpoint_forbidden"):
            reader.request(method, path, params)
    with pytest.raises(HistoryReadError, match="parameters_invalid"):
        reader.request("GET", PATH, {**params, "startTime": 0, "endTime": 1})
    assert sdk._request_api.call_count == 1
    sdk._request_api.side_effect = RuntimeError("synthetic-private-url-and-response")
    with pytest.raises(HistoryReadError) as error:
        reader.request("GET", PATH, params)
    assert str(error.value) == "cost_history_get_failed"
    assert error.value.__suppress_context__
    sdk.verbose = True
    with pytest.raises(HistoryReadError, match="verbose_client_forbidden"):
        reader.request("GET", PATH, params)


def test_offline_cli_prints_report_and_rejects_apply_mode(source, tmp_path):
    saved = tmp_path / "synthetic-history.json"
    saved.write_text(json.dumps(collect(source)))
    for name in ("cost-store.json", "ledger.json", "settings.json"):
        (tmp_path / name).write_text('{"synthetic_existing_state": true}')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    script = ROOT / "scripts/replay_cost_history_coverage.py"
    result = subprocess.run([sys.executable, str(script), "--input", str(saved)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cost_values_produced"] is False
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    result = subprocess.run([sys.executable, str(script), "--input", str(saved), "--mode", "apply"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_conflict_during_fallback_invalidates_earlier_coverage(source):
    source = dense(source, 1001)
    source["trades"][0]["time"] = 0
    for row in source["trades"][1:]:
        row["time"] = DAY_MS
    reader = FixtureReader([], [
        ReadPage(200, [source["trades"][0]]),
        ReadPage(200, source["trades"][1:]),
        ReadPage(200, [{**source["trades"][0], "qty": "9"}, *source["trades"][1:1000]]),
    ])
    history = collect(source, reader, pagination="time-window")
    result = history["symbols"]["BTCUSDT"]
    assert result["covered_ranges"] == []
    assert result["gaps"] == [{"start_ms": 0, "end_ms": source["end_ms"],
                               "reason": "cost_history_duplicate_conflict"}]


def test_cutoff_is_inclusive_and_future_trades_are_filtered(source):
    source = dense(source, 1001)
    for row in source["trades"]:
        row["time"] = row["id"]
    source["end_ms"] = 999
    reader = FixtureReader(source["trades"])
    history = collect(source, reader)
    assert len(history["symbols"]["BTCUSDT"]["trades"]) == 1000
    assert history["request_count"] == 2
    assert history["symbols"]["BTCUSDT"]["covered_ranges"] == [[0, 999]]


def test_sdk_rate_response_never_returns_provider_error_payload():
    class SyntheticRateError(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"Retry-After": "2"})

    sdk = SimpleNamespace(verbose=False, _request_api=Mock(side_effect=SyntheticRateError("private")))
    page = SpotHistoryGET(sdk).request(
        "GET", PATH, {"symbol": "BTCUSDT", "limit": 1000, "fromId": 0},
    )
    assert page == ReadPage(429, headers={"Retry-After": "2"})
    assert "private" not in repr(page)


def test_offline_guard_rejects_storage_sinks(source):
    history = collect(source)
    for target in ("store", "ledger", "settings"):
        sink = Mock()
        with pytest.raises(TypeError):
            replay_coverage(history, **{target: sink})
        assert sink.mock_calls == []
