import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from binance.client import Client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRYPTO_STRATEGIES_SRC = PROJECT_ROOT.parent / "CryptoStrategies" / "src"
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
for path in (PROJECT_ROOT, CRYPTO_STRATEGIES_SRC, QPK_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from crypto_strategies.strategies.crypto_live_pool_rotation.core import (
    allocate_trend_buy_budget,
    build_rotation_pool_ranking,
    compute_allocation_budgets,
    get_dynamic_btc_base_order,
)
from crypto_strategies.strategies.crypto_live_pool_rotation.core import (
    select_rotation_weights as core_select_rotation_weights,
)

INITIAL_CAPITAL = 10000.0
ATR_MULTIPLIER = 2.5
CIRCUIT_BREAKER_PCT = -0.05

BACKTEST_END = pd.Timestamp("2026-03-12 23:00:00", tz="UTC")
DATA_START = pd.Timestamp("2018-01-01 00:00:00", tz="UTC")
WINDOWS = [
    ("2020-2026", pd.Timestamp("2020-01-01 00:00:00", tz="UTC"), BACKTEST_END),
    ("2023-2026", pd.Timestamp("2023-01-01 00:00:00", tz="UTC"), BACKTEST_END),
]

TRUMP_EVENT_DAY_UTC = pd.Timestamp("2025-10-10 00:00:00", tz="UTC")
TRUMP_EVENT_START = pd.Timestamp("2025-10-10 12:00:00", tz="UTC")
TRUMP_EVENT_END = pd.Timestamp("2025-10-10 16:00:00", tz="UTC")

FIXED_POOL = ["ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT"]
RESEARCH_UNIVERSE = [
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "TRXUSDT",
    "ATOMUSDT",
    "LTCUSDT",
    "BCHUSDT",
]
ALL_SYMBOLS = ["BTCUSDT"] + sorted(set(FIXED_POOL + RESEARCH_UNIVERSE))

STRATEGIES = [
    {
        "name": "fixed_pool_relative_btc",
        "label": "固定5币池",
        "pool_mode": "fixed",
        "pool_size": len(FIXED_POOL),
        "top_n": 2,
        "weight_mode": "inverse_vol",
        "btc_gate": True,
    },
    {
        "name": "auto_pool_stable_quality",
        "label": "全自动池-稳健质量排序",
        "pool_mode": "monthly_refresh",
        "pool_size": 5,
        "top_n": 2,
        "weight_mode": "inverse_vol",
        "btc_gate": True,
        "min_history_days": 365,
        "min_avg_quote_vol_180": 8_000_000,
        "membership_bonus": 0.10,
        "pool_score_weights": {
            "trend_rank": 0.24,
            "persistence_rank": 0.20,
            "liq_rank": 0.18,
            "stability_rank": 0.14,
            "rel_core_rank": 0.14,
            "risk_adj_rank": 0.10,
        },
    },
    {
        "name": "auto_pool_stable_quality_rel_boost",
        "label": "全自动池-稳健质量排序(强化相对强弱)",
        "pool_mode": "monthly_refresh",
        "pool_size": 5,
        "top_n": 2,
        "weight_mode": "inverse_vol",
        "btc_gate": True,
        "min_history_days": 365,
        "min_avg_quote_vol_180": 8_000_000,
        "membership_bonus": 0.08,
        "pool_score_weights": {
            "rel_core_rank": 0.24,
            "risk_adj_rank": 0.18,
            "trend_rank": 0.22,
            "persistence_rank": 0.16,
            "liq_rank": 0.12,
            "stability_rank": 0.08,
        },
    },
    {
        "name": "auto_pool_stable_quality_top3",
        "label": "全自动池-稳健质量排序(Top3持仓)",
        "pool_mode": "monthly_refresh",
        "pool_size": 5,
        "top_n": 3,
        "weight_mode": "inverse_vol",
        "btc_gate": True,
        "min_history_days": 365,
        "min_avg_quote_vol_180": 8_000_000,
        "membership_bonus": 0.10,
        "pool_score_weights": {
            "trend_rank": 0.24,
            "persistence_rank": 0.20,
            "liq_rank": 0.18,
            "stability_rank": 0.14,
            "rel_core_rank": 0.14,
            "risk_adj_rank": 0.10,
        },
    },
    {
        "name": "auto_pool_stable_quality_equal_weight",
        "label": "全自动池-稳健质量排序(等权持仓)",
        "pool_mode": "monthly_refresh",
        "pool_size": 5,
        "top_n": 2,
        "weight_mode": "equal",
        "btc_gate": True,
        "min_history_days": 365,
        "min_avg_quote_vol_180": 8_000_000,
        "membership_bonus": 0.10,
        "pool_score_weights": {
            "trend_rank": 0.24,
            "persistence_rank": 0.20,
            "liq_rank": 0.18,
            "stability_rank": 0.14,
            "rel_core_rank": 0.14,
            "risk_adj_rank": 0.10,
        },
    },
]


def build_client() -> Client:
    return Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"), {"timeout": 30})


def load_klines(client: Client, symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    klines = client.get_historical_klines(
        symbol,
        interval,
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )
    if not klines:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "vol", "available_at"])

    df = pd.DataFrame(klines).iloc[:, :7]
    df.columns = ["time", "open", "high", "low", "close", "vol", "close_time"]
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    # Binance's close timestamp is inclusive; the full bar is available after it.
    df["available_at"] = pd.to_datetime(df.pop("close_time"), unit="ms", utc=True) + pd.Timedelta(milliseconds=1)
    df[["open", "high", "low", "close", "vol"]] = df[["open", "high", "low", "close", "vol"]].astype(float)
    return df


