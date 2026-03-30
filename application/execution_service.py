"""Application-level trade execution helpers for BinancePlatform."""

from __future__ import annotations


def run_daily_circuit_breaker(
    runtime,
    report,
    state,
    runtime_trend_universe,
    balances,
    u_total,
    prices,
    trend_daily_pnl,
    circuit_breaker_pct,
    log_buffer,
    *,
    format_qty_fn,
    runtime_notify_fn,
    ensure_asset_available_fn,
    runtime_call_client_fn,
    set_symbol_trade_state_fn,
    runtime_set_trade_state_fn,
    build_balance_snapshot_fn,
    translate_fn,
):
    if trend_daily_pnl > circuit_breaker_pct:
        return False

    for symbol, config in runtime_trend_universe.items():
        tradable_qty = balances[symbol]
        if tradable_qty * prices[symbol] <= 10:
            continue
        qty = format_qty_fn(runtime.client, symbol, tradable_qty)
        report["buy_sell_intents"].append(
            {
                "category": "trend",
                "action": "sell",
                "symbol": symbol,
                "reason": "daily_circuit_breaker",
                "quantity": float(qty),
            }
        )
        try:
            if qty <= 0:
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('circuit_breaker_sell_skipped')} {symbol}\n"
                    f"{translate_fn('qty_zero_msg')}",
                )
                continue
            if not ensure_asset_available_fn(runtime, report, config["base_asset"], qty, log_buffer):
                raise RuntimeError(
                    translate_fn("asset_unavailable_for_circuit_breaker_sell", asset=config["base_asset"])
                )
            runtime_call_client_fn(
                runtime,
                report,
                method_name="order_market_sell",
                payload={"symbol": symbol, "quantity": qty},
                effect_type="order_sell",
            )
            balances[symbol] = max(0.0, balances[symbol] - qty)
            u_total += qty * prices[symbol]
            set_symbol_trade_state_fn(
                state,
                symbol,
                {"is_holding": False, "entry_price": 0.0, "highest_price": 0.0},
            )
        except Exception as exc:
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('circuit_breaker_sell_failed')} {symbol}\n"
                f"{translate_fn('error_label')}: {exc}",
            )

    state.update({"is_circuit_broken": True})
    state["last_balance_snapshot"] = build_balance_snapshot_fn(runtime_trend_universe, balances, u_total)
    report["circuit_breaker_triggered"] = True
    runtime_set_trade_state_fn(runtime, report, state, reason="daily_circuit_breaker")
    runtime_notify_fn(
        runtime,
        report,
        f"{translate_fn('circuit_breaker')}\n"
        f"{translate_fn('circuit_msg', pnl=f'{trend_daily_pnl:.2%}')}",
    )
    return True


def execute_trend_sells(
    runtime,
    report,
    state,
    runtime_trend_universe,
    selected_candidates,
    trend_indicators,
    prices,
    balances,
    u_total,
    log_buffer,
    today_id_str,
    atr_multiplier,
    *,
    get_trend_sell_reason_fn,
    should_skip_duplicate_trend_action_fn,
    append_log_fn,
    translate_fn,
    format_qty_fn,
    ensure_asset_available_fn,
    runtime_call_client_fn,
    next_order_id_fn,
    set_symbol_trade_state_fn,
    record_trend_action_fn,
    runtime_set_trade_state_fn,
    runtime_notify_fn,
):
    for symbol, config in runtime_trend_universe.items():
        curr_price = prices[symbol]
        sell_reason = get_trend_sell_reason_fn(
            state,
            symbol,
            curr_price,
            trend_indicators.get(symbol),
            selected_candidates,
            atr_multiplier,
        )
        if not sell_reason:
            continue

        if should_skip_duplicate_trend_action_fn(state, symbol, "sell", today_id_str):
            append_log_fn(log_buffer, translate_fn("duplicate_sell_skipped", symbol=symbol))
            continue

        qty = format_qty_fn(runtime.client, symbol, balances[symbol])
        report["buy_sell_intents"].append(
            {
                "category": "trend",
                "action": "sell",
                "symbol": symbol,
                "reason": sell_reason,
                "quantity": float(qty),
                "price": float(curr_price),
            }
        )
        try:
            if qty <= 0:
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('trend_sell_skipped')} {symbol}\n"
                    f"{translate_fn('reason_label')}: {sell_reason}\n"
                    f"{translate_fn('qty_zero_msg')}",
                )
                continue
            if not ensure_asset_available_fn(runtime, report, config["base_asset"], qty, log_buffer):
                raise RuntimeError(translate_fn("asset_unavailable_for_trend_sell", asset=config["base_asset"]))
            runtime_call_client_fn(
                runtime,
                report,
                method_name="order_market_sell",
                payload={
                    "symbol": symbol,
                    "quantity": qty,
                    "newClientOrderId": next_order_id_fn(runtime, "T_SELL", symbol),
                },
                effect_type="order_sell",
            )
            balances[symbol] = max(0.0, balances[symbol] - qty)
            u_total += qty * curr_price
            set_symbol_trade_state_fn(
                state,
                symbol,
                {"is_holding": False, "entry_price": 0.0, "highest_price": 0.0},
            )
            record_trend_action_fn(state, symbol, "sell", today_id_str)
            runtime_set_trade_state_fn(runtime, report, state, reason=f"trend_sell:{symbol}")
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('trend_sell')} {symbol}\n"
                f"{translate_fn('reason_label')}: {sell_reason}\n"
                f"{translate_fn('price_label')}: ${curr_price:.2f}",
            )
        except Exception as exc:
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('trend_sell_failed')} {symbol}\n"
                f"{translate_fn('reason_label')}: {sell_reason}\n"
                f"{translate_fn('error_label')}: {exc}",
            )

    return u_total


