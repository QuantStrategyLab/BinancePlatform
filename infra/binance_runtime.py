"""Binance runtime infrastructure helpers for BinancePlatform."""

from __future__ import annotations


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
        spot_free = float(runtime.client.get_asset_balance(asset=asset)["free"])
        if spot_free >= required_amount:
            return True

        shortfall = required_amount - spot_free
        earn_positions = runtime.client.get_simple_earn_flexible_product_position(asset=asset)
        if earn_positions and "rows" in earn_positions and len(earn_positions["rows"]) > 0:
            row = earn_positions["rows"][0]
            product_id = row["productId"]
            earn_free = float(row["totalAmount"])
            if earn_free > 0:
                redeem_amt = round(min(shortfall * 1.001, earn_free), 8)
                intent = {
                    "asset": str(asset),
                    "action": "redeem",
                    "product_id": str(product_id),
                    "amount": float(redeem_amt),
                    "reason": "asset_availability",
                }
                report["redemption_subscription_intents"].append(intent)
                runtime_call_client_fn(
                    runtime,
                    report,
                    method_name="redeem_simple_earn_flexible_product",
                    payload={"productId": product_id, "amount": redeem_amt},
                    effect_type="earn_redeem",
                )
                append_log_fn(
                    log_buffer,
                    translate_fn("execution_spot_short_redeeming_from_earn", asset=asset, amount=redeem_amt),
                )
                if not runtime.dry_run:
                    sleep_fn(3)
                return True
    except Exception as exc:
        runtime_notify_fn(
            runtime,
            report,
            f"{translate_fn('redeem_failed')} {asset}\n"
            f"{translate_fn('error_label')}: {exc}",
        )
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
    try:
        asset = "USDT"
        if spot_free_override is None:
            spot_free = float(runtime.client.get_asset_balance(asset=asset)["free"])
        else:
            spot_free = max(0.0, float(spot_free_override))

        earn_list = runtime.client.get_simple_earn_flexible_product_list(asset=asset)
        if not earn_list or "rows" not in earn_list or len(earn_list["rows"]) == 0:
            return
        product_id = earn_list["rows"][0]["productId"]

        if spot_free > target_buffer + 5.0:
            excess = round(spot_free - target_buffer, 4)
            if excess >= 0.1:
                report["redemption_subscription_intents"].append(
                    {
                        "asset": asset,
                        "action": "subscribe",
                        "product_id": str(product_id),
                        "amount": float(excess),
                        "reason": "maintain_usdt_buffer",
                    }
                )
                runtime_call_client_fn(
                    runtime,
                    report,
                    method_name="subscribe_simple_earn_flexible_product",
                    payload={"productId": product_id, "amount": excess},
                    effect_type="earn_subscribe",
                )
                append_log_fn(log_buffer, translate_fn("cash_manager_subscribed_to_earn", amount=excess))
        elif spot_free < target_buffer - 5.0:
            shortfall = round(target_buffer - spot_free, 4)
            earn_positions = runtime.client.get_simple_earn_flexible_product_position(asset=asset)
            if earn_positions and "rows" in earn_positions and len(earn_positions["rows"]) > 0:
                earn_free = float(earn_positions["rows"][0]["totalAmount"])
                if earn_free > 0:
                    redeem_amt = round(min(shortfall, earn_free), 8)
                    report["redemption_subscription_intents"].append(
                        {
                            "asset": asset,
                            "action": "redeem",
                            "product_id": str(product_id),
                            "amount": float(redeem_amt),
                            "reason": "maintain_usdt_buffer",
                        }
                    )
                    runtime_call_client_fn(
                        runtime,
                        report,
                        method_name="redeem_simple_earn_flexible_product",
                        payload={"productId": product_id, "amount": redeem_amt},
                        effect_type="earn_redeem",
                    )
                    append_log_fn(log_buffer, translate_fn("cash_manager_redeeming_to_spot", amount=redeem_amt))
    except Exception as exc:
        append_log_fn(log_buffer, translate_fn("usdt_earn_buffer_maintenance_failed", error=exc))


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
        except Exception as exc:
            if attempt < max_retries - 1:
                sleep_fn(3)
                continue
            append_report_error_fn(report, f"Unable to connect Binance API: {exc}", stage="client")
            report["status"] = "aborted"
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('api_error')}\n"
                f"{translate_fn('error_label')}: {str(exc)}",
            )
            return False

    return False
