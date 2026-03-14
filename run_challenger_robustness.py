#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

import shadow_replay


DEFAULT_UPSTREAM_ROOT = Path(__file__).resolve().parents[1] / "CryptoLeaderRotation"

PROFILE_RELEASE_INDEX = {
    "baseline_blended_rank": Path("data/output/challenger_shadow_releases/baseline_blended_rank/release_index.csv"),
    "challenger_rank_60": Path("data/output/challenger_shadow_releases/challenger_rank_60/release_index.csv"),
    "challenger_topk_60": Path("data/output/challenger_shadow_releases/challenger_topk_60/release_index.csv"),
}

SCENARIOS = (
    {"scenario": "current_default", "activation_lag_days": None, "cost_bps": 0.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "lag_0", "activation_lag_days": 0, "cost_bps": 0.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "lag_1", "activation_lag_days": 1, "cost_bps": 0.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "lag_2", "activation_lag_days": 2, "cost_bps": 0.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "lag_3", "activation_lag_days": 3, "cost_bps": 0.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "cost_10bps", "activation_lag_days": 1, "cost_bps": 10.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "cost_20bps", "activation_lag_days": 1, "cost_bps": 20.0, "drop_every_nth_release": 0, "drop_phase": 0},
    {"scenario": "missing_every_6th", "activation_lag_days": 1, "cost_bps": 0.0, "drop_every_nth_release": 6, "drop_phase": 5},
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run challenger robustness comparisons using local upstream shadow releases.")
    parser.add_argument("--upstream-root", default=str(DEFAULT_UPSTREAM_ROOT), help="Path to the CryptoLeaderRotation repo.")
    parser.add_argument("--output-dir", default="reports", help="Directory for robustness outputs.")
    parser.add_argument("--raw-dir", default=None, help="Optional override for local daily raw OHLCV CSVs.")
    parser.add_argument("--max-age-days", type=int, default=45, help="Freshness window used by replay.")
    parser.add_argument("--default-slice-scenario", default="lag_1", help="Scenario used for monthly/regime concentration slices.")
    return parser.parse_args()


def compound_return(series: pd.Series) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return float("nan")
    return float((1.0 + clean).prod() - 1.0)


def summarize_detail(detail_table: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    if detail_table.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for group_key, group in detail_table.groupby(group_columns, dropna=False):
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        row = dict(zip(group_columns, group_key))
        row.update(
            {
                "trading_days": int(len(group)),
                "net_return": compound_return(group["net_return"]),
                "gross_return": compound_return(group["gross_return"]),
                "avg_turnover": float(pd.to_numeric(group["turnover"], errors="coerce").mean()),
                "fresh_upstream_pct": float((group["source_kind"] == "fresh_upstream").mean()),
                "last_known_good_pct": float((group["source_kind"] == "last_known_good").mean()),
                "static_pct": float((group["source_kind"] == "static").mean()),
                "avg_release_age_days": float(pd.to_numeric(group["release_age_days"], errors="coerce").mean()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def compare_to_baseline(summary_table: pd.DataFrame) -> pd.DataFrame:
    baseline = summary_table.loc[summary_table["profile"] == "baseline_blended_rank"].copy()
    baseline = baseline.rename(
        columns={
            "CAGR": "baseline_cagr",
            "Sharpe": "baseline_sharpe",
            "Max Drawdown": "baseline_max_drawdown",
            "Turnover": "baseline_turnover",
        }
    )
    merged = summary_table.merge(
        baseline[
            [
                "scenario",
                "baseline_cagr",
                "baseline_sharpe",
                "baseline_max_drawdown",
                "baseline_turnover",
            ]
        ],
        on="scenario",
        how="left",
    )
    challenger_rows = merged.loc[merged["profile"] != "baseline_blended_rank"].copy()
    challenger_rows["delta_cagr_vs_baseline"] = challenger_rows["CAGR"] - challenger_rows["baseline_cagr"]
    challenger_rows["delta_sharpe_vs_baseline"] = challenger_rows["Sharpe"] - challenger_rows["baseline_sharpe"]
    challenger_rows["delta_max_drawdown_vs_baseline"] = challenger_rows["Max Drawdown"] - challenger_rows["baseline_max_drawdown"]
    challenger_rows["delta_turnover_vs_baseline"] = challenger_rows["Turnover"] - challenger_rows["baseline_turnover"]
    return challenger_rows


def build_monthly_comparison(detail_table: pd.DataFrame) -> pd.DataFrame:
    monthly = summarize_detail(detail_table, ["profile", "period_month"]).sort_values(["period_month", "profile"])
    monthly["year"] = monthly["period_month"].str.slice(0, 4).astype(int)
    return monthly


def build_excess_table(monthly_table: pd.DataFrame) -> pd.DataFrame:
    pivoted = monthly_table.pivot(index="period_month", columns="profile", values="net_return").reset_index()
    baseline = "baseline_blended_rank"
    for challenger in ("challenger_rank_60", "challenger_topk_60"):
        if challenger in pivoted.columns and baseline in pivoted.columns:
            pivoted[f"{challenger}_excess_vs_baseline"] = pivoted[challenger] - pivoted[baseline]
            pivoted[f"{challenger}_cum_excess_vs_baseline"] = pivoted[f"{challenger}_excess_vs_baseline"].cumsum()
    return pivoted


def build_excess_concentration(monthly_excess: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for challenger in ("challenger_rank_60", "challenger_topk_60"):
        column = f"{challenger}_excess_vs_baseline"
        if column not in monthly_excess.columns:
            continue
        series = pd.to_numeric(monthly_excess[column], errors="coerce").dropna()
        positive = series.loc[series > 0]
        total_positive = float(positive.sum()) if not positive.empty else 0.0
        sorted_positive = positive.sort_values(ascending=False)
        rows.append(
            {
                "profile": challenger,
                "months_compared": int(len(series)),
                "months_outperforming": int((series > 0).sum()),
                "median_monthly_excess": float(series.median()) if not series.empty else float("nan"),
                "mean_monthly_excess": float(series.mean()) if not series.empty else float("nan"),
                "top_3_positive_excess_share": float(sorted_positive.head(3).sum() / total_positive) if total_positive > 0 else float("nan"),
                "top_5_positive_excess_share": float(sorted_positive.head(5).sum() / total_positive) if total_positive > 0 else float("nan"),
                "best_monthly_excess": float(series.max()) if not series.empty else float("nan"),
                "worst_monthly_excess": float(series.min()) if not series.empty else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    upstream_root = Path(args.upstream_root).resolve()
    raw_dir = Path(args.raw_dir).resolve() if args.raw_dir else upstream_root / "data" / "raw"
    output_dir = shadow_replay.ensure_directory(args.output_dir)

    summaries = []
    details = []
    for scenario in SCENARIOS:
        for profile, relative_index in PROFILE_RELEASE_INDEX.items():
            release_index_path = upstream_root / relative_index
            summary, detail = shadow_replay.run_shadow_replay(
                release_index_path=release_index_path,
                artifacts_root=release_index_path.parent,
                raw_dir=raw_dir,
                max_age_days=max(0, int(args.max_age_days)),
                activation_lag_days=scenario["activation_lag_days"],
                cost_bps=float(scenario["cost_bps"]),
                drop_every_nth_release=int(scenario["drop_every_nth_release"]),
                drop_phase=int(scenario["drop_phase"]),
            )
            summary = summary.copy()
            summary["profile"] = profile
            summary["scenario"] = scenario["scenario"]
            if detail.empty:
                detail = pd.DataFrame(columns=["effective_date"])
            detail = detail.copy()
            detail["profile"] = profile
            detail["scenario"] = scenario["scenario"]
            summaries.append(summary)
            details.append(detail)

    summary_table = pd.concat(summaries, ignore_index=True)
    detail_table = pd.concat(details, ignore_index=True)

    summary_path = output_dir / "challenger_robustness_scenario_summary.csv"
    comparison_path = output_dir / "challenger_robustness_vs_baseline.csv"
    detail_path = output_dir / "challenger_robustness_detail.csv"
    summary_table.to_csv(summary_path, index=False)
    compare_to_baseline(summary_table).to_csv(comparison_path, index=False)
    detail_table.to_csv(detail_path, index=False)

    default_slice = detail_table.loc[detail_table["scenario"] == args.default_slice_scenario].copy()
    monthly_path = output_dir / "challenger_robustness_monthly_returns.csv"
    yearly_path = output_dir / "challenger_robustness_yearly_summary.csv"
    release_path = output_dir / "challenger_robustness_release_period.csv"
    regime_path = output_dir / "challenger_robustness_regime_summary.csv"
    excess_path = output_dir / "challenger_robustness_monthly_vs_baseline.csv"
    concentration_path = output_dir / "challenger_robustness_concentration_summary.csv"

    monthly_table = build_monthly_comparison(default_slice)
    monthly_table.to_csv(monthly_path, index=False)
    summarize_detail(default_slice.assign(year=default_slice["effective_date"].dt.year), ["profile", "year"]).to_csv(
        yearly_path,
        index=False,
    )
    summarize_detail(
        default_slice,
        ["profile", "release_version", "release_as_of_date", "release_regime"],
    ).to_csv(release_path, index=False)
    summarize_detail(
        default_slice.loc[default_slice["release_regime"].notna()].copy(),
        ["profile", "release_regime"],
    ).to_csv(regime_path, index=False)
    monthly_excess = build_excess_table(monthly_table)
    monthly_excess.to_csv(excess_path, index=False)
    build_excess_concentration(monthly_excess).to_csv(concentration_path, index=False)

    print(summary_table.to_string(index=False))
    print(f"summary_path={summary_path}")
    print(f"comparison_path={comparison_path}")
    print(f"detail_path={detail_path}")
    print(f"monthly_path={monthly_path}")
    print(f"yearly_path={yearly_path}")
    print(f"release_path={release_path}")
    print(f"regime_path={regime_path}")
    print(f"excess_path={excess_path}")
    print(f"concentration_path={concentration_path}")


if __name__ == "__main__":
    main()