def execute_trend_buys(
    runtime,
    report,
    state,
    selected_candidates,
    eligible_buy_symbols,
    planned_trend_buys,
    prices,
    balances,
    u_total,
    log_buffer,
    today_id_str,
    *,
    should_skip_duplicate_trend_action_fn,
    append_log_fn,
    translate_fn,
    format_qty_fn,
    ensure_asset_available_fn,
    runtime_call_client_fn,
    next_order_id_fn,
    set_symbol_trade_state_fn,
    record_trend_action_fn,
    runtime_set_trade_state_fn,
    runtime_notify_fn,
):
    for symbol in eligible_buy_symbols:
        curr_price = prices[symbol]
        candidate_meta = selected_candidates[symbol]
        buy_u = planned_trend_buys.get(symbol, 0.0)
        if buy_u <= 15:
            continue

        if should_skip_duplicate_trend_action_fn(state, symbol, "buy", today_id_str):
            append_log_fn(log_buffer, translate_fn("duplicate_buy_skipped", symbol=symbol))
            continue

        qty = format_qty_fn(runtime.client, symbol, buy_u * 0.985 / curr_price)
        usdt_cost = qty * curr_price
        report["buy_sell_intents"].append(
            {
                "category": "trend",
                "action": "buy",
                "symbol": symbol,
                "quantity": float(qty),
                "budget": float(buy_u),
                "weight": float(candidate_meta["weight"]),
                "relative_score": float(candidate_meta["relative_score"]),
            }
        )
        try:
            if qty <= 0 or usdt_cost <= 0:
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('trend_buy_skipped')} {symbol}\n"
                    f"{translate_fn('budget_label')}: ${buy_u:.2f}\n"
                    f"{translate_fn('qty_zero_msg')}",
                )
                continue
            if not ensure_asset_available_fn(runtime, report, "USDT", usdt_cost, log_buffer):
                raise RuntimeError(translate_fn("usdt_unavailable_for_trend_buy"))
            runtime_call_client_fn(
                runtime,
                report,
                method_name="order_market_buy",
                payload={
                    "symbol": symbol,
                    "quantity": qty,
                    "newClientOrderId": next_order_id_fn(runtime, "T_BUY", symbol),
                },
                effect_type="order_buy",
            )
            set_symbol_trade_state_fn(
                state,
                symbol,
                {"is_holding": True, "entry_price": curr_price, "highest_price": curr_price},
            )
            balances[symbol] += qty
            u_total -= usdt_cost
            record_trend_action_fn(state, symbol, "buy", today_id_str)
            runtime_set_trade_state_fn(runtime, report, state, reason=f"trend_buy:{symbol}")
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('trend_buy')} {symbol}\n"
                f"{translate_fn('price_label')}: ${curr_price:.2f}\n"
                f"{translate_fn('budget_label')}: ${buy_u:.2f}\n"
                f"{translate_fn('weight_label')}: {candidate_meta['weight']:.0%}\n"
                f"{translate_fn('rel_score_label')}: {candidate_meta['relative_score']:.2f}",
            )
        except Exception as exc:
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('trend_buy_failed')} {symbol}\n"
                f"{translate_fn('budget_label')}: ${buy_u:.2f}\n"
                f"{translate_fn('error_label')}: {exc}",
            )

    return u_total


