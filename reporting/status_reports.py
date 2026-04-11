"""Status and portfolio reporting helpers for BinancePlatform."""

from __future__ import annotations


def get_periodic_report_bucket(now_utc, interval_hours):
    safe_interval = max(1, min(24, int(interval_hours)))
    if now_utc.hour % safe_interval != 0:
        return ""
    return now_utc.strftime("%Y%m%d") + f"{now_utc.hour:02d}"


def build_btc_manual_hint(btc_snapshot, *, translate_fn):
    ahr = btc_snapshot["ahr999"]
    zscore = btc_snapshot["zscore"]
    sell_trigger = btc_snapshot["sell_trigger"]

    if ahr < 0.45:
        return translate_fn("manual_hint_deep_value")
    if ahr < 0.8:
        return translate_fn("manual_hint_low_value")
    if zscore >= sell_trigger:
        return translate_fn("manual_hint_profit_taking")
    if zscore >= sell_trigger * 0.9:
        return translate_fn("manual_hint_near_profit_taking")
    return translate_fn("manual_hint_neutral")


def maybe_send_periodic_btc_status_report(
    state,
    tg_token,
    tg_chat_id,
    now_utc,
    interval_hours,
    total_equity,
    trend_holdings_equity,
    trend_daily_pnl,
    btc_price,
    btc_snapshot,
    btc_target_ratio,
    strategy_display_name,
    *,
    translate_fn,
    separator,
    notifier_fn=None,
    send_tg_msg_fn=None,
):
    report_bucket = get_periodic_report_bucket(now_utc, interval_hours)
    if not report_bucket or state.get("last_btc_status_report_bucket") == report_bucket:
        return

    gate_text = translate_fn("gate_on") if btc_snapshot["regime_on"] else translate_fn("gate_off")
    text = (
        f"{translate_fn('heartbeat_title')}\n"
        f"{translate_fn('strategy_label', name=strategy_display_name)}\n"
        f"{translate_fn('time_utc')}: {now_utc.strftime('%Y-%m-%d %H:%M')}\n"
        f"{separator}\n"
        f"{translate_fn('total_equity')}: ${total_equity:.2f}\n"
        f"{translate_fn('trend_equity')}: ${trend_holdings_equity:.2f} ({trend_daily_pnl:.2%})\n"
        f"{translate_fn('btc_price')}: ${btc_price:.2f}\n"
        f"{separator}\n"
        f"{translate_fn('ahr999')}: {btc_snapshot['ahr999']:.3f}\n"
        f"{translate_fn('zscore')}: {btc_snapshot['zscore']:.2f} / {translate_fn('zscore_threshold')} {btc_snapshot['sell_trigger']:.2f}\n"
        f"{translate_fn('btc_target')}: {btc_target_ratio:.1%}\n"
        f"{translate_fn('btc_gate')}: {gate_text}\n"
        f"{separator}\n"
        f"{translate_fn('manual_hint')}: {build_btc_manual_hint(btc_snapshot, translate_fn=translate_fn)}"
    )
    if notifier_fn is None:
        send_tg_msg_fn(tg_token, tg_chat_id, text)
    else:
        notifier_fn(text)
    state["last_btc_status_report_bucket"] = report_bucket


def append_portfolio_report(
    log_buffer,
    allocation,
    fuel_val,
    daily_pnl,
    trend_daily_pnl,
    btc_snapshot,
    *,
    append_log_fn,
    translate_fn,
    separator,
):
    append_log_fn(log_buffer, translate_fn("portfolio_snapshot_title"))
    append_log_fn(
        log_buffer,
        translate_fn("portfolio_total_equity_line", total_equity=allocation["total_equity"], daily_pnl=daily_pnl),
    )
    append_log_fn(
        log_buffer,
        translate_fn(
            "portfolio_btc_core_line",
            target_ratio=allocation["btc_target_ratio"],
            current_value=allocation["dca_val"],
            available_value=allocation["dca_usdt_pool"],
        ),
    )
    append_log_fn(
        log_buffer,
        translate_fn(
            "portfolio_trend_sleeve_line",
            target_ratio=allocation["trend_target_ratio"],
            current_value=allocation["trend_val"],
            available_value=allocation["trend_usdt_pool"],
            trend_daily_pnl=trend_daily_pnl,
        ),
    )
    append_log_fn(log_buffer, translate_fn("portfolio_bnb_fuel_reserve_line", fuel_val=fuel_val))
    append_log_fn(
        log_buffer,
        translate_fn(
            "portfolio_btc_gate_line",
            gate_text=translate_fn("gate_on") if btc_snapshot["regime_on"] else translate_fn("gate_off"),
            ahr=btc_snapshot["ahr999"],
            zscore=btc_snapshot["zscore"],
        ),
    )
    append_log_fn(log_buffer, separator)


def append_rotation_summary(
    log_buffer,
    official_trend_pool,
    active_trend_pool,
    selected_candidates,
    *,
    append_log_fn,
    translate_fn,
):
    official_pool_text = ", ".join(official_trend_pool) if official_trend_pool else translate_fn("rotation_no_upstream_pool")
    execution_pool_text = ", ".join(active_trend_pool) if active_trend_pool else translate_fn("rotation_no_execution_pool")
    execution_pool_count = len(active_trend_pool)
    selected_text = (
        ", ".join(
            f"{symbol}({meta['weight']:.0%},RS:{meta['relative_score']:.2f})"
            for symbol, meta in selected_candidates.items()
        )
        if selected_candidates
        else translate_fn("rotation_no_candidates")
    )
    append_log_fn(log_buffer, translate_fn("rotation_upstream_official_monthly_pool", pool_text=official_pool_text))
    append_log_fn(log_buffer, translate_fn("rotation_current_execution_pool", pool_text=execution_pool_text))
    append_log_fn(log_buffer, translate_fn("rotation_current_execution_pool_size", pool_size=execution_pool_count))
    append_log_fn(log_buffer, translate_fn("rotation_current_execution_targets", target_text=selected_text))


def append_trend_symbol_status(
    log_buffer,
    runtime_trend_universe,
    prices,
    trend_indicators,
    state,
    btc_snapshot,
    *,
    append_log_fn,
    translate_fn,
    get_symbol_trade_state_fn,
):
    for symbol in runtime_trend_universe:
        curr_price = prices[symbol]
        indicators = trend_indicators.get(symbol)
        position_state = get_symbol_trade_state_fn(state, symbol)
        score_text = ""
        if indicators and indicators["vol20"] > 0:
            rel_score = (
                0.5 * (indicators["roc20"] - btc_snapshot["btc_roc20"])
                + 0.3 * (indicators["roc60"] - btc_snapshot["btc_roc60"])
                + 0.2 * (indicators["roc120"] - btc_snapshot["btc_roc120"])
            ) / indicators["vol20"]
            abs_momentum = 0.5 * indicators["roc20"] + 0.3 * indicators["roc60"] + 0.2 * indicators["roc120"]
            score_text = translate_fn("trend_symbol_score_text", rel_score=rel_score, abs_momentum=abs_momentum)
        append_log_fn(
            log_buffer,
            translate_fn(
                "trend_symbol_status_line",
                symbol=symbol,
                status=translate_fn("status_holding") if position_state["is_holding"] else translate_fn("status_flat"),
                price=curr_price,
                score_text=score_text,
            ),
        )
