"""Binance runtime infrastructure helpers for BinancePlatform."""

from __future__ import annotations

from math import isfinite

from runtime_support import ExecutionIntegrityError, get_spot_balance


def resolve_runtime_btc_snapshot(
    runtime,
    btc_price,
    log_buffer,
    *,
    fetch_btc_market_snapshot_fn,
    max_attempts=1,
    retry_delays=(),
    sleep_fn=None,
    append_log_fn=None,
    retry_log_message_fn=None,
):
    if runtime.btc_market_snapshot is not None:
        return dict(runtime.btc_market_snapshot)

    attempts = max(1, int(max_attempts))
    delays = tuple(retry_delays or ())
    for attempt in range(1, attempts + 1):
        snapshot = fetch_btc_market_snapshot_fn(runtime.client, btc_price, log_buffer=log_buffer)
        if snapshot is not None:
            return snapshot
        if attempt >= attempts:
            return None

        delay_seconds = 0
        if delays:
            delay_seconds = max(0, delays[min(attempt - 1, len(delays) - 1)])
        if append_log_fn is not None and retry_log_message_fn is not None:
            append_log_fn(log_buffer, retry_log_message_fn(attempt + 1, attempts, delay_seconds))
        if sleep_fn is not None and delay_seconds > 0:
            sleep_fn(delay_seconds)

    return None


def resolve_runtime_trend_indicators(runtime, trend_universe_symbols, *, fetch_daily_indicators_fn):
    if runtime.trend_indicator_snapshots is None:
        trend_indicators = {}
        for symbol in trend_universe_symbols:
            trend_indicators[symbol] = fetch_daily_indicators_fn(runtime.client, symbol)
        return trend_indicators
    return {
        symbol: runtime.trend_indicator_snapshots.get(symbol)
        for symbol in trend_universe_symbols
    }


def ensure_asset_available_runtime(
    runtime,
    report,
    asset,
    required_amount,
    log_buffer,
    *,
    runtime_call_client_fn,
    append_log_fn,
    runtime_notify_fn,
    translate_fn,
    sleep_fn,
):
    try:
        required = float(required_amount)
        if not isfinite(required) or required < 0:
            raise ValueError("invalid_required_amount")
        return get_spot_balance(runtime.client, asset, free_only=True) >= required
    except Exception:
        raise ExecutionIntegrityError("asset_availability_failed") from None


def ensure_runtime_client(
    runtime,
    report,
    *,
    connect_client_fn,
    append_report_error_fn,
    runtime_notify_fn,
    translate_fn,
    sleep_fn,
    max_retries=3,
):
    if runtime.client is not None:
        return True

    for attempt in range(max_retries):
        try:
            runtime.client = connect_client_fn(runtime.api_key, runtime.api_secret, timeout=30)
            return True
        except Exception:
            if attempt < max_retries - 1:
                sleep_fn(3)
                continue
            append_report_error_fn(report, "client_connection_failed", stage="client")
            report["status"] = "aborted"
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('api_error')}\n"
                f"{translate_fn('error_label')}: client_connection_failed",
            )
            return False

    return False