def prepare_trend_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("time").copy()
    df["date"] = df["time"].dt.floor("D")
    df["indicator_event_time"] = df["time"]
    df["indicator_available_at"] = df.get("available_at", df["time"] + pd.Timedelta(days=1))
    df["quote_vol"] = df["close"] * df["vol"]

    df["sma20"] = df["close"].rolling(20).mean()
    df["sma60"] = df["close"].rolling(60).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    df["roc20"] = df["close"].pct_change(20)
    df["roc60"] = df["close"].pct_change(60)
    df["roc120"] = df["close"].pct_change(120)
    df["vol20"] = df["close"].pct_change().rolling(20).std()

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()

    df["avg_quote_vol_30"] = df["quote_vol"].rolling(30).mean()
    df["avg_quote_vol_90"] = df["quote_vol"].rolling(90).mean()
    df["avg_quote_vol_180"] = df["quote_vol"].rolling(180).mean()

    # Use a longer window to confirm the broad trend and reduce frequent pool switching.
    df["trend_persist_90"] = (df["close"] > df["sma200"]).rolling(90).mean()
    df["age_days"] = np.arange(1, len(df) + 1)

    return df[
        [
            "date",
            "indicator_available_at",
            "indicator_event_time",
            "sma20",
            "sma60",
            "sma200",
            "roc20",
            "roc60",
            "roc120",
            "vol20",
            "atr14",
            "avg_quote_vol_30",
            "avg_quote_vol_90",
            "avg_quote_vol_180",
            "trend_persist_90",
            "age_days",
        ]
    ]


def prepare_btc_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("time").copy()
    df["date"] = df["time"].dt.floor("D")
    df["indicator_event_time"] = df["time"]
    df["indicator_available_at"] = df.get("available_at", df["time"] + pd.Timedelta(days=1))
    df["ma200"] = df["close"].rolling(200).mean()
    df["std200"] = df["close"].rolling(200).std()
    df["zscore"] = (df["close"] - df["ma200"]) / df["std200"]
    df["geom200"] = np.exp(np.log(df["close"]).rolling(200).mean())
    df["sell_trigger"] = df["zscore"].rolling(365).quantile(0.95).clip(lower=2.5)
    df["ma200_slope"] = df["ma200"].pct_change(20)
    df["btc_roc20"] = df["close"].pct_change(20)
    df["btc_roc60"] = df["close"].pct_change(60)
    df["btc_roc120"] = df["close"].pct_change(120)
    return df[["date", "indicator_event_time", "indicator_available_at", "ma200", "zscore", "geom200", "sell_trigger", "ma200_slope", "btc_roc20", "btc_roc60", "btc_roc120"]]


def align_symbol_data(hourly_df: pd.DataFrame, daily_indicators: pd.DataFrame, timeline: pd.DatetimeIndex) -> pd.DataFrame:
    aligned = pd.DataFrame(index=timeline)
    aligned.index.name = "time"
    aligned["date"] = aligned.index.floor("D")

    hourly = hourly_df.set_index("time").reindex(timeline)
    aligned["open"] = hourly.get("open", pd.Series(np.nan, index=timeline))
    observed_close = hourly.get("close", pd.Series(np.nan, index=timeline))
    aligned["quote_observed"] = observed_close.notna()
    # Carry-forward is a mark only. Neither open nor quote_observed is filled.
    aligned["close"] = observed_close.ffill()
    aligned["available_at"] = hourly.get("available_at", pd.Series(timeline + pd.Timedelta(hours=1), index=timeline))
    if not daily_indicators.empty:
        daily = daily_indicators.copy()
        if "indicator_available_at" not in daily:
            daily["indicator_available_at"] = daily["date"] + pd.Timedelta(days=1)
        # Conservative boundary: only daily information already available at
        # this hourly bar's OPEN enters its later close-derived signal.
        aligned = pd.merge_asof(
            aligned.reset_index().sort_values("time"),
            daily.drop(columns="date").dropna(subset=["indicator_available_at"]).sort_values("indicator_available_at"),
            left_on="time", right_on="indicator_available_at", direction="backward",
        ).set_index("time")
    return aligned.reset_index()


def load_market_data():
    client = build_client()
    timeline = pd.date_range(DATA_START, BACKTEST_END, freq="1h", tz="UTC")
    market_data = {}

    for symbol in ALL_SYMBOLS:
        print(f"[DATA] 加载 {symbol} 小时线/日线...")
        hourly = load_klines(client, symbol, Client.KLINE_INTERVAL_1HOUR, DATA_START, BACKTEST_END)
        daily = load_klines(client, symbol, Client.KLINE_INTERVAL_1DAY, DATA_START, BACKTEST_END)
        daily_ind = prepare_btc_daily_indicators(daily) if symbol == "BTCUSDT" else prepare_trend_daily_indicators(daily)
        market_data[symbol] = align_symbol_data(hourly, daily_ind, timeline)
        print(f"[DATA] {symbol} 完成")

    return market_data


def position_value(symbols, positions, rows) -> float:
    total = 0.0
    for symbol in symbols:
        price = rows[symbol]["close"]
        if not pd.isna(price):
            total += positions[symbol] * price
    return total


