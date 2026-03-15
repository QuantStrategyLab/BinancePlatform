from __future__ import annotations

from typing import Any, Callable, Mapping


def capture_market_snapshot(
    runtime,
    report: dict[str, Any],
    runtime_trend_universe: Mapping[str, Mapping[str, Any]],
    log_buffer,
    min_bnb_value: float,
    buy_bnb_amount: float,
    *,
    get_total_balance_fn: Callable[..., float],
    ensure_asset_available_fn: Callable[..., bool],
    runtime_call_client_fn: Callable[..., Any],
    runtime_notify_fn: Callable[..., Any],
    append_log_fn: Callable[..., Any],
    resolve_btc_snapshot_fn: Callable[..., Any],
    resolve_trend_indicators_fn: Callable[..., Any],
    bnb_fuel_symbol: str = "BNBUSDT",
    bnb_fuel_asset: str = "BNB",
) -> dict[str, Any]:
    u_total = get_total_balance_fn(runtime.client, "USDT", log_buffer=log_buffer)
    bnb_total = get_total_balance_fn(runtime.client, bnb_fuel_asset, log_buffer=log_buffer)
    bnb_price = float(runtime.client.get_avg_price(symbol=bnb_fuel_symbol)["price"])
    dynamic_usdt_buffer = max(50.0, min(u_total * 0.05, 300.0))

    if bnb_total * bnb_price < min_bnb_value and u_total >= buy_bnb_amount:
        report["buy_sell_intents"].append(
            {
                "category": "fuel",
                "action": "buy",
                "symbol": bnb_fuel_symbol,
                "quote_order_qty": buy_bnb_amount,
            }
        )
        try:
            if not ensure_asset_available_fn(runtime, report, "USDT", buy_bnb_amount, log_buffer):
                raise RuntimeError("USDT spot buffer unavailable for BNB top-up")
            runtime_call_client_fn(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": bnb_fuel_symbol, "quoteOrderQty": buy_bnb_amount},
                effect_type="order_buy",
            )
            u_total -= buy_bnb_amount
            bnb_total += (buy_bnb_amount * 0.995) / bnb_price
            append_log_fn(log_buffer, "🔧 BNB top-up completed")
        except Exception as exc:
            runtime_notify_fn(runtime, report, f"⚠️ BNB top-up failed: {exc}")

    prices = {}
    balances = {}
    for symbol, config in runtime_trend_universe.items():
        prices[symbol] = float(runtime.client.get_avg_price(symbol=symbol)["price"])
        balances[symbol] = get_total_balance_fn(runtime.client, config["base_asset"], log_buffer=log_buffer)

    btc_price = float(runtime.client.get_avg_price(symbol="BTCUSDT")["price"])
    balances["BTCUSDT"] = get_total_balance_fn(runtime.client, "BTC", log_buffer=log_buffer)
    prices["BTCUSDT"] = btc_price

    btc_snapshot = resolve_btc_snapshot_fn(runtime, btc_price, log_buffer)
    if btc_snapshot is None:
        raise RuntimeError("BTC indicators insufficient for rotation and DCA")

    return {
        "u_total": u_total,
        "fuel_val": bnb_total * bnb_price,
        "dynamic_usdt_buffer": dynamic_usdt_buffer,
        "prices": prices,
        "balances": balances,
        "btc_snapshot": btc_snapshot,
        "trend_indicators": resolve_trend_indicators_fn(runtime),
    }
