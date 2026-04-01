"""Application helpers for runtime state loading."""

from __future__ import annotations

from runtime_support import record_gating_event


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

    resolved_trend_universe, trend_pool_resolution = resolve_runtime_trend_pool(runtime, raw_state)
    trend_universe_setter(resolved_trend_universe)

    state = normalize_trade_state(raw_state)
    update_trend_pool_state(state, trend_pool_resolution)
    runtime_set_trade_state(runtime, report, state, reason="trend_pool_metadata_refresh")

    runtime_trend_universe = get_runtime_trend_universe(state)
    allow_new_trend_entries = (not trend_pool_resolution["degraded"]) or allow_new_trend_entries_on_degraded
    if trend_pool_resolution["degraded"] and not allow_new_trend_entries:
        record_gating_event(
            report,
            gate="trend_buy_paused_degraded_mode",
            category="trend",
            detail=str(trend_pool_resolution.get("source", "unknown")),
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
