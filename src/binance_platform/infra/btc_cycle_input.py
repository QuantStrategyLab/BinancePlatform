"""Validate declared BTC cycle inputs for the no-submit evaluation only."""

import json
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path


class BtcCycleInputError(ValueError):
    pass


def cycle_failure_metadata(exc, *, stage, validation_scope):
    """Project only the fixed no-submit input failure, never provider text."""
    if (validation_scope == "full_cycle_no_submit" and stage == "portfolio_allocation"
            and type(exc) is BtcCycleInputError and exc.args == ("btc_cycle_input_unavailable",)):
        return {"error_type": "btc_cycle_input_error", "reason_code": "btc_cycle_input_unavailable"}
    return {}


def _fail():
    raise BtcCycleInputError("btc_cycle_input_unavailable")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def read_envelope(path):
    """Read using the existing local/GCS mechanism, without exposing input details."""
    try:
        if not isinstance(path, str) or not path:
            _fail()
        if path.startswith("gs://"):
            from quant_platform_kit.cloud import get_object_store
            raw = get_object_store().read_text(path)
        else:
            raw = Path(path).read_text(encoding="utf-8")
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _fail())
    except Exception:
        raise BtcCycleInputError("btc_cycle_input_unavailable") from None


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _fail()
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() != timedelta(0)):
        _fail()
    return value.astimezone(timezone.utc)


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail()
    if not math.isfinite(number) or number <= 0:
        _fail()
    return number


def adapt_btc_cycle_input(indicators, envelope, *, as_of, max_age_seconds, price, ma200):
    """Return a copied indicator mapping; metadata is asserted, not authenticated."""
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "contract_version", "source", "source_version", "symbol", "observed_at", "available_at", "ahr999"
    }:
        _fail()
    if envelope["contract_version"] != "btc-cycle-input/v1" or envelope["symbol"] != "BTCUSDT":
        _fail()
    if any(not isinstance(envelope[key], str) or not envelope[key].strip() for key in ("source", "source_version")):
        _fail()
    now, observed, available = _utc(as_of), _utc(envelope["observed_at"]), _utc(envelope["available_at"])
    max_age_seconds = _positive(max_age_seconds)
    if (observed > available or available > now or (now - observed).total_seconds() > max_age_seconds):
        _fail()
    ahr, price, ma200 = _positive(envelope["ahr999"]), _positive(price), _positive(ma200)
    if not isinstance(indicators, Mapping):
        _fail()
    output = dict(indicators)
    aliases = [key for key in output if isinstance(key, str) and key.strip().upper().replace("-", "")
               in {"BTC", "BTCUSD", "BTCUSDT"}]
    if len(aliases) > 1:
        _fail()
    # A top-level indicator payload would take precedence over the symbol entry.
    if any(isinstance(key, str) and key.strip().lower() in {"close", "price", "ahr999", "ahr_999", "ahr999_gma"}
           for key in output):
        _fail()
    existing = output.pop(aliases[0]) if aliases else {}
    if not isinstance(existing, Mapping):
        _fail()
    payload = dict(existing)
    fields = {"close": price, "price": price, "last": price, "last_price": price,
              "ma200": ma200, "sma200": ma200, "sma_200": ma200,
              "ahr999": ahr, "ahr_999": ahr, "ahr999_gma": ahr, "ahr999_index": ahr}
    seen = set()
    for key, value in payload.items():
        normalized = str(key).strip().lower()
        if normalized not in fields:
            _fail()
        if normalized in seen or _positive(value) != fields[normalized]:
            _fail()
        seen.add(normalized)
    payload.update({"close": price, "ma200": ma200, "ahr999": ahr})
    output["BTCUSDT"] = payload
    return output
