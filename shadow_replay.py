#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main as strategy


DEFAULT_UPSTREAM_REPO = Path(__file__).resolve().parents[1] / "CryptoLeaderRotation"
DEFAULT_RELEASE_INDEX = DEFAULT_UPSTREAM_REPO / "data" / "output" / "shadow_releases" / "release_index.csv"
DEFAULT_RAW_DIR = DEFAULT_UPSTREAM_REPO / "data" / "raw"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an end-to-end shadow replay using local upstream artifacts.")
    parser.add_argument("--release-index", default=str(DEFAULT_RELEASE_INDEX), help="Path to upstream release_index.csv.")
    parser.add_argument(
        "--artifacts-root",
        default=None,
        help="Optional root directory for release artifact paths when the index is copied or filtered elsewhere.",
    )
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Path to local daily raw OHLCV CSVs.")
    parser.add_argument("--output-dir", default="reports", help="Directory for replay summary/detail outputs.")
    parser.add_argument("--name", default="baseline", help="Short run name used in output filenames.")
    parser.add_argument("--start-date", default=None, help="Optional replay start date.")
    parser.add_argument("--end-date", default=None, help="Optional replay end date.")
    parser.add_argument("--max-age-days", type=int, default=45, help="Freshness window for upstream artifacts.")
    parser.add_argument("--soft-tilt-field", default=None, help="Optional upstream selection_meta field for soft tilt.")
    parser.add_argument("--soft-tilt-strength", type=float, default=0.15, help="Bounded soft-tilt strength.")
    return parser.parse_args()


def ensure_directory(path: Path | str) -> Path:
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def load_release_index(path: Path | str) -> pd.DataFrame:
    index_path = Path(path)
    if not index_path.exists():
        raise FileNotFoundError(f"Release index not found: {index_path}")
    frame = pd.read_csv(index_path)
    if frame.empty:
        raise ValueError(f"Release index is empty: {index_path}")
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"]).dt.normalize()
    frame["activation_date"] = pd.to_datetime(frame["activation_date"]).dt.normalize()
    return frame.sort_values(["activation_date", "as_of_date"]).reset_index(drop=True)