TREND_ROW_FIELDS = [
    "close",
    "sma20",
    "sma60",
    "sma200",
    "roc20",
    "roc60",
    "roc120",
    "vol20",
    "atr14",
    "avg_quote_vol_30",
    "avg_quote_vol_90",
    "avg_quote_vol_180",
    "trend_persist_90",
    "age_days",
]


def snapshot_numeric_row(row, fields):
    snapshot = {}
    for field in fields:
        value = row.get(field)
        snapshot[field] = None if pd.isna(value) else float(value)
    return snapshot


def build_btc_snapshot_from_row(row, *, regime_on):
    snapshot = snapshot_numeric_row(
        row,
        ["btc_roc20", "btc_roc60", "btc_roc120", "ma200", "ma200_slope"],
    )
    snapshot["regime_on"] = bool(regime_on)
    return snapshot


def build_trend_indicator_map(rows, symbols):
    return {
        symbol: snapshot_numeric_row(rows[symbol], TREND_ROW_FIELDS)
        for symbol in symbols
    }


def build_pool_score_dataframe(strategy, rows, previous_pool):
    btc_snapshot = build_btc_snapshot_from_row(rows["BTCUSDT"], regime_on=True)
    ranking = build_rotation_pool_ranking(
        build_trend_indicator_map(rows, RESEARCH_UNIVERSE),
        btc_snapshot,
        previous_pool,
        min_history_days=strategy["min_history_days"],
        min_avg_quote_vol_180=strategy["min_avg_quote_vol_180"],
        membership_bonus=strategy["membership_bonus"],
        score_weights=strategy.get("pool_score_weights", {}),
    )
    if not ranking:
        return pd.DataFrame()
    return pd.DataFrame(ranking)


def refresh_monthly_pool(strategy, rows, ts, pool_state):
    month_key = ts.strftime("%Y-%m")
    if pool_state.get("last_month") == month_key:
        return pool_state.get("current_pool", FIXED_POOL)

    previous_pool = set(pool_state.get("current_pool", []))
    score_df = build_pool_score_dataframe(strategy, rows, previous_pool)
    if score_df.empty:
        return pool_state.get("current_pool", FIXED_POOL)

    selected = score_df["symbol"].head(strategy["pool_size"]).tolist()
    pool_state["last_month"] = month_key
    pool_state["current_pool"] = selected
    pool_state.setdefault("history", []).append(
        {
            "ts": ts,
            "month": month_key,
            "selected": list(selected),
            "ranking": score_df[
                [
                    "symbol",
                    "score",
                    "relative_strength_fast",
                    "relative_strength_core",
                    "trend_quality",
                    "breakout_strength",
                    "acceleration",
                    "persistence",
                    "risk_adjusted_momentum",
                    "liquidity",
                    "stability",
                ]
            ]
            .round(6)
            .to_dict("records"),
        }
    )
    return selected


def get_candidate_pool(strategy, rows, ts, pool_state):
    if strategy["pool_mode"] == "fixed":
        return FIXED_POOL
    return refresh_monthly_pool(strategy, rows, ts, pool_state)


def select_rotation_weights(strategy, rows, btc_gate_on, ts, pool_state):
    if strategy["btc_gate"] and not btc_gate_on:
        return {}

    current_pool = get_candidate_pool(strategy, rows, ts, pool_state)
    btc_snapshot = build_btc_snapshot_from_row(rows["BTCUSDT"], regime_on=btc_gate_on)
    prices = {symbol: rows[symbol]["close"] for symbol in current_pool}
    indicators_map = build_trend_indicator_map(rows, current_pool)
    return core_select_rotation_weights(
        indicators_map,
        prices,
        btc_snapshot,
        current_pool,
        strategy["top_n"],
        weight_mode=strategy["weight_mode"],
    )


