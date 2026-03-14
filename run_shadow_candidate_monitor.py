#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

import run_challenger_robustness as robustness
import shadow_replay


DEFAULT_UPSTREAM_ROOT = Path(__file__).resolve().parents[1] / "CryptoLeaderRotation"
DEFAULT_TRACKS = {
    "official_baseline": Path("data/output/shadow_candidate_tracks/official_baseline/release_index.csv"),
    "challenger_topk_60": Path("data/output/shadow_candidate_tracks/challenger_topk_60/release_index.csv"),
}

SENSITIVITY_SCENARIOS = (
    {"scenario": "lag_1", "activation_lag_days": 1, "cost_bps": 0.0},
    {"scenario": "lag_3", "activation_lag_days": 3, "cost_bps": 0.0},
    {"scenario": "cost_10bps", "activation_lag_days": 1, "cost_bps": 10.0},
    {"scenario": "cost_20bps", "activation_lag_days": 1, "cost_bps": 20.0},
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run baseline vs challenger_topk_60 dual-track shadow monitoring.")
    parser.add_argument("--upstream-root", default=str(DEFAULT_UPSTREAM_ROOT), help="Path to the CryptoLeaderRotation repo.")
    parser.add_argument("--output-dir", default="reports", help="Directory for shadow monitor outputs.")
    parser.add_argument("--raw-dir", default=None, help="Optional override for local daily raw OHLCV CSVs.")
    parser.add_argument("--max-age-days", type=int, default=45, help="Freshness window used for both tracks.")
    parser.add_argument("--activation-lag-days", type=int, default=None, help="Optional shared activation lag override.")
    parser.add_argument("--cost-bps", type=float, default=0.0, help="Optional shared turnover-scaled cost assumption.")
    return parser.parse_args()


def validate_track_identity(summary_table: pd.DataFrame) -> None:
    required = {
        "official_baseline": ("baseline_blended_rank", "official_baseline", "official_reference"),
        "challenger_topk_60": ("challenger_topk_60", "shadow_candidate", "shadow_candidate"),
    }
    rows = summary_table.set_index("track_id")
    for track_id, (profile, source_track, candidate_status) in required.items():
        if track_id not in rows.index:
            raise ValueError(f"Missing required track: {track_id}")
        row = rows.loc[track_id]
        if str(row.get("track_profile", "")) != profile:
            raise ValueError(f"Track {track_id} has unexpected profile: {row.get('track_profile')}")
        if str(row.get("source_track", "")) != source_track:
            raise ValueError(f"Track {track_id} has unexpected source_track: {row.get('source_track')}")
        if str(row.get("candidate_status", "")) != candidate_status:
            raise ValueError(f"Track {track_id} has unexpected candidate_status: {row.get('candidate_status')}")


def build_side_by_side_summary(summary_table: pd.DataFrame) -> pd.DataFrame:
    baseline = summary_table.loc[summary_table["track_id"] == "official_baseline"].iloc[0]
    challenger = summary_table.loc[summary_table["track_id"] == "challenger_topk_60"].iloc[0]
    return pd.DataFrame(
        [
            {
                "baseline_profile": baseline["track_profile"],
                "challenger_profile": challenger["track_profile"],
                "baseline_source_track": baseline["source_track"],
                "challenger_source_track": challenger["source_track"],
                "baseline_candidate_status": baseline["candidate_status"],
                "challenger_candidate_status": challenger["candidate_status"],
                "baseline_cagr": baseline["CAGR"],
                "challenger_cagr": challenger["CAGR"],
                "delta_cagr": challenger["CAGR"] - baseline["CAGR"],
                "baseline_sharpe": baseline["Sharpe"],
                "challenger_sharpe": challenger["Sharpe"],
                "delta_sharpe": challenger["Sharpe"] - baseline["Sharpe"],
                "baseline_max_drawdown": baseline["Max Drawdown"],
                "challenger_max_drawdown": challenger["Max Drawdown"],
                "delta_max_drawdown": challenger["Max Drawdown"] - baseline["Max Drawdown"],
                "baseline_turnover": baseline["Turnover"],
                "challenger_turnover": challenger["Turnover"],
                "delta_turnover": challenger["Turnover"] - baseline["Turnover"],
            }
        ]
    )


def build_promotion_watchlist(
    monthly_excess: pd.DataFrame,
    concentration: pd.DataFrame,
    regime_summary: pd.DataFrame,
    release_summary: pd.DataFrame,
    sensitivity_summary: pd.DataFrame,
    side_by_side: pd.DataFrame,
) -> pd.DataFrame:
    excess_column = "challenger_topk_60_excess_vs_baseline"
    recent_12 = monthly_excess.tail(12).copy()
    recent_6 = monthly_excess.tail(6).copy()
    prior_12 = monthly_excess.iloc[-24:-12].copy() if len(monthly_excess) >= 24 else pd.DataFrame()

    concentration_row = concentration.loc[concentration["profile"] == "challenger_topk_60"].iloc[0]

    release_pivot = release_summary.pivot(index="release_as_of_date", columns="profile", values="net_return").sort_index()
    release_pivot["excess_vs_baseline"] = (
        release_pivot.get("challenger_topk_60", 0.0) - release_pivot.get("baseline_blended_rank", 0.0)
    )
    recent_release_window = release_pivot.tail(6)

    regime_pivot = regime_summary.pivot(index="release_regime", columns="profile", values="net_return")
    risk_off_excess = (
        float(regime_pivot.loc["risk_off", "challenger_topk_60"] - regime_pivot.loc["risk_off", "baseline_blended_rank"])
        if "risk_off" in regime_pivot.index
        else float("nan")
    )
    sensitivity_status = derive_sensitivity_status(sensitivity_summary)
    side_by_side_row = side_by_side.iloc[0]

    row = {
        "recent_12_month_outperformance_rate": float((recent_12[excess_column] > 0).mean()) if not recent_12.empty else float("nan"),
        "recent_6_month_outperformance_rate": float((recent_6[excess_column] > 0).mean()) if not recent_6.empty else float("nan"),
        "recent_12_month_mean_excess": float(recent_12[excess_column].mean()) if not recent_12.empty else float("nan"),
        "prior_12_month_mean_excess": float(prior_12[excess_column].mean()) if not prior_12.empty else float("nan"),
        "recent_6_release_total_excess": float(recent_release_window["excess_vs_baseline"].sum()) if not recent_release_window.empty else float("nan"),
        "top_5_positive_excess_share": float(concentration_row["top_5_positive_excess_share"]),
        "risk_off_excess_vs_baseline": risk_off_excess,
        "lag_sensitivity_status": sensitivity_status["lag_sensitivity_status"],
        "friction_sensitivity_status": sensitivity_status["friction_sensitivity_status"],
        "gate_recent_12_positive": bool(not recent_12.empty and recent_12[excess_column].sum() > 0),
        "gate_recent_6_releases_positive": bool(not recent_release_window.empty and recent_release_window["excess_vs_baseline"].sum() > 0),
        "gate_concentration_not_extreme": bool(float(concentration_row["top_5_positive_excess_share"]) <= 0.70),
        "gate_risk_off_not_worse": bool(pd.notna(risk_off_excess) and risk_off_excess >= 0.0),
        "gate_lag_sensitivity_ok": bool(sensitivity_status["gate_lag_sensitivity_ok"]),
        "gate_friction_sensitivity_ok": bool(sensitivity_status["gate_friction_sensitivity_ok"]),
        "challenger_cagr": float(side_by_side_row["challenger_cagr"]),
        "baseline_cagr": float(side_by_side_row["baseline_cagr"]),
        "challenger_sharpe": float(side_by_side_row["challenger_sharpe"]),
        "baseline_sharpe": float(side_by_side_row["baseline_sharpe"]),
    }
    row["recommendation"] = recommend_shadow_candidate(row)
    return pd.DataFrame([row])


def run_track_comparison(
    track_paths: dict[str, Path],
    *,
    raw_dir: Path,
    max_age_days: int,
    activation_lag_days: int | None = None,
    cost_bps: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summaries = []
    details = []
    for track_id, release_index_path in track_paths.items():
        summary, detail = shadow_replay.run_shadow_replay(
            release_index_path=release_index_path,
            artifacts_root=release_index_path.parent,
            raw_dir=raw_dir,
            max_age_days=max(0, int(max_age_days)),
            activation_lag_days=activation_lag_days,
            cost_bps=float(cost_bps),
        )
        summary = summary.copy()
        summary["track_id"] = track_id
        detail = detail.copy()
        detail["track_id"] = track_id
        summaries.append(summary)
        details.append(detail)
    return pd.concat(summaries, ignore_index=True), pd.concat(details, ignore_index=True)


def build_sensitivity_summary(
    track_paths: dict[str, Path],
    *,
    raw_dir: Path,
    max_age_days: int,
) -> pd.DataFrame:
    rows = []
    for scenario in SENSITIVITY_SCENARIOS:
        summary_table, _ = run_track_comparison(
            track_paths,
            raw_dir=raw_dir,
            max_age_days=max_age_days,
            activation_lag_days=int(scenario["activation_lag_days"]),
            cost_bps=float(scenario["cost_bps"]),
        )
        baseline = summary_table.loc[summary_table["track_id"] == "official_baseline"].iloc[0]
        challenger = summary_table.loc[summary_table["track_id"] == "challenger_topk_60"].iloc[0]
        rows.append(
            {
                "scenario": scenario["scenario"],
                "activation_lag_days": int(scenario["activation_lag_days"]),
                "cost_bps": float(scenario["cost_bps"]),
                "baseline_cagr": float(baseline["CAGR"]),
                "challenger_cagr": float(challenger["CAGR"]),
                "delta_cagr": float(challenger["CAGR"] - baseline["CAGR"]),
                "baseline_sharpe": float(baseline["Sharpe"]),
                "challenger_sharpe": float(challenger["Sharpe"]),
                "delta_sharpe": float(challenger["Sharpe"] - baseline["Sharpe"]),
                "baseline_max_drawdown": float(baseline["Max Drawdown"]),
                "challenger_max_drawdown": float(challenger["Max Drawdown"]),
                "delta_max_drawdown": float(challenger["Max Drawdown"] - baseline["Max Drawdown"]),
            }
        )
    return pd.DataFrame(rows)


def derive_sensitivity_status(sensitivity_summary: pd.DataFrame) -> dict[str, Any]:
    lag_rows = sensitivity_summary.loc[sensitivity_summary["scenario"].str.startswith("lag_")].copy()
    friction_rows = sensitivity_summary.loc[sensitivity_summary["scenario"].str.startswith("cost_")].copy()

    lag_ok = bool(not lag_rows.empty and (lag_rows["delta_sharpe"] > 0).all() and (lag_rows["delta_cagr"] > 0).all())
    friction_ok = bool(
        not friction_rows.empty
        and (friction_rows["delta_sharpe"] > 0).all()
        and (friction_rows["delta_cagr"] > 0).all()
    )
    return {
        "lag_sensitivity_status": "pass" if lag_ok else "warning",
        "friction_sensitivity_status": "pass" if friction_ok else "warning",
        "gate_lag_sensitivity_ok": lag_ok,
        "gate_friction_sensitivity_ok": friction_ok,
    }


def recommend_shadow_candidate(watchlist_row: dict[str, Any] | pd.Series) -> str:
    row = dict(watchlist_row)
    cumulative_advantage = (
        float(row.get("challenger_cagr", float("nan"))) > float(row.get("baseline_cagr", float("nan")))
        and float(row.get("challenger_sharpe", float("nan"))) > float(row.get("baseline_sharpe", float("nan")))
    )
    hard_fail = (
        not cumulative_advantage
        or not bool(row.get("gate_risk_off_not_worse", False))
        or not bool(row.get("gate_concentration_not_extreme", False))
        or not bool(row.get("gate_lag_sensitivity_ok", False))
        or not bool(row.get("gate_friction_sensitivity_ok", False))
    )
    if hard_fail:
        return "remain shadow-only"

    if (
        float(row.get("recent_12_month_outperformance_rate", 0.0)) >= 0.50
        and float(row.get("recent_6_month_outperformance_rate", 0.0)) >= 0.50
        and bool(row.get("gate_recent_12_positive", False))
        and bool(row.get("gate_recent_6_releases_positive", False))
    ):
        return "candidate for future controlled trial"
    return "continue observation"


def run_shadow_candidate_monitor(
    *,
    upstream_root: Path,
    output_dir: Path,
    raw_dir: Path,
    max_age_days: int,
    activation_lag_days: int | None = None,
    cost_bps: float = 0.0,
) -> dict[str, pd.DataFrame]:
    track_paths = {track_id: upstream_root / relative_path for track_id, relative_path in DEFAULT_TRACKS.items()}
    summary_table, detail_table = run_track_comparison(
        track_paths,
        raw_dir=raw_dir,
        max_age_days=max_age_days,
        activation_lag_days=activation_lag_days,
        cost_bps=cost_bps,
    )
    validate_track_identity(summary_table)

    side_by_side = build_side_by_side_summary(summary_table)
    monthly_table = robustness.build_monthly_comparison(detail_table)
    monthly_excess = robustness.build_excess_table(monthly_table)
    concentration = robustness.build_excess_concentration(monthly_excess)
    regime_summary = robustness.summarize_detail(
        detail_table.loc[detail_table["release_regime"].notna()].copy(),
        ["profile", "release_regime"],
    )
    release_summary = robustness.summarize_detail(
        detail_table,
        ["profile", "release_version", "release_as_of_date", "release_regime"],
    )
    sensitivity_summary = build_sensitivity_summary(
        track_paths,
        raw_dir=raw_dir,
        max_age_days=max_age_days,
    )
    watchlist = build_promotion_watchlist(
        monthly_excess,
        concentration,
        regime_summary,
        release_summary,
        sensitivity_summary,
        side_by_side,
    )

    summary_path = output_dir / "shadow_candidate_track_summary.csv"
    side_by_side_path = output_dir / "shadow_candidate_side_by_side_summary.csv"
    detail_path = output_dir / "shadow_candidate_detail.csv"
    monthly_path = output_dir / "shadow_candidate_monthly_returns.csv"
    monthly_vs_baseline_path = output_dir / "shadow_candidate_monthly_vs_baseline.csv"
    regime_path = output_dir / "shadow_candidate_regime_summary.csv"
    release_path = output_dir / "shadow_candidate_release_period.csv"
    concentration_path = output_dir / "shadow_candidate_concentration_summary.csv"
    watchlist_path = output_dir / "shadow_candidate_promotion_watchlist.csv"
    sensitivity_path = output_dir / "shadow_candidate_sensitivity_summary.csv"

    summary_table.to_csv(summary_path, index=False)
    side_by_side.to_csv(side_by_side_path, index=False)
    detail_table.to_csv(detail_path, index=False)
    monthly_table.to_csv(monthly_path, index=False)
    monthly_excess.to_csv(monthly_vs_baseline_path, index=False)
    regime_summary.to_csv(regime_path, index=False)
    release_summary.to_csv(release_path, index=False)
    concentration.to_csv(concentration_path, index=False)
    watchlist.to_csv(watchlist_path, index=False)
    sensitivity_summary.to_csv(sensitivity_path, index=False)

    return {
        "summary_table": summary_table,
        "side_by_side": side_by_side,
        "detail_table": detail_table,
        "monthly_table": monthly_table,
        "monthly_excess": monthly_excess,
        "regime_summary": regime_summary,
        "release_summary": release_summary,
        "concentration": concentration,
        "watchlist": watchlist,
        "sensitivity_summary": sensitivity_summary,
        "paths": pd.DataFrame(
            [
                {"name": "summary_path", "path": str(summary_path)},
                {"name": "side_by_side_path", "path": str(side_by_side_path)},
                {"name": "detail_path", "path": str(detail_path)},
                {"name": "monthly_path", "path": str(monthly_path)},
                {"name": "monthly_vs_baseline_path", "path": str(monthly_vs_baseline_path)},
                {"name": "regime_path", "path": str(regime_path)},
                {"name": "release_path", "path": str(release_path)},
                {"name": "concentration_path", "path": str(concentration_path)},
                {"name": "watchlist_path", "path": str(watchlist_path)},
                {"name": "sensitivity_path", "path": str(sensitivity_path)},
            ]
        ),
    }


def print_monitor_console_summary(results: dict[str, pd.DataFrame]) -> None:
    side_by_side_row = results["side_by_side"].iloc[0]
    watchlist_row = results["watchlist"].iloc[0]
    print(
        "baseline"
        f" cagr={side_by_side_row['baseline_cagr']:.4f}"
        f" sharpe={side_by_side_row['baseline_sharpe']:.4f}"
        f" mdd={side_by_side_row['baseline_max_drawdown']:.4f}"
    )
    print(
        "challenger_topk_60"
        f" cagr={side_by_side_row['challenger_cagr']:.4f}"
        f" sharpe={side_by_side_row['challenger_sharpe']:.4f}"
        f" mdd={side_by_side_row['challenger_max_drawdown']:.4f}"
    )
    print(
        "watchlist"
        f" recent12={watchlist_row['recent_12_month_outperformance_rate']:.4f}"
        f" recent6={watchlist_row['recent_6_month_outperformance_rate']:.4f}"
        f" top5_share={watchlist_row['top_5_positive_excess_share']:.4f}"
        f" lag_status={watchlist_row['lag_sensitivity_status']}"
        f" friction_status={watchlist_row['friction_sensitivity_status']}"
    )
    print(f"recommendation={watchlist_row['recommendation']}")


def main() -> None:
    args = parse_args()
    upstream_root = Path(args.upstream_root).resolve()
    raw_dir = Path(args.raw_dir).resolve() if args.raw_dir else upstream_root / "data" / "raw"
    output_dir = shadow_replay.ensure_directory(args.output_dir)
    results = run_shadow_candidate_monitor(
        upstream_root=upstream_root,
        output_dir=output_dir,
        raw_dir=raw_dir,
        max_age_days=max(0, int(args.max_age_days)),
        activation_lag_days=args.activation_lag_days,
        cost_bps=float(args.cost_bps),
    )
    print_monitor_console_summary(results)
    for row in results["paths"].to_dict("records"):
        print(f"{row['name']}={row['path']}")


if __name__ == "__main__":
    main()