def load_daily_history(raw_dir: Path | str, symbol: str) -> pd.DataFrame:
    path = Path(raw_dir) / f"{symbol}.csv"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    if "volume" not in frame.columns and "vol" in frame.columns:
        frame["volume"] = frame["vol"]
    for column in ["open", "high", "low", "close", "volume", "quote_volume"]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "quote_volume" not in frame.columns and {"close", "volume"}.issubset(frame.columns):
        frame["quote_volume"] = frame["close"] * frame["volume"]
    return frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def prepare_trend_indicator_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    history = frame.copy()
    history["quote_vol"] = history["quote_volume"]
    history["sma20"] = history["close"].rolling(20).mean()
    history["sma60"] = history["close"].rolling(60).mean()
    history["sma200"] = history["close"].rolling(200).mean()
    history["roc20"] = history["close"].pct_change(20)
    history["roc60"] = history["close"].pct_change(60)
    history["roc120"] = history["close"].pct_change(120)
    history["vol20"] = history["close"].pct_change().rolling(20).std()
    tr = pd.concat(
        [
            history["high"] - history["low"],
            (history["high"] - history["close"].shift(1)).abs(),
            (history["low"] - history["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    history["atr14"] = tr.rolling(14).mean()
    history["avg_quote_vol_30"] = history["quote_vol"].rolling(30).mean()
    history["avg_quote_vol_90"] = history["quote_vol"].rolling(90).mean()
    history["avg_quote_vol_180"] = history["quote_vol"].rolling(180).mean()
    history["trend_persist_90"] = (history["close"] > history["sma200"]).rolling(90).mean()
    history["age_days"] = np.arange(1, len(history) + 1)
    return history.set_index("date")


def prepare_btc_snapshot_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    history = frame.copy()
    history["ma200"] = history["close"].rolling(200).mean()
    history["std200"] = history["close"].rolling(200).std()
    history["zscore"] = (history["close"] - history["ma200"]) / history["std200"]
    history["geom200"] = np.exp(np.log(history["close"]).rolling(200).mean())
    history["sell_trigger"] = history["zscore"].rolling(365).quantile(0.95).clip(lower=2.5)
    history["ma200_slope"] = history["ma200"].pct_change(20)
    history["btc_roc20"] = history["close"].pct_change(20)
    history["btc_roc60"] = history["close"].pct_change(60)
    history["btc_roc120"] = history["close"].pct_change(120)
    history["ahr999"] = history["close"] / history["geom200"]
    history["regime_on"] = (history["close"] > history["ma200"]) & (history["ma200_slope"] > 0)
    return history.set_index("date")


def compute_open_to_open_returns(histories: dict[str, pd.DataFrame], dates: pd.DatetimeIndex) -> pd.DataFrame:
    matrices = {}
    for symbol, history in histories.items():
        if history.empty or "open" not in history.columns:
            continue
        matrices[symbol] = history.set_index("date")["open"].reindex(dates)
    open_matrix = pd.DataFrame(matrices, index=dates).sort_index()
    return open_matrix.shift(-1).div(open_matrix).sub(1.0)


def resolve_active_release(signal_date: pd.Timestamp, releases: list[dict[str, Any]], max_age_days: int) -> tuple[dict[str, Any], str]:
    eligible = [release for release in releases if release["activation_date"] <= signal_date]
    if not eligible:
        return {
            "symbols": list(strategy.STATIC_TREND_UNIVERSE.keys()),
            "symbol_map": {symbol: meta.copy() for symbol, meta in strategy.STATIC_TREND_UNIVERSE.items()},
            "selection_meta": {},
            "version": "static-fallback",
            "as_of_date": "",
            "mode": "static",
            "source_project": "BinanceQuant",
        }, "static"

    latest = eligible[-1]
    age_days = int((signal_date - latest["as_of_date"]).days)
    if age_days <= max_age_days:
        return latest["payload"], "fresh_upstream"
    return latest["payload"], "last_known_good"


def compute_performance_metrics(returns: pd.Series, turnover: pd.Series | None = None) -> dict[str, float]:
    returns = returns.dropna()
    if returns.empty:
        return {
            "CAGR": float("nan"),
            "Annualized Volatility": float("nan"),
            "Sharpe": float("nan"),
            "Max Drawdown": float("nan"),
            "Turnover": float("nan"),
        }

    equity = (1.0 + returns).cumprod()
    total_days = len(returns)
    cagr = float(equity.iloc[-1] ** (365 / total_days) - 1.0)
    ann_vol = float(returns.std(ddof=0) * math.sqrt(365))
    ann_return = float(returns.mean() * 365)
    sharpe = ann_return / ann_vol if ann_vol > 0 else float("nan")
    drawdown = equity / equity.cummax() - 1.0
    turnover_value = float(turnover.mean() * 365) if turnover is not None else float("nan")
    return {
        "CAGR": cagr,
        "Annualized Volatility": ann_vol,
        "Sharpe": sharpe,
        "Max Drawdown": float(drawdown.min()),
        "Turnover": turnover_value,
    }


def build_release_payloads(index_table: pd.DataFrame, base_dir: Path) -> list[dict[str, Any]]:
    releases = []
    for row in index_table.to_dict("records"):
        live_pool_path = base_dir / str(row["live_pool_path"])
        with live_pool_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        releases.append(
            {
                "version": str(row["version"]),
                "as_of_date": pd.Timestamp(row["as_of_date"]).normalize(),
                "activation_date": pd.Timestamp(row["activation_date"]).normalize(),
                "payload": payload,
            }
        )
    return releases


def run_shadow_replay(
    *,
    release_index_path: Path,
    artifacts_root: Path | None,
    raw_dir: Path,
    max_age_days: int,
    soft_tilt_field: str | None = None,
    soft_tilt_strength: float = 0.15,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    index_table = load_release_index(release_index_path)
    base_dir = artifacts_root or release_index_path.parent
    releases = build_release_payloads(index_table, base_dir)

    release_symbols = set()
    for release in releases:
        release_symbols.update(release["payload"].get("symbols", []))
    symbols = sorted(release_symbols | set(strategy.STATIC_TREND_UNIVERSE.keys()) | {"BTCUSDT"})

    histories = {symbol: load_daily_history(raw_dir, symbol) for symbol in symbols}
    histories = {symbol: history for symbol, history in histories.items() if not history.empty}
    if "BTCUSDT" not in histories:
        raise FileNotFoundError("BTCUSDT history is required for shadow replay.")

    trend_histories = {
        symbol: prepare_trend_indicator_history(history)
        for symbol, history in histories.items()
        if symbol != "BTCUSDT"
    }
    btc_history = prepare_btc_snapshot_history(histories["BTCUSDT"])

    dates = pd.DatetimeIndex(btc_history.index).sort_values().unique()
    if start_date is not None:
        dates = dates[dates >= pd.Timestamp(start_date).normalize()]
    if end_date is not None:
        dates = dates[dates <= pd.Timestamp(end_date).normalize()]
    if len(dates) < 3:
        raise ValueError("Not enough overlapping dates for shadow replay.")

    returns_matrix = compute_open_to_open_returns(histories, dates)
    signal_dates = list(dates[:-1])
    all_symbols = sorted(set(returns_matrix.columns))
    current_weights = pd.Series(0.0, index=all_symbols, dtype=float)
    weight_matrix = pd.DataFrame(0.0, index=dates[:-1], columns=all_symbols, dtype=float)
    turnover_series = pd.Series(0.0, index=dates[:-1], dtype=float)
    detail_rows = []

    for signal_date in signal_dates[:-1]:
        payload, source_kind = resolve_active_release(signal_date, releases, max_age_days=max_age_days)
        candidate_pool = [
            symbol
            for symbol in payload.get("symbols", list(strategy.STATIC_TREND_UNIVERSE.keys()))
            if symbol in trend_histories
        ]

        btc_row = btc_history.loc[signal_date] if signal_date in btc_history.index else None
        if btc_row is None or btc_row.isna().any():
            desired_weights = {}
        else:
            btc_snapshot = btc_row.to_dict()
            indicators_map = {}
            prices = {}
            for symbol in candidate_pool:
                history = trend_histories.get(symbol)
                if history is None or signal_date not in history.index:
                    continue
                row = history.loc[signal_date]
                if row.isna().any():
                    continue
                indicators_map[symbol] = row.to_dict()
                prices[symbol] = float(row["close"])

            desired_weights = strategy.select_rotation_weights(
                indicators_map=indicators_map,
                prices=prices,
                btc_snapshot=btc_snapshot,
                candidate_pool=candidate_pool,
                top_n=strategy.ROTATION_TOP_N,
            )
            if soft_tilt_field:
                desired_weights = strategy.apply_selection_meta_soft_tilt(
                    desired_weights,
                    payload.get("selection_meta", {}),
                    field=soft_tilt_field,
                    strength=soft_tilt_strength,
                )

        effective_date = signal_date + pd.Timedelta(days=1)
        next_date = dates[dates.get_loc(signal_date) + 1]
        effective_date = pd.Timestamp(next_date).normalize()

        next_weights = pd.Series(0.0, index=all_symbols, dtype=float)
        for symbol, meta in desired_weights.items():
            if symbol in next_weights.index:
                next_weights.loc[symbol] = float(meta["weight"])
        turnover = float((next_weights - current_weights).abs().sum() / 2.0)
        current_weights = next_weights
        if effective_date in weight_matrix.index:
            weight_matrix.loc[effective_date] = current_weights
            turnover_series.loc[effective_date] = turnover

        detail_rows.append(
            {
                "signal_date": signal_date,
                "effective_date": effective_date,
                "source_kind": source_kind,
                "release_as_of_date": payload.get("as_of_date", ""),
                "release_version": payload.get("version", ""),
                "pool_symbols": "|".join(candidate_pool),
                "selected_symbols": "|".join(symbol for symbol, weight in current_weights.items() if weight > 0),
                "selected_weights": "|".join(
                    f"{symbol}:{weight:.4f}" for symbol, weight in current_weights.items() if weight > 0
                ),
                "turnover": turnover,
                "fresh_upstream": float(source_kind == "fresh_upstream"),
                "degraded_fallback": float(source_kind != "fresh_upstream"),
            }
        )

    returns = (weight_matrix * returns_matrix.reindex(weight_matrix.index).fillna(0.0)).sum(axis=1)
    metrics = compute_performance_metrics(returns, turnover=turnover_series)
    detail_table = pd.DataFrame(detail_rows)
    source_mix = detail_table["source_kind"].value_counts(normalize=True).to_dict() if not detail_table.empty else {}
    summary_row = {
        "run_name": release_index_path.parent.name,
        "release_index_path": str(release_index_path),
        "artifacts_root": str(base_dir),
        "signal_dates": int(len(detail_table)),
        "soft_tilt_field": soft_tilt_field or "",
        "soft_tilt_strength": float(soft_tilt_strength if soft_tilt_field else 0.0),
        **metrics,
        "fresh_upstream_pct": float(source_mix.get("fresh_upstream", 0.0)),
        "last_known_good_pct": float(source_mix.get("last_known_good", 0.0)),
        "static_pct": float(source_mix.get("static", 0.0)),
        "average_active_positions": float((weight_matrix > 0).sum(axis=1).mean()),
    }
    return pd.DataFrame([summary_row]), detail_table


def main_cli() -> None:
    args = parse_args()
    output_dir = ensure_directory(args.output_dir)
    release_index_path = Path(args.release_index).resolve()
    summary, detail = run_shadow_replay(
        release_index_path=release_index_path,
        artifacts_root=Path(args.artifacts_root).resolve() if args.artifacts_root else None,
        raw_dir=Path(args.raw_dir).resolve(),
        max_age_days=max(0, int(args.max_age_days)),
        soft_tilt_field=args.soft_tilt_field,
        soft_tilt_strength=float(args.soft_tilt_strength),
        start_date=args.start_date,
        end_date=args.end_date,
    )
    summary["run_name"] = args.name
    summary_path = output_dir / f"{args.name}_shadow_replay_summary.csv"
    detail_path = output_dir / f"{args.name}_shadow_replay_detail.csv"
    summary.to_csv(summary_path, index=False)
    detail.to_csv(detail_path, index=False)
    print(summary.to_string(index=False))
    print(f"detail_path={detail_path}")
    print(f"summary_path={summary_path}")


if __name__ == "__main__":
    main_cli()