def simulate_window(
    strategy, window_name: str, start: pd.Timestamp, end: pd.Timestamp, market_data,
    capture_details=False, *, fee_rate=None, slippage_rate=None,
):
    """Closed-hour signals fill at the next actual hourly open, never on a mark.

    start/end bound available observations, not unfinished bar open timestamps.
    Rates are per-side fractions supplied by the caller, not account fee claims.
    Omitting either rate is explicitly labelled unspecified zero-cost research.
    """
    costs_configured = fee_rate is not None and slippage_rate is not None
    fee_rate = 0.0 if fee_rate is None else float(fee_rate)
    slippage_rate = 0.0 if slippage_rate is None else float(slippage_rate)
    if any(not np.isfinite(rate) or not 0 <= rate < 1 for rate in (fee_rate, slippage_rate)):
        raise ValueError("fee/slippage rates must be finite per-side fractions in [0, 1)")
    if end < start:
        raise ValueError("end must not precede start")
    base_index = market_data["BTCUSDT"]["time"]
    for symbol in ALL_SYMBOLS:
        if not market_data[symbol]["time"].equals(base_index):
            raise ValueError(f"unaligned hourly timeline for {symbol}")
    base_available = market_data["BTCUSDT"].get("available_at", base_index + pd.Timedelta(hours=1))
    mask = (base_index >= start) & (base_available <= end)
    idxs = np.flatnonzero(mask.to_numpy())

    positions = {symbol: 0.0 for symbol in ALL_SYMBOLS}
    trend_state = {symbol: {"is_holding": False, "highest_price": 0.0} for symbol in ALL_SYMBOLS if symbol != "BTCUSDT"}
    cash_usdt = INITIAL_CAPITAL
    trade_count = 0
    dca_last_buy_date = ""
    dca_last_sell_date = ""
    daily_equity_base = None
    daily_trend_equity_base = None
    last_reset_date = None
    is_circuit_broken = False
    pending_circuit_exits = set()
    equity_curve = [(start, INITIAL_CAPITAL)]
    trade_ledger = []
    circuit_breaker_dates = []
    circuit_breaker_events = []
    pool_trace = []
    pool_state = {}

    trend_symbols = [symbol for symbol in ALL_SYMBOLS if symbol != "BTCUSDT"]
    last_available_close = {symbol: np.nan for symbol in ALL_SYMBOLS}

    for idx in idxs:
        ts = base_index.iloc[idx]
        today_utc = ts.strftime("%Y-%m-%d")
        today_id_str = ts.strftime("%Y%m%d")
        current_rows = {symbol: market_data[symbol].iloc[idx] for symbol in ALL_SYMBOLS}
        rows = {symbol: market_data[symbol].iloc[idx - 1].copy() for symbol in ALL_SYMBOLS} if idx > 0 else {}
        signal_time = base_index.iloc[idx - 1] if idx > 0 else None
        for symbol, row in rows.items():
            available = row.get("available_at", row["time"] + pd.Timedelta(hours=1))
            observed = row.get("quote_observed", pd.notna(row["close"]))
            if not observed or pd.isna(available) or available > ts:
                row["close"] = np.nan
            elif pd.notna(row["close"]):
                last_available_close[symbol] = row["close"]
        mark_rows = {}
        for symbol, current in current_rows.items():
            mark_rows[symbol] = current.copy()
            # At execution time current close is still in the future. In a gap,
            # the preceding observed close may be used only for valuation.
            mark_rows[symbol]["close"] = current.get("open") if pd.notna(current.get("open")) else last_available_close[symbol]

        def executable_price(symbol, execution_rows=current_rows):
            price = execution_rows[symbol].get("open")
            return float(price) if pd.notna(price) and np.isfinite(price) and price > 0 else None

        def record_trade(
            symbol, side, quantity, reference_price, reason, *,
            signal_rows=rows, observed_signal_time=signal_time, fill_time=ts,
        ):
            execution_price = reference_price * (1 + slippage_rate if side == "buy" else 1 - slippage_rate)
            notional = quantity * execution_price
            fee = notional * fee_rate
            cash_delta = -notional - fee if side == "buy" else notional - fee
            trade_ledger.append({
                "symbol": symbol, "side": side, "reason": reason,
                "signal_time": fill_time if reason == "circuit_breaker" else observed_signal_time,
                "signal_available_at": fill_time if reason == "circuit_breaker" else signal_rows[symbol].get(
                    "available_at", observed_signal_time + pd.Timedelta(hours=1)
                ),
                "fill_time": fill_time, "reference_price": reference_price,
                "execution_price": execution_price, "quantity": quantity,
                "notional": notional, "fee": fee,
                "slippage_cost": quantity * abs(execution_price - reference_price),
                "cash_delta": cash_delta,
            })
            return cash_delta

        trend_val = position_value(trend_symbols, positions, mark_rows)
        btc_mark = mark_rows["BTCUSDT"]["close"]
        dca_val = 0.0 if pd.isna(btc_mark) else positions["BTCUSDT"] * btc_mark
        total_equity = cash_usdt + trend_val + dca_val
        allocation = compute_allocation_budgets(total_equity, cash_usdt, trend_val, dca_val)
        trend_usdt_pool = allocation["trend_usdt_pool"]
        trend_layer_equity = allocation["trend_layer_equity"]

        if last_reset_date != today_utc:
            daily_equity_base = total_equity
            # Match live circuit breaker basis: real trend holdings only.
            daily_trend_equity_base = trend_val
            last_reset_date = today_utc
            is_circuit_broken = False

        daily_pnl = 0.0 if not daily_equity_base else (total_equity - daily_equity_base) / daily_equity_base
        trend_daily_pnl = (
            0.0 if not daily_trend_equity_base else (trend_val - daily_trend_equity_base) / daily_trend_equity_base
        )
        if trend_daily_pnl <= CIRCUIT_BREAKER_PCT and not is_circuit_broken:
            pending_circuit_exits.update(symbol for symbol in trend_symbols if positions[symbol] > 0)
            is_circuit_broken = True
            circuit_breaker_dates.append(today_utc)
            circuit_breaker_events.append(
                {
                    "ts": ts,
                    "date": today_utc,
                    "daily_pnl": float(trend_daily_pnl),
                    "total_daily_pnl": float(daily_pnl),
                    "equity": float(total_equity),
                    "trend_layer_equity": float(trend_layer_equity),
                }
            )

        # A missing executable quote defers a risk exit; it does not cancel it.
        # Keep these exits across daily resets until a real open permits a fill.
        for symbol in trend_symbols:
            if symbol not in pending_circuit_exits:
                continue
            if positions[symbol] <= 0:
                pending_circuit_exits.remove(symbol)
                continue
            price = executable_price(symbol)
            if price is not None and positions[symbol] * price > 10 and rows:
                cash_usdt += record_trade(symbol, "sell", positions[symbol], price, "circuit_breaker")
                positions[symbol] = 0.0
                trend_state[symbol] = {"is_holding": False, "highest_price": 0.0}
                pending_circuit_exits.remove(symbol)
                trade_count += 1

        if not rows:
            equity_curve.append((base_available.iloc[idx], total_equity))
            continue
        btc_row = rows["BTCUSDT"]
        btc_price = btc_row["close"]
        btc_gate_on = (
            not any(pd.isna(btc_row.get(k)) for k in ["close", "ma200", "ma200_slope"])
            and btc_row["close"] > btc_row["ma200"]
            and btc_row["ma200_slope"] > 0
        )

        active_weights = select_rotation_weights(strategy, rows, btc_gate_on, signal_time, pool_state)
        current_pool = get_candidate_pool(strategy, rows, signal_time, pool_state)
        if capture_details and (not pool_trace or pool_trace[-1][0].strftime("%Y-%m") != ts.strftime("%Y-%m")):
            pool_trace.append((ts, list(current_pool)))

        if not is_circuit_broken:
            for symbol in trend_symbols:
                if symbol in pending_circuit_exits:
                    continue
                row = rows[symbol]
                price = row["close"]
                if pd.isna(price):
                    # Missing signal information is not an instruction to exit.
                    continue
                sma60 = row.get("sma60")
                atr14 = row.get("atr14")

                if trend_state[symbol]["is_holding"] and not pd.isna(price):
                    trend_state[symbol]["highest_price"] = max(trend_state[symbol]["highest_price"], price)

                stop_triggered = False
                if trend_state[symbol]["is_holding"] and not any(pd.isna(v) for v in [price, sma60, atr14]):
                    stop_p = trend_state[symbol]["highest_price"] - (ATR_MULTIPLIER * atr14)
                    stop_triggered = price < sma60 or price < stop_p

                should_liquidate = (
                    trend_state[symbol]["is_holding"]
                    and positions[symbol] > 0
                    and (symbol not in active_weights or stop_triggered)
                )
                fill_price = executable_price(symbol)
                if should_liquidate and fill_price is not None:
                    cash_usdt += record_trade(symbol, "sell", positions[symbol], fill_price, "trend_exit")
                    positions[symbol] = 0.0
                    trend_state[symbol] = {"is_holding": False, "highest_price": 0.0}
                    trade_count += 1

            trend_val = position_value(trend_symbols, positions, mark_rows)
            dca_val = 0.0 if pd.isna(btc_mark) else positions["BTCUSDT"] * btc_mark
            total_equity = cash_usdt + trend_val + dca_val
            post_sell_allocation = compute_allocation_budgets(total_equity, cash_usdt, trend_val, dca_val)
            trend_usdt_pool = post_sell_allocation["trend_usdt_pool"]
            eligible_buy_symbols = []
            for symbol in active_weights:
                if symbol in pending_circuit_exits:
                    continue
                row = rows[symbol]
                price = row["close"]
                needed = ["sma20", "sma60", "sma200", "atr14"]
                if pd.isna(price) or price <= 0 or executable_price(symbol) is None or any(pd.isna(row.get(k)) for k in needed):
                    continue
                if trend_state[symbol]["is_holding"]:
                    continue
                eligible_buy_symbols.append(symbol)

            planned_trend_buys = allocate_trend_buy_budget(active_weights, eligible_buy_symbols, trend_usdt_pool)
            for symbol in eligible_buy_symbols:
                row = rows[symbol]
                price = executable_price(symbol)
                buy_u = planned_trend_buys.get(symbol, 0.0)
                if buy_u > 15:
                    qty = (buy_u * 0.985) / (price * (1 + slippage_rate) * (1 + fee_rate))
                    cost = qty * price * (1 + slippage_rate) * (1 + fee_rate)
                    if cost <= cash_usdt:
                        positions[symbol] += qty
                        cash_usdt += record_trade(symbol, "buy", qty, price, "trend_buy")
                        trend_state[symbol] = {"is_holding": True, "highest_price": price}
                        trade_count += 1

        trend_val = position_value(trend_symbols, positions, mark_rows)
        dca_val = 0.0 if pd.isna(btc_mark) else positions["BTCUSDT"] * btc_mark
        total_equity = cash_usdt + trend_val + dca_val
        post_trade_allocation = compute_allocation_budgets(total_equity, cash_usdt, trend_val, dca_val)
        trend_usdt_pool = post_trade_allocation["trend_usdt_pool"]
        dca_usdt_pool = post_trade_allocation["dca_usdt_pool"]

        btc_fill_price = executable_price("BTCUSDT")
        if not pd.isna(btc_price) and btc_price > 0 and btc_fill_price is not None:
            geom200 = btc_row.get("geom200")
            zscore = btc_row.get("zscore")
            sell_trigger = btc_row.get("sell_trigger")

            if not pd.isna(geom200):
                ahr = btc_price / geom200
                multiplier = 0
                if ahr < 0.45:
                    multiplier = 5
                elif ahr < 0.8:
                    multiplier = 2
                elif ahr < 1.2:
                    multiplier = 1

                if multiplier > 0 and dca_usdt_pool > 15 and dca_last_buy_date != today_id_str:
                    buy_budget = min(dca_usdt_pool, get_dynamic_btc_base_order(total_equity) * multiplier)
                    qty = (buy_budget * 0.985) / (btc_fill_price * (1 + slippage_rate) * (1 + fee_rate))
                    cost = qty * btc_fill_price * (1 + slippage_rate) * (1 + fee_rate)
                    if cost <= cash_usdt:
                        positions["BTCUSDT"] += qty
                        cash_usdt += record_trade("BTCUSDT", "buy", qty, btc_fill_price, "dca_buy")
                        dca_last_buy_date = today_id_str
                        trade_count += 1

            if (
                not pd.isna(zscore)
                and not pd.isna(sell_trigger)
                and zscore > sell_trigger
                and dca_val > 20
                and dca_last_sell_date != today_id_str
            ):
                sell_pct = 0.1
                if zscore > 4.0:
                    sell_pct = 0.3
                if zscore > 5.0:
                    sell_pct = 0.5

                qty = positions["BTCUSDT"] * sell_pct
                if qty > 0:
                    cash_usdt += record_trade("BTCUSDT", "sell", qty, btc_fill_price, "dca_reduce")
                    positions["BTCUSDT"] -= qty
                    dca_last_sell_date = today_id_str
                    trade_count += 1

        close_marks = {}
        for symbol, current in current_rows.items():
            available = current.get("available_at", current["time"] + pd.Timedelta(hours=1))
            if pd.notna(available) and available <= base_available.iloc[idx] and pd.notna(current["close"]):
                last_available_close[symbol] = current["close"]
            close_marks[symbol] = {"close": last_available_close[symbol]}
        end_equity = cash_usdt + position_value(ALL_SYMBOLS, positions, close_marks)
        equity_curve.append((base_available.iloc[idx], end_equity))

    equity_series = pd.Series([v for _, v in equity_curve], index=[ts for ts, _ in equity_curve], dtype=float)
    final_equity = float(equity_series.iloc[-1])
    total_return = final_equity / INITIAL_CAPITAL - 1.0
    years = (equity_series.index[-1] - equity_series.index[0]).total_seconds() / (365.25 * 24 * 3600)
    with np.errstate(over="ignore", invalid="ignore"):
        cagr = np.nan if years <= 0 else np.expm1(np.log(final_equity / INITIAL_CAPITAL) / years)
    drawdown = equity_series / equity_series.cummax() - 1.0
    max_drawdown = float(drawdown.min())

    return {
        "strategy_name": strategy["name"],
        "strategy_label": strategy["label"],
        "window": window_name,
        "final_equity": final_equity,
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": float(max_drawdown),
        "trades": trade_count,
        "execution_model": "closed_hour_signal_next_open_v1",
        "cost_assumption_status": "EXPLICIT_RESEARCH_RATES" if costs_configured else "UNSPECIFIED_ZERO_COST",
        "fee_rate": fee_rate,
        "slippage_rate": slippage_rate,
        "research_status": "DEVELOPMENT_ONLY",
        "total_fees": sum(trade["fee"] for trade in trade_ledger),
        "total_costs": sum(trade["fee"] + trade["slippage_cost"] for trade in trade_ledger),
        "turnover_notional": sum(trade["notional"] for trade in trade_ledger),
        "trade_ledger": trade_ledger if capture_details else [],
        "equity_series": equity_series if capture_details else None,
        "circuit_breaker_dates": circuit_breaker_dates if capture_details else [],
        "circuit_breaker_events": circuit_breaker_events if capture_details else [],
        "pool_trace": pool_trace if capture_details else [],
        "pool_history": pool_state.get("history", []) if capture_details else [],
    }