def execute_trend_rotation(
    runtime,
    report,
    state,
    runtime_trend_universe,
    trend_indicators,
    btc_snapshot,
    prices,
    balances,
    u_total,
    fuel_val,
    log_buffer,
    today_id_str,
    allow_new_trend_entries,
    allow_pool_refresh,
    atr_multiplier,
    *,
    refresh_rotation_pool,
    select_rotation_weights,
    append_rotation_summary,
    compute_portfolio_allocation,
    execute_trend_sells,
    plan_trend_buys,
    execute_trend_buys,
    append_trend_symbol_status,
    rotation_top_n,
    official_trend_pool_symbols,
):
    active_trend_pool, _ = refresh_rotation_pool(
        state,
        trend_indicators,
        btc_snapshot,
        allow_refresh=allow_pool_refresh,
        now_utc=runtime.now_utc,
    )
    selected_candidates = select_rotation_weights(
        trend_indicators,
        prices,
        btc_snapshot,
        active_trend_pool,
        rotation_top_n,
    )
    report["selected_symbols"]["active_trend_pool"] = list(active_trend_pool)
    report["selected_symbols"]["selected_candidates"] = list(selected_candidates.keys())

    append_rotation_summary(
        log_buffer,
        official_trend_pool_symbols,
        active_trend_pool,
        selected_candidates,
    )
    u_total = execute_trend_sells(
        runtime,
        report,
        state,
        runtime_trend_universe,
        selected_candidates,
        trend_indicators,
        prices,
        balances,
        u_total,
        log_buffer,
        today_id_str,
        atr_multiplier,
    )

    current_allocation = compute_portfolio_allocation(
        runtime_trend_universe,
        balances,
        prices,
        u_total,
        fuel_val,
    )
    eligible_buy_symbols, planned_trend_buys = plan_trend_buys(
        state,
        runtime_trend_universe,
        selected_candidates,
        trend_indicators,
        prices,
        current_allocation["trend_usdt_pool"],
        allow_new_trend_entries,
    )
    u_total = execute_trend_buys(
        runtime,
        report,
        state,
        selected_candidates,
        eligible_buy_symbols,
        planned_trend_buys,
        prices,
        balances,
        u_total,
        log_buffer,
        today_id_str,
    )
    append_trend_symbol_status(
        log_buffer,
        runtime_trend_universe,
        prices,
        trend_indicators,
        state,
        btc_snapshot,
    )
    return u_total


def _resolve_btc_buy_multiplier(ahr):
    if ahr < 0.45:
        return 5
    if ahr < 0.8:
        return 2
    if ahr < 1.2:
        return 1
    return 0


def _resolve_btc_trim_sell_pct(zscore):
    sell_pct = 0.1
    if zscore > 4.0:
        sell_pct = 0.3
    if zscore > 5.0:
        sell_pct = 0.5
    return sell_pct


