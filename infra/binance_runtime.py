"""Binance runtime infrastructure helpers for BinancePlatform."""

from __future__ import annotations

from math import isfinite

from infra.closed_daily_bars import ClosedDailyBarsError, closed_daily_bars
from runtime_support import ExecutionIntegrityError


def _require_latest_btc_bar_computable(bars):
    """Match the pinned BTC helper's required fields at the last closed bar."""
    import numpy as np
    import pandas as pd

    try:
        values = []
        for row in bars:
            if len(row) < 6 or any(isinstance(value, bool) for value in row[1:6]):
                raise ValueError
            opened, high, low, close, volume = (float(value) for value in row[1:6])
            if (not all(isfinite(value) for value in (opened, high, low, close, volume))
                    or min(opened, high, low, close) <= 0 or volume < 0
                    or high < max(opened, low, close) or low > min(opened, high, close)):
                raise ValueError
            values.append(close)
        close = pd.Series(values, dtype=float)
        ma200 = close.rolling(200).mean()
        std200 = close.rolling(200).std()
        zscore = (close - ma200) / std200
        required = (
            ma200, zscore, np.exp(np.log(close).rolling(200).mean()),
            zscore.rolling(365).quantile(0.95).clip(lower=2.5), ma200.pct_change(20),
            close.pct_change(20), close.pct_change(60), close.pct_change(120),
        )
        if not all(isfinite(float(series.iloc[-1])) for series in required):
            raise ValueError
    except (ValueError, TypeError, IndexError, OverflowError):
        raise ClosedDailyBarsError("btc_latest_closed_bar_invalid") from None


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
    closed_bar_validation=False,
):
    if runtime.btc_market_snapshot is not None:
        if closed_bar_validation:
            raise ClosedDailyBarsError("injected_btc_snapshot_forbidden")
        return dict(runtime.btc_market_snapshot)

    attempts = max(1, int(max_attempts))
    delays = tuple(retry_delays or ())
    for attempt in range(1, attempts + 1):
        client = runtime.client
        if closed_bar_validation:
            rows = client.get_historical_klines("BTCUSDT", "1d", "700 days ago UTC")
            bars = closed_daily_bars(rows, runtime.now_utc)
            _require_latest_btc_bar_computable(bars)

            class SnapshotClient:
                def get_historical_klines(self, symbol, interval, lookback):
                    if (symbol, interval, lookback) != ("BTCUSDT", "1d", "700 days ago UTC"):
                        raise ClosedDailyBarsError("daily_bar_request_invalid")
                    return bars

            client = SnapshotClient()
        snapshot = fetch_btc_market_snapshot_fn(client, btc_price, log_buffer=log_buffer)
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


def resolve_runtime_trend_indicators(runtime, trend_universe_symbols, *, fetch_daily_indicators_fn,
                                     closed_bar_validation=False):
    if not closed_bar_validation:
        if runtime.trend_indicator_snapshots is None:
            trend_indicators = {}
            for symbol in trend_universe_symbols:
                trend_indicators[symbol] = fetch_daily_indicators_fn(runtime.client, symbol)
            return trend_indicators
        return {
            symbol: runtime.trend_indicator_snapshots.get(symbol)
            for symbol in trend_universe_symbols
        }
    if runtime.trend_indicator_snapshots is not None:
        raise ClosedDailyBarsError("injected_daily_indicators_forbidden")
    symbols = list(trend_universe_symbols)
    if "BTCUSDT" not in symbols:
        symbols.append("BTCUSDT")
    trend_indicators = {}
    for symbol in symbols:
        rows = runtime.client.get_historical_klines(symbol, "1d", "420 days ago UTC")
        bars = closed_daily_bars(rows, runtime.now_utc)

        class SnapshotClient:
            def get_historical_klines(self, requested_symbol, interval, lookback):
                if (requested_symbol, interval, lookback) != (symbol, "1d", "420 days ago UTC"):
                    raise ClosedDailyBarsError("daily_bar_request_invalid")
                return bars

        indicators = fetch_daily_indicators_fn(SnapshotClient(), symbol)
        numeric_keys = ("close", "sma20", "sma60", "sma200", "roc20", "roc60", "roc120",
                        "vol20", "atr14", "avg_quote_vol_30", "avg_quote_vol_90",
                        "avg_quote_vol_180", "trend_persist_90", "age_days")
        if (not isinstance(indicators, dict) or any(
                isinstance(indicators.get(key), bool) or not isinstance(indicators.get(key), (int, float))
                or not isfinite(indicators[key]) for key in numeric_keys
        ) or any(indicators[key] <= 0 for key in ("close", "sma20", "sma60", "sma200", "age_days"))
                or any(indicators[key] < 0 for key in ("vol20", "atr14", "avg_quote_vol_30",
                                                       "avg_quote_vol_90", "avg_quote_vol_180"))):
            raise ClosedDailyBarsError("closed_daily_indicators_unavailable")
        trend_indicators[symbol] = indicators
    return trend_indicators


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
    """Check available Spot cash without reading or moving Earn funds."""
    try:
        spot_free = float(runtime.client.get_asset_balance(asset=asset)["free"])
        if not isfinite(spot_free) or spot_free < 0 or not isfinite(required_amount) or required_amount <= 0:
            raise ExecutionIntegrityError("asset_availability_failed")
        if spot_free >= required_amount:
            return True

    except ExecutionIntegrityError:
        raise
    except Exception:
        raise ExecutionIntegrityError("asset_availability_failed") from None
    return False


def manage_usdt_earn_buffer_runtime(
    runtime,
    report,
    target_buffer,
    log_buffer,
    *,
    runtime_call_client_fn,
    append_log_fn,
    translate_fn,
    spot_free_override=None,
):
    """Leave Earn funds untouched; automatic buffer maintenance is disabled."""
    return


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