def summarize_trump_event_window(result):
    equity_series = result["equity_series"]
    if equity_series is None:
        return None

    scoped = equity_series.loc[(equity_series.index >= TRUMP_EVENT_START) & (equity_series.index <= TRUMP_EVENT_END)]
    if scoped.empty:
        return None

    pre_event_series = equity_series.loc[equity_series.index < TRUMP_EVENT_START]
    base_equity = float(pre_event_series.iloc[-1]) if not pre_event_series.empty else float(scoped.iloc[0])
    event_day_series = equity_series.loc[
        (equity_series.index >= TRUMP_EVENT_DAY_UTC) & (equity_series.index <= TRUMP_EVENT_END)
    ]
    day_base_equity = float(event_day_series.iloc[0]) if not event_day_series.empty else base_equity
    min_equity = float(scoped.min())
    end_equity = float(scoped.iloc[-1])
    min_time = scoped.idxmin()
    event_drawdown = (min_equity / base_equity) - 1.0 if base_equity > 0 else np.nan
    breaker_daily_pnl = (min_equity / day_base_equity) - 1.0 if day_base_equity > 0 else np.nan
    event_return = (end_equity / base_equity) - 1.0 if base_equity > 0 else np.nan
    event_breakers = [
        event
        for event in result["circuit_breaker_events"]
        if TRUMP_EVENT_START <= event["ts"] <= TRUMP_EVENT_END
    ]

    return {
        "base_equity": base_equity,
        "day_base_equity": day_base_equity,
        "min_equity": min_equity,
        "end_equity": end_equity,
        "min_time": min_time,
        "event_drawdown": float(event_drawdown),
        "breaker_daily_pnl": float(breaker_daily_pnl),
        "event_return": float(event_return),
        "circuit_breakers": event_breakers,
    }