def execute_btc_dca_cycle(
    runtime,
    report,
    state,
    balances,
    prices,
    u_total,
    total_equity,
    dca_usdt_pool,
    dca_val,
    btc_snapshot,
    btc_target_ratio,
    today_id_str,
    log_buffer,
    *,
    append_log_fn,
    translate_fn,
    get_dynamic_btc_base_order,
    format_qty_fn,
    ensure_asset_available_fn,
    runtime_call_client_fn,
    next_order_id_fn,
    runtime_notify_fn,
    runtime_set_trade_state_fn,
):
    if dca_usdt_pool <= 10 and dca_val <= 10:
        return u_total

    btc_price = prices["BTCUSDT"]
    ahr = btc_snapshot["ahr999"]
    zscore = btc_snapshot["zscore"]
    sell_trigger = btc_snapshot["sell_trigger"]
    append_log_fn(
        log_buffer,
        translate_fn(
            "btc_accumulation_radar_line",
            ahr=ahr,
            zscore=zscore,
            sell_trigger=sell_trigger,
        ),
    )

    base_order = get_dynamic_btc_base_order(total_equity)
    multiplier = _resolve_btc_buy_multiplier(ahr)

    if multiplier > 0 and dca_usdt_pool > 15 and state.get("dca_last_buy_date") != today_id_str:
        budget = min(dca_usdt_pool, base_order * multiplier)
        qty = format_qty_fn(runtime.client, "BTCUSDT", budget * 0.985 / btc_price)
        buy_cost = qty * btc_price
        report["btc_dca_intents"].append(
            {
                "action": "buy",
                "symbol": "BTCUSDT",
                "quantity": float(qty),
                "budget": float(budget),
                "ahr999": float(ahr),
            }
        )
        try:
            if qty <= 0 or buy_cost <= 0:
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('btc_dca_buy_skipped')}\n"
                    f"{translate_fn('qty_zero_msg')}",
                )
            else:
                if not ensure_asset_available_fn(runtime, report, "USDT", buy_cost, log_buffer):
                    raise RuntimeError(translate_fn("usdt_unavailable_for_btc_dca_buy"))
                runtime_call_client_fn(
                    runtime,
                    report,
                    method_name="order_market_buy",
                    payload={
                        "symbol": "BTCUSDT",
                        "quantity": qty,
                        "newClientOrderId": next_order_id_fn(runtime, "D_BUY", "BTCUSDT"),
                    },
                    effect_type="order_buy",
                )
                balances["BTCUSDT"] += qty
                u_total -= buy_cost
                state["dca_last_buy_date"] = today_id_str
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('btc_dca_buy')} BTC\n"
                    f"{translate_fn('ahr999')}: {ahr:.2f}\n"
                    f"{translate_fn('target_alloc_label')}: {btc_target_ratio:.1%}\n"
                    f"{translate_fn('quantity_label')}: {qty} BTC",
                )
                runtime_set_trade_state_fn(runtime, report, state, reason="btc_dca_buy")
        except Exception as exc:
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('btc_dca_buy_failed')} BTC\n"
                f"{translate_fn('error_label')}: {exc}",
            )

    if zscore > sell_trigger and dca_val > 20 and state.get("dca_last_sell_date") != today_id_str:
        sell_pct = _resolve_btc_trim_sell_pct(zscore)
        qty = format_qty_fn(runtime.client, "BTCUSDT", balances["BTCUSDT"] * sell_pct)
        report["btc_dca_intents"].append(
            {
                "action": "sell",
                "symbol": "BTCUSDT",
                "quantity": float(qty),
                "sell_pct": float(sell_pct),
                "zscore": float(zscore),
            }
        )
        try:
            if qty <= 0:
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('btc_dca_trim_skipped')}\n"
                    f"{translate_fn('qty_zero_msg')}",
                )
            else:
                if not ensure_asset_available_fn(runtime, report, "BTC", qty, log_buffer):
                    raise RuntimeError(translate_fn("btc_unavailable_for_dca_sell"))
                runtime_call_client_fn(
                    runtime,
                    report,
                    method_name="order_market_sell",
                    payload={
                        "symbol": "BTCUSDT",
                        "quantity": qty,
                        "newClientOrderId": next_order_id_fn(runtime, "D_SELL", "BTCUSDT"),
                    },
                    effect_type="order_sell",
                )
                balances["BTCUSDT"] = max(0.0, balances["BTCUSDT"] - qty)
                u_total += qty * btc_price
                state["dca_last_sell_date"] = today_id_str
                runtime_notify_fn(
                    runtime,
                    report,
                    f"{translate_fn('btc_dca_trim')} BTC\n"
                    f"{translate_fn('ratio_label')}: {sell_pct*100}%\n"
                    f"{translate_fn('quantity_label')}: {qty} BTC",
                )
                runtime_set_trade_state_fn(runtime, report, state, reason="btc_dca_sell")
        except Exception as exc:
            runtime_notify_fn(
                runtime,
                report,
                f"{translate_fn('btc_dca_trim_failed')} BTC\n"
                f"{translate_fn('error_label')}: {exc}",
            )

    return u_total
