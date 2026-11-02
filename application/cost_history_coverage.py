"""Pure offline coverage replay. Never calculate, return, or persist cost values."""

from __future__ import annotations

from collections import Counter
from decimal import localcontext
import re

from application.cost_history_collector import (
    HistoryReadError,
    amount,
    integer,
    manifest,
    require_coverage_only,
    trade,
)


def _merged(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _missing(ranges, start, end):
    cursor, gaps = start, []
    for left, right in _merged(ranges):
        if cursor < left:
            gaps.append([cursor, left - 1])
        cursor = right + 1
    if cursor <= end:
        gaps.append([cursor, end])
    return gaps


def _read_history(history):
    if not isinstance(history, dict) or history.get("schema_version") != "cost_history.v1":
        raise HistoryReadError("cost_history_schema_invalid")
    require_coverage_only(history.get("mode"))
    universe = manifest(history.get("universe"))
    start, end = integer(history.get("account_open_ms")), integer(history.get("end_ms"))
    if start > end or not isinstance(history.get("symbols"), dict):
        raise HistoryReadError("cost_history_material_invalid")
    if set(history["symbols"]) != set(universe["symbols"]):
        raise HistoryReadError("cost_history_symbol_manifest_mismatch")
    symbols = {}
    for symbol, material in history["symbols"].items():
        if not isinstance(material, dict):
            raise HistoryReadError("cost_history_material_invalid")
        ranges = material.get("covered_ranges")
        if not isinstance(ranges, list):
            raise HistoryReadError("cost_history_ranges_invalid")
        for span in ranges:
            if (not isinstance(span, list) or len(span) != 2
                    or not start <= integer(span[0]) <= integer(span[1]) <= end):
                raise HistoryReadError("cost_history_ranges_invalid")
        if not isinstance(material.get("gaps"), list):
            raise HistoryReadError("cost_history_gaps_invalid")
        for gap in material["gaps"]:
            if (not isinstance(gap, dict)
                    or not start <= integer(gap.get("start_ms")) <= integer(gap.get("end_ms")) <= end
                    or not isinstance(gap.get("reason"), str)
                    or not re.fullmatch(r"cost_history_[a-z_]{1,64}", gap["reason"])):
                raise HistoryReadError("cost_history_gaps_invalid")
        if not isinstance(material.get("trades"), list):
            raise HistoryReadError("cost_history_trades_invalid")
        unique, duplicates = {}, integer(material.get("duplicate_count"))
        for raw in material["trades"]:
            row = trade(raw, symbol)
            if not start <= row["time"] <= end or row["commissionAsset"] not in universe["coins"]:
                raise HistoryReadError("cost_history_trade_outside_scope")
            if row["id"] in unique:
                if unique[row["id"]] != row:
                    raise HistoryReadError("cost_history_duplicate_conflict")
                duplicates += 1
            unique[row["id"]] = row
        symbols[symbol] = {**material, "trades": list(unique.values()),
                           "duplicate_count": duplicates}
    return universe, start, end, symbols


def replay_coverage(history, *, mode="coverage-only"):
    """Return counts, query coverage and UNKNOWN reasons for every supplied coin.

    The mode guard rejects all apply/write requests before inspecting material.
    There is no sink, backend, settings object or cost-valued output. Trade-only
    input cannot prove opening inventory or nontrade completeness, even when
    the API query ranges cover the full account lifetime.
    """
    require_coverage_only(mode)
    universe, start, end, symbols = _read_history(history)
    coins = {}
    for coin in universe["coins"]:
        pairs = [symbol for symbol, pair in universe["symbols"].items() if coin in pair]
        gaps, range_gaps = [], []
        for symbol in pairs:
            material = symbols[symbol]
            missing = _missing(material["covered_ranges"], start, end)
            explicit = [[gap["start_ms"], gap["end_ms"]] for gap in material["gaps"]]
            range_gaps.extend(missing + explicit)
            gaps.extend({"symbol": symbol, "start_ms": left, "end_ms": right,
                         "reason": "trade_query_uncovered"} for left, right in missing)
            gaps.extend({"symbol": symbol, **gap} for gap in material["gaps"])
        if not pairs:
            range_gaps = [[start, end]]
            gaps.append({"start_ms": start, "end_ms": end, "reason": "no_historical_pair"})
        # Complement the union of missing ranges: every relevant pair must be read.
        covered = _missing(_merged(range_gaps), start, end)
        coins[coin] = {
            "status": "UNKNOWN", "covered_ranges": covered,
            "covered_ranges_by_symbol": {symbol: symbols[symbol]["covered_ranges"] for symbol in pairs},
            "gaps": gaps,
            "unknown_segments": [{"start_ms": start, "end_ms": end, "reasons": [
                "historical_universe_unverified", "opening_and_nontrade_cost_proof_missing",
            ]}],
            "counts": {"trades": 0, "buys": 0, "sells": 0, "fee_events": 0,
                       "duplicates": sum(symbols[s]["duplicate_count"] for s in pairs)},
        }
    balances = Counter()
    ordered = sorted((row for result in symbols.values() for row in result["trades"]),
                     key=lambda row: (row["time"], row["symbol"], row["id"]))

    def unknown(coin, timestamp, reason):
        coins[coin]["unknown_segments"].append({
            "start_ms": timestamp, "end_ms": end, "reasons": [reason],
        })

    # Quantities only detect unsupported debits. They are never returned as
    # holdings or used to infer acquisition cost, prices or a zero-cost seed.
    with localcontext() as context:
        context.prec = 100
        for row in ordered:
            base, quote = universe["symbols"][row["symbol"]]
            qty, quote_qty = amount(row["qty"]), amount(row["quoteQty"])
            received, spent = (base, quote) if row["isBuyer"] else (quote, base)
            credit, debit = (qty, quote_qty) if row["isBuyer"] else (quote_qty, qty)
            for coin, kind in ((base, "buys" if row["isBuyer"] else "sells"),
                               (quote, "sells" if row["isBuyer"] else "buys")):
                coins[coin]["counts"]["trades"] += 1
                coins[coin]["counts"][kind] += 1
            if balances[spent] < debit:
                unknown(spent, row["time"], "debit_without_observed_trade_inventory")
            balances[spent] -= debit
            balances[received] += credit
            fee, fee_coin = amount(row["commission"]), row["commissionAsset"]
            if fee:
                coins[fee_coin]["counts"]["fee_events"] += 1
                balances[fee_coin] -= fee
                if fee_coin not in {base, quote}:
                    for coin in {base, quote, fee_coin}:
                        unknown(coin, row["time"], "third_coin_fee_valuation_and_basis_missing")
            if quote != "USDT":
                unknown(base, row["time"], "historical_quote_cost_proof_missing")
    for coin in coins.values():
        for key in ("covered_ranges", "gaps", "unknown_segments"):
            coin["counts"][key] = len(coin[key])
    return {"schema_version": "cost_history_coverage.v1", "mode": "coverage-only",
            "method": "moving-weighted-average", "scope": "all-coins",
            "account_open_ms": start, "end_ms": end,
            "cost_proof_status": "UNKNOWN", "cost_values_produced": False,
            "coins": coins, "counts": {"coins": len(coins), "symbols": len(symbols),
                                       "trades": len(ordered),
                                       "requests": integer(history.get("request_count"))}}
