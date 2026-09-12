"""Application helpers for runtime state loading."""

from __future__ import annotations

from collections.abc import Mapping

from runtime_support import ExecutionIntegrityError, record_gating_event


def _check_rebased_asset_scope(raw_state, universe):
    if "accounting_rebase" not in raw_state:
        return
    opening = raw_state.get("last_balance_snapshot")
    if not isinstance(opening, Mapping) or not opening or not isinstance(universe, Mapping):
        raise ExecutionIntegrityError("managed_asset_scope_mismatch")
    # The approved opening and subsequent scoped snapshots form a non-expanding
    # boundary. BTC/BNB/USDT have dedicated roles outside the trend sleeve.
    allowed = set(opening) - {"BTC", "BNB", "USDT"}
    for symbol, meta in universe.items():
        asset = meta.get("base_asset") if isinstance(meta, Mapping) else None
        if asset not in allowed or symbol != f"{asset}USDT":
            raise ExecutionIntegrityError("managed_asset_scope_mismatch")


def load_cycle_state(
    runtime,
    report,
    allow_new_trend_entries_on_degraded,
    *,
    state_loader,
    resolve_runtime_trend_pool,
    normalize_trade_state,
    update_trend_pool_state,
    runtime_set_trade_state,
    get_runtime_trend_universe,
    append_report_error,
    trend_universe_setter,
):
    raw_state = state_loader(normalize=False)
    if raw_state is None:
        append_report_error(
            report,
            "Failed to load Firestore state. Check GCP credentials (GCP_SA_KEY / GOOGLE_APPLICATION_CREDENTIALS), service account validity, and Firestore API enablement.",
            stage="state_load",
        )
        report["status"] = "aborted"
        return None

    # Never normalize a legacy combined balance into a new Spot opening.
    if raw_state.get("balance_scope") != "spot":
        raise ExecutionIntegrityError("spot_scope_migration_required")

    resolved_trend_universe, trend_pool_resolution = resolve_runtime_trend_pool(runtime, raw_state)
    _check_rebased_asset_scope(raw_state, resolved_trend_universe)
    trend_universe_setter(resolved_trend_universe)

    state = normalize_trade_state(raw_state)
    runtime.trade_state = state
    update_trend_pool_state(state, trend_pool_resolution)
    runtime_trend_universe = get_runtime_trend_universe(state)
    _check_rebased_asset_scope(raw_state, runtime_trend_universe)
    runtime_set_trade_state(runtime, report, state, reason="trend_pool_metadata_refresh")
    allow_new_trend_entries = (not trend_pool_resolution["degraded"]) or allow_new_trend_entries_on_degraded
    if trend_pool_resolution["degraded"] and not allow_new_trend_entries:
        record_gating_event(
            report,
            gate="trend_buy_paused_degraded_mode",
            category="trend",
            detail=str(trend_pool_resolution.get("source_kind") or trend_pool_resolution.get("source", "unknown")),
        )
    return state, trend_pool_resolution, runtime_trend_universe, allow_new_trend_entries


def append_trend_pool_source_logs(
    log_buffer,
    trend_pool_resolution,
    allow_new_trend_entries,
    *,
    formatter,
    append_log_fn,
):
    for line in formatter(
        trend_pool_resolution,
        allow_new_trend_entries=allow_new_trend_entries,
    ):
        append_log_fn(log_buffer, line)
