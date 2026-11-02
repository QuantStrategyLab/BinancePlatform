"""Isolated Spot trade-history GET collection; no runtime or accounting writes."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


DAY_MS = 86_400_000
PAGE_SIZE = 1000
PATH = "/api/v3/myTrades"


class HistoryReadError(ValueError):
    """Fixed failure codes without provider messages or signed URLs."""


def require_coverage_only(mode):
    if mode != "coverage-only":
        raise HistoryReadError("cost_history_write_forbidden")


def asset(value):
    if not isinstance(value, str) or not re.fullmatch(r"[^\W_]{1,128}", value):
        raise HistoryReadError("cost_history_asset_invalid")
    return value


def integer(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise HistoryReadError("cost_history_integer_invalid")
    return value


def amount(value, *, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,30}(?:\.\d{1,30})?", value):
        raise HistoryReadError("cost_history_amount_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise HistoryReadError("cost_history_amount_invalid") from None
    if positive and parsed <= 0:
        raise HistoryReadError("cost_history_amount_invalid")
    return parsed


def trade(row, symbol):
    if not isinstance(row, dict) or row.get("symbol") != symbol:
        raise HistoryReadError("cost_history_trade_invalid")
    for key in ("id", "time"):
        integer(row.get(key))
    if type(row.get("isBuyer")) is not bool:
        raise HistoryReadError("cost_history_trade_invalid")
    for key in ("qty", "price", "quoteQty", "commission"):
        amount(row.get(key), positive=key != "commission")
    asset(row.get("commissionAsset"))
    return {key: row[key] for key in (
        "symbol", "id", "time", "qty", "price", "quoteQty", "commission",
        "commissionAsset", "isBuyer",
    )}


def manifest(value):
    if not isinstance(value, dict) or set(value) != {"coins", "symbols"}:
        raise HistoryReadError("cost_history_manifest_invalid")
    if not isinstance(value["coins"], list) or not isinstance(value["symbols"], dict):
        raise HistoryReadError("cost_history_manifest_invalid")
    coins = {asset(coin) for coin in value["coins"]}
    symbols = {}
    for symbol, pair in value["symbols"].items():
        asset(symbol)
        if not isinstance(pair, list) or len(pair) != 2:
            raise HistoryReadError("cost_history_manifest_invalid")
        base, quote = map(asset, pair)
        if base == quote or symbol != base + quote:
            raise HistoryReadError("cost_history_manifest_invalid")
        symbols[symbol] = [base, quote]
        coins.update(pair)
    if not coins:
        raise HistoryReadError("cost_history_manifest_invalid")
    return {"coins": sorted(coins), "symbols": dict(sorted(symbols.items()))}


@dataclass(frozen=True)
class ReadPage:
    status: int
    rows: object = field(default=None, repr=False)
    headers: dict = field(default_factory=dict, repr=False)


class SpotHistoryGET:
    """Restrict an existing non-verbose SDK client to one signed GET endpoint.

    Construction performs no request and loads no credentials. The caller owns
    approved credential handling and must not share the SDK client concurrently.
    """

    def __init__(self, client):
        self.__client = client

    def request(self, method, path, params):
        if method != "GET" or path != PATH:
            raise HistoryReadError("cost_history_endpoint_forbidden")
        keys = set(params)
        if (keys not in ({"symbol", "limit", "fromId"},
                         {"symbol", "limit", "startTime", "endTime"})
                or params.get("limit") != PAGE_SIZE):
            raise HistoryReadError("cost_history_parameters_invalid")
        asset(params["symbol"])
        for key in keys - {"symbol"}:
            integer(params[key])
        if "startTime" in keys and not (
            params["startTime"] <= params["endTime"] < params["startTime"] + DAY_MS
        ):
            raise HistoryReadError("cost_history_parameters_invalid")
        if getattr(self.__client, "verbose", False):
            raise HistoryReadError("cost_history_verbose_client_forbidden")
        try:
            rows = self.__client._request_api(
                "get", "myTrades", signed=True, version="v3", data=dict(params),
                allow_redirects=False,
            )
            response = self.__client.response
            return ReadPage(response.status_code, rows, dict(response.headers))
        except Exception as error:
            response = getattr(error, "response", None)
            status = getattr(error, "status_code", 0)
            if status in {418, 429}:
                return ReadPage(status, headers=dict(getattr(response, "headers", {})))
            raise HistoryReadError("cost_history_get_failed") from None


class _Pages:
    def __init__(self, reader, sleep, max_requests, interval, weight_ceiling):
        self.reader, self.sleep = reader, sleep
        self.max_requests, self.interval = max_requests, interval
        self.weight_ceiling = weight_ceiling
        self.requests = 0

    def read(self, params):
        for attempt in range(4):
            if self.requests >= self.max_requests:
                raise HistoryReadError("cost_history_request_budget_exhausted")
            self.sleep(self.interval)
            self.requests += 1
            try:
                page = self.reader.request("GET", PATH, params)
            except Exception:
                raise HistoryReadError("cost_history_get_failed") from None
            if (not isinstance(page, ReadPage) or not isinstance(page.headers, dict)
                    or any(not isinstance(key, str) for key in page.headers)):
                raise HistoryReadError("cost_history_response_invalid")
            headers = {key.lower(): str(value) for key, value in page.headers.items()}
            if page.status == 418:
                raise HistoryReadError("cost_history_ip_banned")
            if page.status == 429:
                try:
                    delay = float(headers["retry-after"])
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError
                except (KeyError, ValueError):
                    raise HistoryReadError("cost_history_retry_after_invalid") from None
                # Long backoffs stop this attempt rather than retrying early.
                if attempt == 3 or delay > 60:
                    raise HistoryReadError("cost_history_rate_limited")
                self.sleep(max(delay, self.interval))
                continue
            if page.status != 200:
                raise HistoryReadError("cost_history_get_failed")
            try:
                weight = int(headers.get("x-mbx-used-weight-1m", "0"))
                if weight < 0:
                    raise ValueError
            except ValueError:
                raise HistoryReadError("cost_history_weight_invalid") from None
            if weight >= self.weight_ceiling:
                self.sleep(60)
            if not isinstance(page.rows, list) or len(page.rows) > PAGE_SIZE:
                raise HistoryReadError("cost_history_page_invalid")
            return page.rows


def collect_history(reader, *, universe, account_open_ms, end_ms,
                    pagination="from-id", sleep=time.sleep, max_requests=100_000,
                    request_interval=1.0, weight_ceiling=1000, mode="coverage-only"):
    """Collect every supplied symbol, retaining failures as explicit gaps.

    A supplied historical universe must include delisted pairs and coins with
    no trades. Current exchange metadata cannot prove that universe complete.
    Empty pages prove a successful query, never the absence of nontrade flows.
    """
    require_coverage_only(mode)
    universe = manifest(universe)
    start, end = integer(account_open_ms), integer(end_ms)
    if (start > end or pagination not in {"from-id", "time-window"}
            or type(max_requests) is not int or max_requests < 1
            or not isinstance(request_interval, (int, float))
            or not math.isfinite(request_interval) or request_interval < 1
            or type(weight_ceiling) is not int or weight_ceiling < 1):
        raise HistoryReadError("cost_history_configuration_invalid")
    pages = _Pages(reader, sleep, max_requests, request_interval, weight_ceiling)
    results = {}
    for symbol in universe["symbols"]:
        seen, covered, gaps = {}, [], []
        duplicates = 0

        def accept(raw_rows, *, cursor=None, window=None):
            nonlocal duplicates
            rows = sorted((trade(row, symbol) for row in raw_rows), key=lambda row: row["id"])
            for row in rows:
                if window and not window[0] <= row["time"] <= window[1]:
                    raise HistoryReadError("cost_history_time_outside_window")
                previous = seen.get(row["id"])
                if previous is not None:
                    if previous != row:
                        raise HistoryReadError("cost_history_duplicate_conflict")
                    duplicates += 1
                elif cursor is not None and row["id"] < cursor:
                    raise HistoryReadError("cost_history_cursor_regressed")
                seen[row["id"]] = row
            if any(a["time"] > b["time"] for a, b in zip(rows, rows[1:])):
                raise HistoryReadError("cost_history_time_regressed")
            return rows

        def scan_ids():
            cursor, last_time = 0, -1
            while True:
                raw = pages.read({"symbol": symbol, "limit": PAGE_SIZE, "fromId": cursor})
                rows = accept(raw, cursor=cursor)
                advancing = [row for row in rows if row["id"] >= cursor]
                if rows and (not advancing or advancing[0]["time"] < last_time):
                    raise HistoryReadError("cost_history_cursor_stalled")
                if not rows or len(raw) < PAGE_SIZE or rows[-1]["time"] > end:
                    return
                cursor, last_time = rows[-1]["id"] + 1, rows[-1]["time"]

        window_start = start
        try:
            if pagination == "from-id":
                scan_ids()
                covered.append([start, end])
            else:
                while window_start <= end:
                    window_end = min(window_start + DAY_MS - 1, end)
                    raw = pages.read({"symbol": symbol, "limit": PAGE_SIZE,
                                      "startTime": window_start, "endTime": window_end})
                    accept(raw, window=(window_start, window_end))
                    if len(raw) == PAGE_SIZE:
                        # A capped window may contain the newest rows. Restart
                        # at ID zero, never infer a cursor from that window.
                        scan_ids()
                        covered = [[start, end]]
                        break
                    covered.append([window_start, window_end])
                    window_start = window_end + 1
        except HistoryReadError as error:
            if str(error) in {"cost_history_duplicate_conflict", "cost_history_time_regressed"}:
                covered, window_start = [], start
            gaps.append({"start_ms": window_start, "end_ms": end, "reason": str(error)})
        results[symbol] = {
            "covered_ranges": covered, "gaps": gaps, "duplicate_count": duplicates,
            "trades": sorted((row for row in seen.values() if start <= row["time"] <= end),
                             key=lambda row: (row["time"], row["id"])),
        }
    # Commission assets are part of the all-coins scope even without their own pair.
    coins = set(universe["coins"])
    for result in results.values():
        coins.update(row["commissionAsset"] for row in result["trades"])
    universe["coins"] = sorted(coins)
    return {"schema_version": "cost_history.v1", "mode": "coverage-only",
            "account_open_ms": start, "end_ms": end, "universe": universe,
            "symbols": results, "request_count": pages.requests}
