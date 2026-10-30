"""Recompute declared strategy BTC ownership for no-submit proposals only."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


class AttributionBlocked(ValueError):
    """The supplied attribution evidence cannot support a proposal."""


def _blocked():
    raise AttributionBlocked("btc_attribution_unavailable")


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _blocked()
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        _blocked()
    if not result.is_finite() or result < 0 or result > Decimal("1000000000") or result.as_tuple().exponent < -18:
        _blocked()
    return result


def _utc(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() != timedelta(0):
            _blocked()
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _blocked()


def _identity(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


@dataclass(frozen=True)
class Attribution:
    spot: Decimal
    earn: Decimal
    fees: dict


def reconstruct(snapshot, *, as_of, account_scope, account_selector):
    """Recompute a declared ledger; completeness/source claims are not authentication."""
    keys = {"contract_version", "account_scope", "account_selector", "strategy_profile", "source", "source_version",
            "opening_at", "observed_through", "available_at", "complete", "opening_spot_btc", "opening_earn_btc",
            "fills", "transfers", "unsettled_orders", "observed_spot_free_btc", "observed_earn_btc"}
    if not isinstance(snapshot, Mapping) or set(snapshot) != keys:
        _blocked()
    if (snapshot["contract_version"] != "btc-attribution/v1" or snapshot["strategy_profile"] != "crypto_btc_dca"
            or not all(_identity(x) for x in (account_scope, account_selector, snapshot["source"], snapshot["source_version"]))
            or snapshot["account_scope"] != account_scope or snapshot["account_selector"] != account_selector
            or snapshot["complete"] is not True):
        _blocked()
    now, opened = _utc(as_of), _utc(snapshot["opening_at"])
    if not opened < _utc(snapshot["observed_through"]) == now or _utc(snapshot["available_at"]) != now:
        _blocked()
    if any(not isinstance(snapshot[k], list) for k in ("fills", "transfers", "unsettled_orders")):
        _blocked()
    if snapshot["unsettled_orders"] or len(snapshot["fills"]) + len(snapshot["transfers"]) > 10000:
        _blocked()
    spot, earn = _amount(snapshot["opening_spot_btc"]), _amount(snapshot["opening_earn_btc"])
    events, ids = [], set()
    for kind in ("fills", "transfers"):
        for event in snapshot[kind]:
            expected = ({"id", "at", "side", "btc_qty", "fee_asset", "fee_qty"} if kind == "fills"
                        else {"id", "at", "direction", "btc_qty"})
            if not isinstance(event, Mapping) or set(event) != expected or not _identity(event["id"]) or event["id"] in ids:
                _blocked()
            ids.add(event["id"])
            time = _utc(event["at"])
            if not opened < time <= now:
                _blocked()
            events.append((time, kind, event))
    events.sort(key=lambda item: item[0])
    if len({event[0] for event in events}) != len(events):
        _blocked()  # Ambiguous ordering cannot establish conservation.
    fees = {}
    for _, kind, event in events:
        quantity = _amount(event["btc_qty"])
        if quantity <= 0:
            _blocked()
        if kind == "fills":
            asset = event["fee_asset"]
            if not _identity(asset) or not asset.isascii() or not asset.isalnum() or asset != asset.upper():
                _blocked()
            fee = _amount(event["fee_qty"])
            fees[asset] = fees.get(asset, Decimal(0)) + fee
            if event["side"] == "BUY":
                spot += quantity
            elif event["side"] == "SELL":
                spot -= quantity
            else:
                _blocked()
            if asset == "BTC":
                spot -= fee
        elif event["direction"] == "spot_to_earn":
            spot -= quantity
            earn += quantity
        elif event["direction"] == "earn_to_spot":
            earn -= quantity
            spot += quantity
        else:
            _blocked()
        if spot < 0 or earn < 0 or spot > Decimal("1000000000") or earn > Decimal("1000000000"):
            _blocked()
    if spot != _amount(snapshot["observed_spot_free_btc"]) or earn != _amount(snapshot["observed_earn_btc"]):
        _blocked()
    return Attribution(spot=spot, earn=earn, fees=fees)


def read_btc_balance_with_parts(get_total_balance_fn, client, asset, *, log_buffer, output):
    """Capture BTC or USDT parts of existing balance reads without extra calls."""
    if asset not in {"BTC", "USDT"}:
        return get_total_balance_fn(client, asset, log_buffer=log_buffer)
    captured = {}

    class CaptureClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def get_asset_balance(self, **kwargs):
            response = client.get_asset_balance(**kwargs)
            captured["spot"] = response
            return response

        def get_simple_earn_flexible_product_position(self, **kwargs):
            response = client.get_simple_earn_flexible_product_position(**kwargs)
            captured["earn"] = response
            return response

    total = get_total_balance_fn(CaptureClient(), asset, log_buffer=log_buffer)
    try:
        spot = captured["spot"]
        earn = captured["earn"]
        free, locked = _amount(spot["free"]), _amount(spot["locked"])
        if not isinstance(earn, Mapping) or not isinstance(earn.get("rows"), list):
            _blocked()
        earn_total = sum((_amount(row["totalAmount"]) for row in earn["rows"]), Decimal(0))
        output.update(spot_free=str(free), spot_locked=str(locked), earn=str(earn_total))
        if asset == "USDT":
            output["managed_total"] = total
    except (KeyError, TypeError, AttributionBlocked):
        output.clear()
    return total
