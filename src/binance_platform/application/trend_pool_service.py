"""Application-level trend pool resolution helpers for BinancePlatform."""

from __future__ import annotations


def resolve_runtime_trend_pool(
    runtime,
    raw_state,
    *,
    load_trend_universe_from_live_pool_fn,
    get_trend_pool_contract_settings_fn,
    validate_trend_pool_payload_fn,
    build_trend_pool_resolution_fn,
    translate_fn,
):
    if runtime.trend_pool_payload is None:
        return load_trend_universe_from_live_pool_fn(state=raw_state, now_utc=runtime.now_utc)

    settings = get_trend_pool_contract_settings_fn()
    validated = validate_trend_pool_payload_fn(
        runtime.trend_pool_payload,
        source_label="runtime:trend_pool_payload",
        now_utc=runtime.now_utc,
        max_age_days=settings["max_age_days"],
        acceptable_modes=settings["acceptable_modes"],
        expected_pool_size=settings["expected_pool_size"],
        enforce_freshness=True,
    )
    if validated.get("ok"):
        resolution = build_trend_pool_resolution_fn(
            validated,
            source_kind="fresh_upstream",
            degraded=False,
            now_utc=runtime.now_utc,
            messages=[translate_fn("trend_pool_loaded_runtime_payload")],
        )
        return resolution["symbol_map"], resolution

    raise ValueError(
        "Injected trend_pool_payload failed validation: "
        + "; ".join(validated.get("errors", []) or ["unknown validation error"])
    )
