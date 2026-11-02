"""Pure forward accounting projection; acquisition and trading are separate.

No acquisition, persistence, recovery, pricing or execution clients. Receipts are
explicit evidence claims, never proof of deployed collection. Native Funding
and locked Earn remain unknown unless separately supplied with coverage. The
offline CLI accepts only synthetic observations; the private API additionally
requires an independently configured account and source binding.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re

from application.reconciliation_observation import POLICY, parse_time


WALLETS = ("spot", "funding", "flexible_earn", "locked_earn")
SURFACES = ("fills", "deposits", "withdrawals", "rewards", "convert", "dust",
            "transfers", "flexible_lifecycle", "locked_lifecycle")
SURFACE_BY_KIND = {
    "fill": "fills", "deposit": "deposits", "withdrawal": "withdrawals",
    "reward": "rewards", "convert": "convert", "dust": "dust", "transfer": "transfers",
    "flexible_subscription": "flexible_lifecycle", "flexible_redemption": "flexible_lifecycle",
    "locked_subscription": "locked_lifecycle", "locked_redemption": "locked_lifecycle",
}
PROGRAM_START = parse_time("2026-11-03T00:00:00Z")
_ASSET = re.compile(r"[A-Z0-9]{1,20}\Z")
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,30})?\Z")
_SYNTHETIC = re.compile(r"synthetic-[A-Za-z0-9_.-]{1,64}\Z")
_ACCOUNT = re.compile(r"binance-paper-mock-[A-Za-z0-9_.-]{1,64}\Z")


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _asset(value):
    _require(isinstance(value, str) and _ASSET.fullmatch(value), "asset_invalid")
    return value


def _amount(value):
    _require(isinstance(value, str) and _AMOUNT.fullmatch(value), "quantity_invalid")
    return Decimal(value)


def _text(value):
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _synthetic(value):
    return isinstance(value, str) and _SYNTHETIC.fullmatch(value)


def _opaque(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value


def page_content_sha256(rows):
    """Private integrity receipt, never an anonymized/public identity."""
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def _receipt(receipt, binding, cutoff, rows):
    """Check complete, stable, source-bound pages and retain explicit zero rows."""
    _require(isinstance(receipt, dict), "receipt_missing")
    _require(receipt.get("binding") == binding, "receipt_binding_invalid")
    _require(parse_time(receipt.get("source_as_of")) == cutoff, "source_time_invalid")
    started = parse_time(receipt.get("started_at"))
    finished = parse_time(receipt.get("finished_at"))
    _require(started <= finished and cutoff <= finished, "capture_time_invalid")
    revision = receipt.get("stable_revision")
    _require(_opaque(revision), "page_revision_missing")
    pages = receipt.get("pages")
    _require(isinstance(pages, list) and 1 <= len(pages) <= 1000, "pages_incomplete")
    consumed = 0
    count = len(rows)
    previous_finish = started
    for number, page in enumerate(pages, 1):
        _require(isinstance(page, dict), "page_invalid")
        _require(type(page.get("number")) is int and page["number"] == number, "page_order_invalid")
        size, received, total = (page.get(k) for k in ("size", "count", "total"))
        _require(type(size) is int and 1 <= size <= 1000
                 and type(received) is int and 0 <= received <= size
                 and type(total) is int and 0 <= total <= 10000, "page_count_invalid")
        _require(page.get("binding") == binding and page.get("revision") == revision,
                 "page_binding_or_revision_changed")
        begin, end = parse_time(page.get("started_at")), parse_time(page.get("finished_at"))
        _require(previous_finish <= begin <= end <= finished, "page_capture_invalid")
        previous_finish = end
        _require(total == count and received == min(size, max(0, total - consumed)), "pages_incomplete")
        _require(number == 1 or consumed < total, "page_extra")
        _require(page.get("content_sha256") == page_content_sha256(rows[consumed:consumed + received]),
                 "page_content_changed")
        consumed += received
    _require(consumed == count, "pages_incomplete")


def _snapshot(value, *, account, binding, cutoff):
    _require(isinstance(value, dict), "snapshot_missing")
    _require(value.get("account_id") == account and value.get("binding") == binding,
             "snapshot_binding_invalid")
    _require(parse_time(value.get("as_of")) == cutoff, "snapshot_cutoff_invalid")
    _require(isinstance(value.get("wallets"), dict), "wallets_missing")
    return value["wallets"]


def _native_balances(rows, wallet):
    """Retain native rows in private inputs; derive quantities without valuation."""
    _require(isinstance(rows, list) and len(rows) <= 10000, "native_rows_invalid")
    balances, identities = {}, set()
    for row in rows:
        _require(isinstance(row, dict), "native_row_invalid")
        asset = _asset(row.get("asset"))
        if wallet in ("spot", "funding"):
            _require(asset not in balances, "native_asset_duplicate")
            names = ("free", "locked") if wallet == "spot" else ("free", "locked", "freeze", "withdrawing")
            components = [_amount(row.get(name)) for name in names]
            # Withdrawal-in-progress ownership/fee semantics are not qualified.
            if wallet == "funding":
                _require(components[-1] == 0, "funding_withdrawing_unproven")
            quantity = sum(components)
        else:
            identity_field, amount_field = (("productId", "totalAmount") if wallet == "flexible_earn"
                                             else ("positionId", "amount"))
            identity = row.get(identity_field)
            _require(_opaque(identity) and identity not in identities, "native_product_invalid")
            identities.add(identity)
            _require(row.get("unsupported") is not True, "native_product_unsupported")
            quantity = _amount(row.get(amount_field))
        balances[asset] = balances.get(asset, Decimal(0)) + quantity
    return balances


def _wallet(value, *, wallet, binding, cutoff):
    _require(isinstance(value, dict), "wallet_inventory_missing")
    _require(not ("native_rows" in value and "balances" in value), "inventory_ambiguous")
    if "native_rows" in value:
        normalized = _native_balances(value["native_rows"], wallet)
        rows = value["native_rows"]
    else:
        balances = value.get("balances")
        _require(isinstance(balances, dict) and len(balances) <= 10000, "balances_invalid")
        normalized = {_asset(a): _amount(q) for a, q in balances.items()}
        rows = [{"asset": asset, "quantity": quantity} for asset, quantity in sorted(balances.items())]
    _receipt(value.get("receipt"), binding, cutoff, rows)
    return normalized


def project_spot_fill(row, *, base_asset, quote_asset):
    """Project one native fill using an independently qualified symbol mapping.

    ``quoteQty`` is mandatory; ``price * qty`` is never substituted. Symbol and
    trade ID form the canonical identity across an all-symbol input universe.
    """
    _require(isinstance(row, dict), "fill_invalid")
    _require(_opaque(row.get("symbol")) and type(row.get("id")) is int
             and row["id"] >= 0 and type(row.get("time")) is int
             and type(row.get("isBuyer")) is bool, "fill_identity_invalid")
    _require(_asset(base_asset) != _asset(quote_asset), "fill_mapping_invalid")
    base, quote, fee = (_amount(row.get(k)) for k in ("qty", "quoteQty", "commission"))
    _require(base > 0 and quote > 0, "fill_quantity_invalid")
    at = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=row["time"])).isoformat().replace("+00:00", "Z")
    outgoing, incoming = ((quote_asset, base_asset) if row["isBuyer"] else (base_asset, quote_asset))
    outgoing_qty, incoming_qty = ((quote, base) if row["isBuyer"] else (base, quote))
    return {"kind": "fill", "id": row["symbol"] + ":" + str(row["id"]), "at": at,
            "status": "SUCCESS", "wallet": "spot", "from_asset": outgoing,
            "from_quantity": _text(outgoing_qty), "to_asset": incoming,
            "to_quantity": _text(incoming_qty), "fee_asset": _asset(row.get("commissionAsset")),
            "fee_quantity": _text(fee), "fee_semantics": "additional_debit"}


def _legs(event):
    """Derive signed legs once from one canonical native event, without prices."""
    kind = event["kind"]
    _require(event.get("status") == "SUCCESS", "event_status_unproven")
    legs = []

    def add(wallet, asset, quantity):
        _require(wallet in WALLETS, "wallet_boundary_unsupported")
        legs.append({"wallet": wallet, "asset": _asset(asset), "quantity": quantity})

    if kind in ("transfer", "flexible_subscription", "flexible_redemption",
                "locked_subscription", "locked_redemption"):
        source, destination = event.get("source_wallet"), event.get("destination_wallet")
        _require(source != destination, "transfer_wallets_invalid")
        if kind != "transfer":
            earn = "flexible_earn" if kind.startswith("flexible") else "locked_earn"
            pair = ("spot", earn) if kind.endswith("subscription") else (earn, "spot")
            _require((source, destination) == pair, "lifecycle_wallets_unsupported")
        quantity, asset = _amount(event.get("quantity")), _asset(event.get("asset"))
        _require(quantity > 0, "event_quantity_invalid")
        add(source, asset, -quantity)
        add(destination, asset, quantity)
        # Nonzero or unknown fees cannot disappear into the paired amount.
        _require(event.get("fee_quantity", "0") == "0", "transfer_fee_unsupported")
    elif kind in ("fill", "convert", "dust"):
        _require(event.get("from_asset") != event.get("to_asset"), "exchange_assets_invalid")
        outgoing, incoming = _amount(event.get("from_quantity")), _amount(event.get("to_quantity"))
        _require(outgoing > 0 and incoming > 0, "event_quantity_invalid")
        _require(event.get("fee_semantics") == "additional_debit", "fee_semantics_unproven")
        add(event.get("wallet"), event.get("from_asset"), -outgoing)
        add(event.get("wallet"), event.get("to_asset"), incoming)
        add(event.get("wallet"), event.get("fee_asset"), -_amount(event.get("fee_quantity")))
    else:
        quantity = _amount(event.get("quantity"))
        _require(quantity > 0, "event_quantity_invalid")
        if kind == "withdrawal":
            _require(event.get("fee_semantics") == "additional_debit", "withdrawal_fee_semantics_unproven")
            add(event.get("wallet"), event.get("asset"), -quantity)
            add(event.get("wallet"), event.get("fee_asset"), -_amount(event.get("fee_quantity")))
        else:
            add(event.get("wallet"), event.get("asset"), quantity)
    return legs


def build_forward_report(fixture, *, expected_account_id=None, expected_binding=None):
    """Recompute an informational report from one detached observation bundle.

    No persisted MATCHED flag, checkpoint, historical cost, recovery cursor or
    midnight assumption is consumed. Private observations require binding from
    the caller's approved configuration, separate from the input bundle. Missing
    input fails the comparison, never a trading action. No financial PASS or
    runtime adoption is implied, even for an exact quantity match.
    """
    _require(isinstance(fixture, dict), "forward_input_invalid")
    synthetic = fixture.get("environment") == "synthetic"
    if synthetic:
        _require(isinstance(fixture.get("account_id"), str)
                 and _ACCOUNT.fullmatch(fixture["account_id"])
                 and _synthetic(fixture.get("binding")), "forward_synthetic_input_required")
    else:
        _require(fixture.get("environment") == "private_observation",
                 "forward_synthetic_input_required")
        _require(_opaque(expected_account_id) and _opaque(expected_binding)
                 and fixture.get("account_id") == expected_account_id
                 and fixture.get("binding") == expected_binding, "forward_private_binding_required")
    inputs = deepcopy(fixture)
    with localcontext() as context:
        context.prec = 100
        report = _build(inputs)
        if not synthetic:
            report["environment"] = "private_observation"
            report["schema_version"] = "binance_forward_observation.v1"
            if report["status"] == "MATCHED_SYNTHETIC":
                report["status"] = "MATCHED_OBSERVATION"
            for wallet in report["wallets"].values():
                if wallet["status"] == "MATCHED_SYNTHETIC":
                    wallet["status"] = "MATCHED_OBSERVATION"
        return report


def _build(inputs):
    reasons = set()
    report = {"schema_version": "binance_forward_observation.synthetic.v1",
              "environment": "synthetic", "status": "UNRECONCILED",
              "scope": "spot+funding+flexible_earn+locked_earn",
              "uncovered_scopes": ["margin", "futures", "options", "portfolio_margin"],
              "runtime_status": "NOT_PROVEN", "anchor_at": None,
              "previous_cutoff": None, "cutoff": None,
              "policy": dict(POLICY), "halt_required": False,
              "wallets": {}, "legs": [], "combined": None,
              "reason_codes": [], "inputs": inputs}
    for w in WALLETS:
        report["wallets"][w] = {"status": "UNRECONCILED", "opening": None, "closing": None,
                                 "movements": None, "residuals": None}
    try:
        anchor, previous, cutoff = (parse_time(inputs.get(k)) for k in
                                    ("anchor_at", "previous_cutoff", "cutoff"))
        _require(PROGRAM_START <= anchor <= previous < cutoff, "forward_window_invalid")
        _require((cutoff - previous).total_seconds() <= 86400, "forward_window_too_wide")
        for key, value in (("anchor_at", anchor), ("previous_cutoff", previous), ("cutoff", cutoff)):
            report[key] = value.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        report["reason_codes"] = ["forward_window_invalid"]
        return report

    account, binding = inputs["account_id"], inputs["binding"]
    normalized = {"opening": {}, "closing": {}}
    for name, at in (("opening", previous), ("closing", cutoff)):
        try:
            wallets = _snapshot(inputs.get(name), account=account, binding=binding, cutoff=at)
        except (ValueError, TypeError, OverflowError):
            reasons.add(name + "_snapshot_unproven")
            continue
        for wallet in WALLETS:
            try:
                values = _wallet(wallets.get(wallet), wallet=wallet, binding=binding, cutoff=at)
            except (ValueError, TypeError, OverflowError):
                reasons.add(name + "_" + wallet + "_unproven")
                continue
            normalized[name][wallet] = values
            report["wallets"][wallet][name] = {a: _text(q) for a, q in values.items()}

    events = inputs.get("events")
    event_rows = {surface: [] for surface in SURFACES}
    legs = []
    seen = set()
    if not isinstance(events, list) or len(events) > 10000:
        reasons.add("events_missing_or_invalid")
    else:
        for event in events:
            try:
                _require(isinstance(event, dict), "event_invalid")
                surface = SURFACE_BY_KIND.get(event.get("kind"))
                _require(surface is not None, "event_kind_unsupported")
                event_rows[surface].append(event)
                identity = event.get("id")
                _require(_opaque(identity), "event_identity_invalid")
                key = (surface, identity)
                _require(key not in seen, "event_duplicate")
                seen.add(key)
                _require(previous < parse_time(event.get("at")) <= cutoff, "event_outside_window")
                legs.extend(_legs(event))
            except (ValueError, TypeError, OverflowError):
                reasons.add("native_event_unproven")

    receipts = inputs.get("event_receipts")
    if not isinstance(receipts, dict):
        receipts = {}
    for surface in SURFACES:
        try:
            receipt = receipts.get(surface)
            _receipt(receipt, binding, cutoff, event_rows[surface])
            _require(parse_time(receipt.get("start_exclusive")) == previous
                     and parse_time(receipt.get("end_inclusive")) == cutoff,
                     "event_coverage_window_invalid")
            _require(receipt.get("universe") == "all_assets", "event_universe_incomplete")
        except (ValueError, TypeError, OverflowError):
            reasons.add(surface + "_coverage_unproven")

    # Invalid or incomplete event evidence never publishes a partially summed ledger.
    events_proven = not any(r == "native_event_unproven" or r == "events_missing_or_invalid"
                            or r.endswith("_coverage_unproven") for r in reasons)
    if events_proven:
        report["legs"] = [{**leg, "quantity": _text(leg["quantity"])} for leg in legs]
    universe = set()
    for snapshot in normalized.values():
        for values in snapshot.values():
            universe.update(values)
    universe.update(leg["asset"] for leg in legs)
    deltas = {w: dict.fromkeys(universe, Decimal(0)) for w in WALLETS}
    for leg in legs:
        deltas[leg["wallet"]][leg["asset"]] += leg["quantity"]
    for wallet in WALLETS:
        opening, closing = normalized["opening"].get(wallet), normalized["closing"].get(wallet)
        if opening is None or closing is None or not events_proven:
            continue
        if not universe.issubset(opening) or not universe.issubset(closing):
            reasons.add(wallet + "_asset_coverage_missing")
            continue
        residuals = {a: closing[a] - opening[a] - deltas[wallet][a] for a in sorted(universe)}
        row = report["wallets"][wallet]
        row["movements"] = {a: _text(deltas[wallet][a]) for a in sorted(universe)}
        row["residuals"] = {a: _text(q) for a, q in residuals.items()}
        if any(residuals.values()):
            reasons.add(wallet + "_quantity_residual")
        else:
            row["status"] = "MATCHED_SYNTHETIC"
    if all(report["wallets"][w]["residuals"] is not None for w in WALLETS):
        report["combined"] = {
            a: {"opening": _text(sum(normalized["opening"][w][a] for w in WALLETS)),
                "closing": _text(sum(normalized["closing"][w][a] for w in WALLETS)),
                "movement": _text(sum(deltas[w][a] for w in WALLETS)),
                "residual": _text(sum(Decimal(report["wallets"][w]["residuals"][a]) for w in WALLETS))}
            for a in sorted(universe)}
    report["reason_codes"] = sorted(reasons)
    if not reasons:
        report["status"] = "MATCHED_SYNTHETIC"
    return report


def forward_envelope(report):
    """Allowlist public fields; private quantities, identities and receipts stay local."""
    return {key: deepcopy(report[key]) for key in (
        "schema_version", "environment", "status", "runtime_status", "anchor_at",
        "previous_cutoff", "cutoff", "scope", "uncovered_scopes", "policy", "halt_required", "reason_codes")}
