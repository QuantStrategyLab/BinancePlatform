"""Application helpers for runtime state loading."""

from __future__ import annotations

from collections.abc import Mapping

from runtime_support import ExecutionIntegrityError, record_gating_event


_DEDICATED_ASSETS = {"BTC", "BNB", "USDT"}


def _rebased_approved_assets(raw_state):
    if "accounting_rebase" not in raw_state:
        return None
    marker = raw_state.get("accounting_rebase")
    opening = raw_state.get("last_balance_snapshot")
    if not isinstance(marker, Mapping) or not isinstance(opening, Mapping) or not opening:
        raise ExecutionIntegrityError("managed_asset_scope_mismatch")

    approved = set(opening)
    checkpoint = raw_state.get("earn_accrual_checkpoint")
    if checkpoint is not None:
        checkpoint_assets = checkpoint.get("assets") if isinstance(checkpoint, Mapping) else None
        if not isinstance(checkpoint_assets, Mapping) or set(checkpoint_assets) != approved:
            raise ExecutionIntegrityError("managed_asset_scope_mismatch")
        approved = set(checkpoint_assets)

    if not _DEDICATED_ASSETS.issubset(approved) or any(
        not isinstance(asset, str)
        or not asset
        or asset != asset.strip().upper()
        or not asset.isalnum()
        for asset in approved
    ):
        raise ExecutionIntegrityError("managed_asset_scope_mismatch")
    return approved


def _has_active_symbol_state(raw_state, symbol):
    values = [raw_state.get(symbol)]
    retired = raw_state.get("retired_trend_positions")
    if isinstance(retired, Mapping):
        values.append(retired.get(symbol))
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if value.get("is_holding") is True:
            return True
        for key in ("entry_price", "highest_price", "holding_qty"):
            raw = value.get(key, 0.0)
            try:
                if float(raw or 0.0) != 0.0:
                    return True
            except (TypeError, ValueError):
                return True
    return False


def _canonical_trend_meta(symbol, meta):
    asset = meta.get("base_asset") if isinstance(meta, Mapping) else None
    if (
        not isinstance(symbol, str)
        or not isinstance(asset, str)
        or asset in _DEDICATED_ASSETS
        or asset != asset.strip().upper()
        or symbol != f"{asset}USDT"
    ):
        raise ExecutionIntegrityError("managed_asset_scope_mismatch")
    return asset, dict(meta)


def _prepare_rebased_universes(raw_state, resolved_universe):
    approved = _rebased_approved_assets(raw_state)
    if approved is None:
        return resolved_universe, resolved_universe
    if not isinstance(resolved_universe, Mapping):
        raise ExecutionIntegrityError("managed_asset_scope_mismatch")

    approved_trend_assets = approved - _DEDICATED_ASSETS
    retired = raw_state.get("retired_trend_positions")
    state_symbols = {
        symbol
        for symbol in raw_state
        if isinstance(symbol, str)
        and symbol.endswith("USDT")
        and symbol.removesuffix("USDT") not in _DEDICATED_ASSETS
    }
    if isinstance(retired, Mapping):
        state_symbols.update(
            symbol
            for symbol in retired
            if isinstance(symbol, str) and symbol.endswith("USDT")
        )
    for symbol in state_symbols:
        asset = symbol.removesuffix("USDT")
        if asset not in approved_trend_assets and _has_active_symbol_state(raw_state, symbol):
            raise ExecutionIntegrityError("managed_asset_scope_mismatch")

    candidate_universe = {}
    for symbol, meta in resolved_universe.items():
        asset, canonical_meta = _canonical_trend_meta(symbol, meta)
        if asset in approved_trend_assets:
            candidate_universe[symbol] = canonical_meta
        elif _has_active_symbol_state(raw_state, symbol):
            raise ExecutionIntegrityError("managed_asset_scope_mismatch")

    effective_universe = dict(candidate_universe)
    for asset in sorted(approved_trend_assets):
        symbol = f"{asset}USDT"
        effective_universe.setdefault(
            symbol,
            {"base_asset": asset, "valuation_only": True},
        )
    return candidate_universe, effective_universe


def _check_rebased_asset_scope(raw_state, universe):
    approved = _rebased_approved_assets(raw_state)
    if approved is None:
        return
    if not isinstance(universe, Mapping):
        raise ExecutionIntegrityError("managed_asset_scope_mismatch")
    allowed = approved - _DEDICATED_ASSETS
    for symbol, meta in universe.items():
        asset, _canonical_meta = _canonical_trend_meta(symbol, meta)
        if asset not in allowed:
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

    resolved_trend_universe, trend_pool_resolution = resolve_runtime_trend_pool(runtime, raw_state)
    runtime.trend_pool_contract = dict(trend_pool_resolution)
    candidate_trend_universe, effective_trend_universe = _prepare_rebased_universes(
        raw_state, resolved_trend_universe
    )
    trend_universe_setter(effective_trend_universe)

    state = normalize_trade_state(raw_state)
    runtime.trade_state = state
    update_trend_pool_state(state, trend_pool_resolution)
    if candidate_trend_universe is not resolved_trend_universe:
        cached_pool = state.get("rotation_pool_symbols")
        state["rotation_pool_symbols"] = [
            symbol
            for symbol in cached_pool if symbol in candidate_trend_universe
        ] if isinstance(cached_pool, list) else []
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
