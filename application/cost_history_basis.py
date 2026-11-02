"""Pure, evidence-backed moving-average preview; no cost persistence or live I/O."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, localcontext

from application.cost_history_collector import HistoryReadError, amount, integer, require_coverage_only
from application.cost_history_coverage import _missing, _read_history, replay_coverage


def _reference(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 512


def _text(value):
    if value is None:
        return None
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _price(row, coin, reporting_asset):
    if coin == reporting_asset:
        return Decimal(1)
    prices = row.get("prices", {})
    valuation = prices.get(coin) if isinstance(prices, dict) else None
    if (not isinstance(valuation, dict) or not _reference(valuation.get("source"))
            or type(valuation.get("time_ms")) is not int
            or valuation["time_ms"] != row["time_ms"]):
        return None
    try:
        return amount(valuation.get("price"), positive=True)
    except HistoryReadError:
        return None


class _Pool:
    def __init__(self, opening, complete, start, end):
        self.quantity = Decimal(0)
        self.cost = None
        self.reasons = set()
        self.segments = []
        self.end = end
        self.complete = complete
        if isinstance(opening, dict) and _reference(opening.get("source")):
            self.quantity = amount(opening.get("quantity"))
            if opening.get("cost") is not None:
                self.cost = amount(opening["cost"])
                if not self.quantity and self.cost:
                    raise HistoryReadError("cost_history_opening_invalid")
        else:
            self.complete = False
            self.unknown(start, "opening_evidence_missing")
        if self.cost is None:
            self.unknown(start, "opening_cost_unknown")
        if not complete:
            self.unknown(start, "history_completeness_unproven")

    def unknown(self, time, reason, *, quantity_uncertain=False):
        if quantity_uncertain:
            self.complete = False
        self.cost = None
        if reason not in self.reasons:
            self.reasons.add(reason)
            self.segments.append({"start_ms": time, "end_ms": self.end, "reason": reason})

    def debit(self, quantity, time):
        removed = None
        if quantity > self.quantity:
            self.unknown(time, "inventory_shortfall", quantity_uncertain=True)
        if self.cost is not None and self.quantity:
            removed = self.cost * quantity / self.quantity
            self.cost -= removed
        self.quantity -= quantity
        if not self.quantity and self.complete:
            self.cost = Decimal(0)
            for segment in self.segments:
                segment["end_ms"] = min(segment["end_ms"], time)
            self.reasons.clear()
        return removed

    def credit(self, quantity, cost, time, reason):
        self.quantity += quantity
        if cost is None:
            self.unknown(time, reason)
        elif self.cost is not None:
            self.cost += cost

    def report(self):
        known = self.complete and self.cost is not None
        return {
            "status": "KNOWN" if known else "UNKNOWN",
            "quantity": _text(self.quantity) if self.complete else None,
            "observed_quantity": _text(self.quantity),
            "total_cost": _text(self.cost) if known else None,
            "average_cost": _text(self.cost / self.quantity) if known and self.quantity else None,
            "reasons": sorted(self.reasons), "unknown_segments": self.segments,
        }


def _events(evidence, universe, start, end, symbols):
    events, seen, transfers, conversions = [], {}, set(), {}
    supplied = evidence.get("events", [])
    trade_proofs = evidence.get("trades", {})
    if not isinstance(supplied, list) or not isinstance(trade_proofs, dict):
        raise HistoryReadError("cost_history_evidence_invalid")
    trade_keys = set()
    for symbol, material in symbols.items():
        for row in material["trades"]:
            key = f"{symbol}:{row['id']}"
            trade_keys.add(key)
            proof = trade_proofs.get(key, {})
            if not isinstance(proof, dict):
                raise HistoryReadError("cost_history_evidence_invalid")
            base, quote = universe["symbols"][symbol]
            sold, bought = (quote, base) if row["isBuyer"] else (base, quote)
            debit, credit = (row["quoteQty"], row["qty"]) if row["isBuyer"] else (
                row["qty"], row["quoteQty"])
            events.append({"id": key, "kind": "spot", "time_ms": row["time"],
                           "sold_coin": sold, "bought_coin": bought,
                           "sold_quantity": debit, "bought_quantity": credit,
                           "fee_coin": row["commissionAsset"], "fee_quantity": row["commission"],
                           "valuation_coin": quote, "valuation_quantity": row["quoteQty"],
                           "prices": proof.get("prices", {}), "sequence": proof.get("sequence"),
                           "source": key, "execution_id": key})
    if set(trade_proofs) - trade_keys:
        raise HistoryReadError("cost_history_trade_evidence_unmatched")
    for raw in supplied:
        if (not isinstance(raw, dict) or not _reference(raw.get("id"))
                or raw["id"] in trade_keys or not isinstance(raw.get("kind"), str)):
            raise HistoryReadError("cost_history_event_invalid")
        row = dict(raw)
        if row["kind"] not in {"earn_reward", "staking_reward", "airdrop", "deposit", "transfer_in",
                               "internal_transfer", "withdrawal", "convert", "dust"}:
            raise HistoryReadError("cost_history_event_kind_unsupported")
        if not start <= integer(row.get("time_ms")) <= end:
            raise HistoryReadError("cost_history_event_outside_scope")
        if row["id"] in seen:
            if seen[row["id"]] != row:
                raise HistoryReadError("cost_history_duplicate_conflict")
            continue
        seen[row["id"]] = row
        if row["kind"] in {"convert", "dust"} and _reference(row.get("execution_id")):
            key = (row["kind"], row["execution_id"])
            execution = {k: v for k, v in row.items() if k != "id"}
            if key in conversions:
                if conversions[key] != execution:
                    raise HistoryReadError("cost_history_duplicate_conflict")
                continue
            conversions[key] = execution
        if row["kind"] in {"deposit", "transfer_in", "internal_transfer"}:
            identity = row.get("transfer_id")
            if _reference(identity):
                if identity in transfers:
                    raise HistoryReadError("cost_history_transfer_reused")
                transfers.add(identity)
        events.append(row)
    for row in events:
        if row.get("sequence") is not None:
            integer(row["sequence"])
        assets = _assets(row)
        if not assets or assets - set(universe["coins"]):
            raise HistoryReadError("cost_history_event_coin_outside_scope")
    return sorted(events, key=lambda r: (r["time_ms"], r.get("sequence") or 0, r["id"]))


def _assets(row):
    keys = ("sold_coin", "bought_coin", "fee_coin") if row["kind"] in {
        "spot", "convert", "dust",
    } else ("coin", "fee_coin")
    values = [row[key] for key in keys if key in row]
    if any(not isinstance(value, str) for value in values):
        raise HistoryReadError("cost_history_event_invalid")
    return set(values)


def _transfer_cost(row):
    """Match an external outgoing leg to a received net amount and documented basis."""
    leg = row.get("source_leg")
    fields = ("transfer_id", "from_account", "to_account", "owner", "coin")
    if (not isinstance(leg, dict) or not _reference(row.get("source"))
            or not _reference(leg.get("source"))
            or any(not _reference(row.get(k)) or row[k] != leg.get(k) for k in fields)
            or row["from_account"] == row["to_account"]
            or type(leg.get("time_ms")) is not int
            or not 0 <= leg["time_ms"] <= row["time_ms"]):
        return None
    try:
        gross = amount(leg.get("quantity"), positive=True)
        fee = amount(leg.get("fee"))
        cost = amount(leg.get("source_cost"))
        if (leg.get("fee_coin") != row["coin"]
                or gross - fee != amount(row.get("quantity"), positive=True)):
            return None
        # The sending fee is disposed at source average cost, not carried into
        # the arriving net lot. source_cost describes the full outgoing lot.
        return cost * (gross - fee) / gross
    except HistoryReadError:
        return None


def _internal_matched(row):
    leg = row.get("source_leg")
    fields = ("transfer_id", "from_account", "to_account", "owner", "coin")
    try:
        return (isinstance(leg, dict) and _reference(row.get("source"))
                and _reference(leg.get("source"))
                and all(_reference(row.get(k)) and row[k] == leg.get(k) for k in fields)
                and row["from_account"] != row["to_account"]
                and amount(row["quantity"], positive=True) == amount(leg.get("quantity"), positive=True)
                and amount(row.get("fee_quantity")) == amount(leg.get("fee_quantity")) == 0)
    except HistoryReadError:
        return False


def _acquisition(row, coin, quantity, cost, reporting):
    valuations = []
    for asset in sorted(_assets(row)):
        price = _price(row, asset, reporting)
        if price is not None:
            valuations.append({"coin": asset, "price": _text(price), "time_ms": row["time_ms"],
                               "source": "reporting-unit" if asset == reporting else
                               row["prices"][asset]["source"]})
    leg = row.get("source_leg", {})
    return {"event_id": row["id"], "kind": row["kind"], "time_ms": row["time_ms"],
            "coin": coin, "quantity": _text(quantity), "acquisition_cost": _text(cost),
            "source": row.get("source") if _reference(row.get("source")) else None,
            "source_leg_reference": leg.get("source") if isinstance(leg, dict) else None,
            "execution_id": row.get("execution_id"), "valuations": valuations}


def _exchange(row, pools, reporting, disposals, acquisitions):
    sold, bought = row.get("sold_coin"), row.get("bought_coin")
    if sold not in pools or bought not in pools or sold == bought:
        raise HistoryReadError("cost_history_exchange_invalid")
    debit = amount(row.get("sold_quantity"), positive=True)
    credit = amount(row.get("bought_quantity"), positive=True)
    time = row["time_ms"]
    complete = (_reference(row.get("execution_id")) and _reference(row.get("source"))
                and row.get("fee_coin") in pools and row.get("fee_quantity") is not None)
    fee = amount(row["fee_quantity"]) if row.get("fee_quantity") is not None else Decimal(0)
    fee_coin = row.get("fee_coin")
    if fee_coin == bought and fee >= credit:
        raise HistoryReadError("cost_history_fee_exceeds_receipt")
    if row["kind"] == "spot":
        valuation_coin = row["valuation_coin"]
        valuation_quantity = amount(row["valuation_quantity"], positive=True)
    elif reporting in {sold, bought}:
        valuation_coin = reporting
        valuation_quantity = debit if sold == reporting else credit
    else:
        valuation_coin, valuation_quantity = sold, debit
    execution_price = _price(row, valuation_coin, reporting)
    value = valuation_quantity * execution_price if execution_price is not None else None
    removed = pools[sold].debit(debit, time)
    disposals.append({"event_id": row["id"], "time_ms": time, "coin": sold,
                      "quantity": _text(debit), "removed_cost": _text(removed), "kind": "exchange"})
    reason = "execution_valuation_missing"
    if not complete:
        value, reason = None, "conversion_evidence_incomplete"
        for coin in _assets(row):
            pools[coin].unknown(time, reason, quantity_uncertain=True)
        if fee_coin is None or row.get("fee_quantity") is None:
            for pool in pools.values():
                pool.unknown(time, "conversion_fee_evidence_missing", quantity_uncertain=True)
    if fee and fee_coin != bought and fee_coin in pools:
        fee_cost = pools[fee_coin].debit(fee, time)
        disposals.append({"event_id": row["id"], "time_ms": time, "coin": fee_coin,
                          "quantity": _text(fee), "removed_cost": _text(fee_cost), "kind": "fee"})
        fee_price = _price(row, fee_coin, reporting)
        if fee_price is None:
            value, reason = None, "fee_valuation_missing"
        elif fee_coin != sold and fee_cost is None:
            value, reason = None, "fee_coin_basis_unknown"
        elif value is not None:
            value += fee * fee_price
    net = credit - (fee if fee_coin == bought else 0)
    pools[bought].credit(net, value, time, reason)
    acquisitions.append(_acquisition(row, bought, net, value, reporting))


def _replay(history, evidence):
    universe, start, end, symbols = _read_history(history)
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != "cost_basis_evidence.v1"
            or type(evidence.get("account_open_ms")) is not int
            or type(evidence.get("end_ms")) is not int
            or evidence["account_open_ms"] != start or evidence["end_ms"] != end
            or evidence.get("reporting_asset") not in universe["coins"]):
        raise HistoryReadError("cost_history_evidence_scope_invalid")
    opening, sources = evidence.get("opening", {}), evidence.get("nontrade_sources", {})
    if (not isinstance(opening, dict) or not isinstance(sources, dict)
            or (set(opening) | set(sources)) - set(universe["coins"])):
        raise HistoryReadError("cost_history_evidence_invalid")
    # A missed trade can debit any manifest coin as its commission asset, not
    # just the base/quote. Nontrade completeness cannot prove these fees absent.
    queried = all(not material["gaps"] and not _missing(material["covered_ranges"], start, end)
                  for material in symbols.values())
    pools = {}
    for coin in universe["coins"]:
        complete = queried and _reference(sources.get(coin)) and _reference(evidence.get("universe_source"))
        pools[coin] = _Pool(opening.get(coin), complete, start, end)
    events = _events(evidence, universe, start, end, symbols)
    by_time = Counter(row["time_ms"] for row in events)
    sequences = {}
    for row in events:
        sequences.setdefault(row["time_ms"], []).append(row.get("sequence"))
    ambiguous = {t for t, seq in sequences.items() if by_time[t] > 1
                 and (None in seq or len(set(seq)) != len(seq))}
    disposals, acquisitions = [], []
    for row in events:
        time, kind = row["time_ms"], row["kind"]
        if time in ambiguous:
            for coin in _assets(row):
                pools[coin].unknown(time, "same_time_order_unproven", quantity_uncertain=True)
        if kind in {"spot", "convert", "dust"}:
            _exchange(row, pools, evidence["reporting_asset"], disposals, acquisitions)
            continue
        coin = row.get("coin")
        if coin not in pools:
            raise HistoryReadError("cost_history_event_invalid")
        quantity = amount(row.get("quantity"), positive=True)
        if kind in {"earn_reward", "staking_reward", "airdrop"}:
            price = _price(row, coin, evidence["reporting_asset"])
            cost = quantity * price if price is not None and _reference(row.get("source")) else None
            pools[coin].credit(quantity, cost, time, "receipt_valuation_missing")
            acquisitions.append(_acquisition(row, coin, quantity, cost, evidence["reporting_asset"]))
        elif kind in {"deposit", "transfer_in"}:
            cost = _transfer_cost(row)
            pools[coin].credit(quantity, cost, time, "transfer_source_unmatched")
            acquisitions.append(_acquisition(row, coin, quantity, cost, evidence["reporting_asset"]))
        elif kind == "internal_transfer":
            # Spot/Earn principal is inside the aggregate account inventory.
            if not _internal_matched(row) or pools[coin].quantity < quantity:
                pools[coin].unknown(time, "internal_principal_unmatched", quantity_uncertain=True)
        elif kind == "withdrawal":
            removed = pools[coin].debit(quantity, time)
            disposals.append({"event_id": row["id"], "time_ms": time, "coin": coin,
                              "quantity": _text(quantity), "removed_cost": _text(removed),
                              "kind": "withdrawal"})
            if not _reference(row.get("source")) or row.get("fee_quantity") is None:
                for pool in pools.values():
                    pool.unknown(time, "withdrawal_evidence_incomplete", quantity_uncertain=True)
            else:
                fee = amount(row["fee_quantity"])
                if fee:
                    fee_coin = row.get("fee_coin")
                    if fee_coin not in pools:
                        raise HistoryReadError("cost_history_event_invalid")
                    removed = pools[fee_coin].debit(fee, time)
                    disposals.append({"event_id": row["id"], "time_ms": time, "coin": fee_coin,
                                      "quantity": _text(fee), "removed_cost": _text(removed),
                                      "kind": "fee"})
        else:
            raise HistoryReadError("cost_history_event_kind_unsupported")
    coins = {coin: pool.report() for coin, pool in pools.items()}
    return {"schema_version": "cost_basis_preview.v1", "mode": "coverage-only",
            "method": "moving-weighted-average", "scope": "all-coins",
            "account_open_ms": start, "end_ms": end, "reporting_asset": evidence["reporting_asset"],
            "cost_writes_allowed": False, "cost_values_produced": True,
            "cost_proof_status": "KNOWN" if all(c["status"] == "KNOWN" for c in coins.values()) else "UNKNOWN",
            "coins": coins, "disposals": disposals, "acquisitions": acquisitions,
            "coverage": replay_coverage(history)}


def replay_cost_basis(history, evidence, *, mode="coverage-only"):
    """Return a private preview from supplied evidence; reject writes before input.

    Evidence references are supplied provenance claims, never authenticated by
    this function. Incomplete history, unmatched legs and missing historical
    valuations remain UNKNOWN. No runtime, persistence or network dependency.
    """
    require_coverage_only(mode)
    with localcontext() as context:
        context.prec = 160
        return _replay(history, evidence)