def compute_forward_return(close_series: pd.Series, ts: pd.Timestamp, horizon_days: int):
    target_ts = ts + pd.Timedelta(days=horizon_days)
    if ts not in close_series.index or target_ts not in close_series.index:
        return np.nan

    base_price = close_series.loc[ts]
    future_price = close_series.loc[target_ts]
    if pd.isna(base_price) or pd.isna(future_price) or base_price <= 0:
        return np.nan
    return float(future_price / base_price - 1.0)


def analyze_pool_discovery(result, market_data, future_windows=(30, 60, 90), top_k=3):
    if not result["pool_history"]:
        return None

    close_map = {symbol: market_data[symbol].set_index("time")["close"] for symbol in RESEARCH_UNIVERSE}
    focus_symbols = ["ETHUSDT", "SOLUSDT", "XRPUSDT"]

    stats = {
        horizon: {
            "months": 0,
            "hit_count": 0,
            "top_slots": 0,
            "avg_selected_return_sum": 0.0,
            "avg_universe_return_sum": 0.0,
            "avg_top_return_sum": 0.0,
            "corr_sum": 0.0,
            "corr_count": 0,
        }
        for horizon in future_windows
    }
    focus_stats = {
        symbol: {horizon: {"opportunities": 0, "caught": 0} for horizon in future_windows}
        for symbol in focus_symbols
    }

    for snapshot in result["pool_history"]:
        ts = snapshot["ts"]
        ranking_df = pd.DataFrame(snapshot["ranking"])
        if ranking_df.empty:
            continue

        eligible_symbols = ranking_df["symbol"].tolist()
        selected_set = set(snapshot["selected"])

        for horizon in future_windows:
            returns = []
            for symbol in eligible_symbols:
                forward_ret = compute_forward_return(close_map[symbol], ts, horizon)
                if not np.isnan(forward_ret):
                    returns.append((symbol, forward_ret))
            if len(returns) < top_k:
                continue

            stats[horizon]["months"] += 1
            available_returns = dict(returns)
            sorted_future = sorted(returns, key=lambda item: item[1], reverse=True)
            future_top_symbols = [symbol for symbol, _ in sorted_future[:top_k]]
            stats[horizon]["hit_count"] += len(selected_set & set(future_top_symbols))
            stats[horizon]["top_slots"] += top_k

            selected_returns = [available_returns[symbol] for symbol in snapshot["selected"] if symbol in available_returns]
            if selected_returns:
                stats[horizon]["avg_selected_return_sum"] += float(np.mean(selected_returns))
            stats[horizon]["avg_universe_return_sum"] += float(np.mean(list(available_returns.values())))
            stats[horizon]["avg_top_return_sum"] += float(np.mean([ret for _, ret in sorted_future[:top_k]]))

            merged = ranking_df.copy()
            merged["future_return"] = merged["symbol"].map(available_returns)
            merged = merged.dropna(subset=["future_return"])
            if len(merged) >= 3:
                corr = merged["score"].rank(method="average").corr(
                    merged["future_return"].rank(method="average")
                )
                if pd.notna(corr):
                    stats[horizon]["corr_sum"] += float(corr)
                    stats[horizon]["corr_count"] += 1

            future_top_set = set(future_top_symbols)
            for symbol in focus_symbols:
                if symbol in future_top_set:
                    focus_stats[symbol][horizon]["opportunities"] += 1
                    if symbol in selected_set:
                        focus_stats[symbol][horizon]["caught"] += 1

    return {
        "stats": stats,
        "focus_stats": focus_stats,
    }


