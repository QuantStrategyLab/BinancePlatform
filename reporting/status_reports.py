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

    gate_text = translate_fn("gate_on") if btc_snapshot.get("regime_on", False) else translate_fn("gate_off")
    hint = build_btc_manual_hint(btc_snapshot, translate_fn=translate_fn)
    text = (
        f"{translate_fn('heartbeat_title')}\n"
        f"{translate_fn('strategy_label', name=strategy_display_name)}\n"
        f"{separator}\n"
        f"💰 {translate_fn('total_equity')}: ${total_equity:,.0f}\n"
        f"📈 {translate_fn('trend_equity')}: ${trend_holdings_equity:,.0f} ({trend_daily_pnl:+.1%})\n"
        f"₿ {translate_fn('btc_price')}: ${btc_price:,.0f}\n"
        f"{separator}\n"
        f"🚦 {translate_fn('btc_gate')}: {gate_text}\n"
        f"📊 Ahr999: {btc_snapshot['ahr999']:.2f} | Z-Score: {btc_snapshot['zscore']:.1f} ({translate_fn('zscore_threshold')} {btc_snapshot['sell_trigger']:.1f})\n"
        f"🎯 {translate_fn('btc_target')}: {btc_target_ratio:.1%}\n"
        f"{separator}\n"
        f"💡 {hint}"
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
    gate_text = translate_fn("gate_on") if btc_snapshot.get("regime_on", False) else translate_fn("gate_off")
    append_log_fn(log_buffer, translate_fn("portfolio_snapshot_title"))
    append_log_fn(
        log_buffer,
        f"💰 {translate_fn('total_equity')}: ${allocation['total_equity']:,.0f} ({daily_pnl:+.1%})",
    )
    append_log_fn(
        log_buffer,
        f"🪙 {translate_fn('btc_target')}: {allocation['btc_target_ratio']:.0%} | ${allocation['dca_val']:,.0f}",
    )
    append_log_fn(
        log_buffer,
        f"🔥 {translate_fn('trend_equity')}: {allocation['trend_target_ratio']:.0%} | ${allocation['trend_val']:,.0f} ({trend_daily_pnl:+.1%})",
    )
    append_log_fn(
        log_buffer,
        f"🚦 {translate_fn('btc_gate')}: {gate_text} | Ahr={btc_snapshot['ahr999']:.2f} Z={btc_snapshot['zscore']:.1f}",
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
    pool_text = ", ".join(active_trend_pool) if active_trend_pool else translate_fn("rotation_no_execution_pool")
    append_log_fn(log_buffer, f"🎯 {pool_text}")


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
    holding = [s for s in runtime_trend_universe if get_symbol_trade_state_fn(state, s)["is_holding"]]
    if holding:
        append_log_fn(log_buffer, f"📌 {', '.join(holding)}")
    else:
        append_log_fn(log_buffer, translate_fn("status_flat"))