def print_pool_discovery_report(result, market_data):
    analysis = analyze_pool_discovery(result, market_data)
    if analysis is None:
        return

    print(f"\n===== {result['strategy_label']} 自动发现强币验证 | {result['window']} =====")
    print("说明: 每月初用当时可见数据自动建池，检验未来 30/60/90 天是否能覆盖后续最强币。")

    for horizon, stat in analysis["stats"].items():
        if stat["months"] == 0 or stat["top_slots"] == 0:
            continue

        hit_rate = stat["hit_count"] / stat["top_slots"]
        avg_selected = stat["avg_selected_return_sum"] / stat["months"]
        avg_universe = stat["avg_universe_return_sum"] / stat["months"]
        avg_top = stat["avg_top_return_sum"] / stat["months"]
        avg_corr = np.nan if stat["corr_count"] == 0 else stat["corr_sum"] / stat["corr_count"]
        print(
            f"未来{horizon}天 | "
            f"Top3命中率: {hit_rate*100:.2f}% | "
            f"入池币平均收益: {avg_selected*100:.2f}% | "
            f"候选池平均收益: {avg_universe*100:.2f}% | "
            f"未来Top3平均收益: {avg_top*100:.2f}% | "
            f"评分相关性(Spearman): {avg_corr:.3f}"
        )

    print("重点币提前识别率:")
    for symbol, horizon_map in analysis["focus_stats"].items():
        text_parts = []
        for horizon, symbol_stat in horizon_map.items():
            opp = symbol_stat["opportunities"]
            caught = symbol_stat["caught"]
            hit_text = "无样本" if opp == 0 else f"{caught}/{opp} ({caught/opp*100:.1f}%)"
            text_parts.append(f"{horizon}天={hit_text}")
        print(f"{symbol}: " + " | ".join(text_parts))


def print_pool_trace(result):
    if not result["pool_trace"]:
        return
    print(f"\n[{result['strategy_label']}] 2023-2026 月度池样本:")
    preview = result["pool_trace"][-6:]
    for ts, pool in preview:
        print(f"{ts.strftime('%Y-%m')}: {', '.join(pool)}")


def print_auto_strategy_leaderboard(all_results, detailed_results, market_data):
    auto_results = [result for result in all_results if result["strategy_name"].startswith("auto_pool_")]
    if not auto_results:
        return

    discovery_map = {}
    for result in detailed_results:
        if result["strategy_name"].startswith("auto_pool_"):
            analysis = analyze_pool_discovery(result, market_data)
            if analysis is not None:
                discovery_map[(result["strategy_name"], result["window"])] = analysis

    print("\n===== 稳健质量排序优化对比 =====")
    for window_name, _, _ in WINDOWS:
        print(f"\n--- {window_name} ---")
        rows = []
        for result in auto_results:
            if result["window"] != window_name:
                continue
            analysis = discovery_map.get((result["strategy_name"], window_name))
            hit_60 = np.nan
            hit_90 = np.nan
            if analysis is not None:
                stat60 = analysis["stats"].get(60, {})
                stat90 = analysis["stats"].get(90, {})
                if stat60.get("top_slots", 0):
                    hit_60 = stat60["hit_count"] / stat60["top_slots"]
                if stat90.get("top_slots", 0):
                    hit_90 = stat90["hit_count"] / stat90["top_slots"]
            rows.append((result["strategy_label"], result["cagr"], result["max_drawdown"], hit_60, hit_90))

        rows.sort(key=lambda item: ((-np.inf if np.isnan(item[3]) else item[3]), item[1]), reverse=True)
        for label, cagr, max_dd, hit_60, hit_90 in rows:
            hit60_text = "NA" if np.isnan(hit_60) else f"{hit_60*100:.2f}%"
            hit90_text = "NA" if np.isnan(hit_90) else f"{hit_90*100:.2f}%"
            print(
                f"{label} | "
                f"60天Top3命中率: {hit60_text} | "
                f"90天Top3命中率: {hit90_text} | "
                f"年化收益: {cagr*100:.2f}% | "
                f"最大回撤: {max_dd*100:.2f}%"
            )


def run_strategy_comparison():
    market_data = load_market_data()
    all_results = []
    detailed_results = []

    for strategy in STRATEGIES:
        for window_name, start, end in WINDOWS:
            print(f"[RUN] {strategy['label']} | {window_name}")
            capture_details = window_name == "2023-2026" or strategy["pool_mode"] == "monthly_refresh"
            result = simulate_window(strategy, window_name, start, end, market_data, capture_details=capture_details)
            all_results.append(result)
            if capture_details:
                detailed_results.append(result)

    print("===== 全自动选池研究回测（已完成小时信号、下一实际小时开盘成交）=====")
    print("研究状态: DEVELOPMENT_ONLY；未提供真实费率与滑点，当前结果为 UNSPECIFIED_ZERO_COST，不可用于晋级或调整预算。")
    print("说明: 所有策略都保留 BTC 动态仓位 + AHR 定投/高估减仓 + BTC 趋势闸门 + ATR/SMA60 风控。")
    print("当前重点比较 固定5币池 基准 与 稳健质量排序家族的不同参数变体。")

    for window_name, _, _ in WINDOWS:
        print(f"\n--- {window_name} ---")
        window_results = [r for r in all_results if r["window"] == window_name]
        window_results.sort(key=lambda r: (r["cagr"], r["max_drawdown"]), reverse=True)
        for result in window_results:
            print(
                f"{result['strategy_label']} | "
                f"年化收益: {result['cagr']*100:.2f}% | "
                f"最大回撤: {result['max_drawdown']*100:.2f}% | "
                f"总收益: {result['total_return']*100:.2f}% | "
                f"期末净值: ${result['final_equity']:.2f} | "
                f"交易次数: {result['trades']}"
            )

    print_auto_strategy_leaderboard(all_results, detailed_results, market_data)

    print("\n===== 2025-10-10 美东早上黑天鹅窗口 =====")
    print("说明: 事件窗口按 EDT 08:00-12:00 折算为 UTC 12:00-16:00，重点检查日内熔断阈值是否被打穿。")
    for result in detailed_results:
        if result["window"] != "2023-2026":
            continue
        summary = summarize_trump_event_window(result)
        if summary is None:
            continue
        if not summary["circuit_breakers"]:
            breaker_text = "未触发"
        else:
            breaker_text = ",".join(
                f"{event['ts'].strftime('%Y-%m-%d %H:%M')} UTC({event['daily_pnl']*100:.2f}%)"
                for event in summary["circuit_breakers"]
            )
        print(
            f"{result['strategy_label']} | "
            f"事件回撤: {summary['event_drawdown']*100:.2f}% | "
            f"相对UTC日初净值跌幅: {summary['breaker_daily_pnl']*100:.2f}% | "
            f"窗口收益: {summary['event_return']*100:.2f}% | "
            f"最低净值: ${summary['min_equity']:.2f} @ {summary['min_time'].strftime('%Y-%m-%d %H:%M')} UTC | "
            f"日熔断: {breaker_text}"
        )

    for result in detailed_results:
        if result["strategy_name"].startswith("auto_pool_"):
            print_pool_discovery_report(result, market_data)
            if result["window"] == "2023-2026":
                print_pool_trace(result)


if __name__ == "__main__":
    run_strategy_comparison()
