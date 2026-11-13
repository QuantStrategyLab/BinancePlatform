"""BinancePlatform live strategy orchestration entrypoint.

Thin facade over the platform split modules. Public and private helpers are
re-exported by explicit name so existing ``import main`` / monkeypatch call
sites keep working. Mutable process globals resolve through ``__getattr__``
so rebinding inside ``runtime_wiring`` stays visible on ``main``.
"""
from __future__ import annotations

import sys
import types

import http_routes as _http_routes
import notifications_runtime as _notifications_runtime
import runtime_wiring as _runtime_wiring
import strategy_plugins_runtime as _strategy_plugins_runtime

from runtime_wiring import (
    run_cli_entrypoint, build_strategy_display_name, t,
    dm_format_trend_pool_source_logs, dm_load_trend_universe_from_live_pool, dm_resolve_trend_pool_source,
    dm_update_trend_pool_state, qpk_connect_client, qpk_fetch_btc_market_snapshot,
    qpk_fetch_daily_indicators, qpk_format_qty, bind_trade_state_access,
    live_get_firestore_client, live_get_state_doc_ref, live_send_tg_msg,
    ms_capture_market_snapshot, ms_top_up_bnb_fuel, read_managed_balance,
    append_report_error, build_execution_report, next_order_id,
    runtime_call_client, runtime_notify, runtime_set_trade_state,
    rc_build_live_runtime, rc_get_env_bool, rc_get_env_csv,
    rc_get_env_int, rc_load_cycle_execution_settings, execute_strategy_cycle,
    app_execute_trend_buys, app_execute_trend_sells, app_execute_btc_dca_cycle,
    app_execute_trend_rotation, app_run_daily_circuit_breaker, app_append_portfolio_report,
    app_build_balance_snapshot, app_compute_daily_pnls, app_maybe_rebase_daily_state_for_balance_change,
    app_maybe_reset_daily_state, app_append_trend_pool_source_logs, app_load_cycle_state,
    app_resolve_runtime_trend_pool, infra_ensure_asset_available_runtime, infra_ensure_runtime_client,
    infra_manage_usdt_earn_buffer_runtime, infra_resolve_runtime_btc_snapshot, infra_resolve_runtime_trend_indicators,
    infra_load_runtime_trade_state, infra_save_runtime_trade_state, report_append_portfolio_report,
    report_append_rotation_summary, report_append_trend_symbol_status, report_build_btc_manual_hint,
    report_get_periodic_report_bucket, report_maybe_send_periodic_btc_status_report, map_decision_to_allocation,
    map_decision_to_rotation_plan, public_stoploss_observation, load_strategy_runtime,
    RunContext, bind_run_context, get_active_run_context,
    reset_run_context, resolve_strategy_runtime, resolve_trend_universe,
    set_active_trend_universe, DEFAULT_STRATEGY_PROFILE, ts_build_default_state,
    ts_default_trend_symbol_state, ts_get_runtime_trend_universe, ts_get_symbol_trade_state,
    ts_has_active_position, ts_infer_base_asset, ts_is_trend_symbol_state,
    ts_normalize_symbol_state, ts_normalize_trade_state, ts_record_trend_action,
    ts_safe_float, ts_set_symbol_trade_state, ts_should_skip_duplicate_trend_action,
    tp_build_static_trend_pool_resolution, tp_build_trend_pool_resolution, tp_extract_trend_pool_symbols,
    tp_get_default_live_pool_candidates, tp_get_last_known_good_trend_pool, tp_get_trend_pool_contract_settings,
    tp_load_trend_pool_from_file, tp_load_trend_pool_from_firestore, tp_parse_trend_pool_date,
    tp_parse_trend_universe_mapping, tp_validate_trend_pool_payload, ExecutionRuntime,
    _SAFE_RUNTIME_SETUP_REASONS, _RUNTIME_ERROR_NOTIFICATION_REASONS, _AUTHORITY_ERROR_REASON_CODES,
    _runtime_setup_reason, _load_import_safe_strategy_runtime, SEPARATOR,
    STATIC_TREND_UNIVERSE, BNB_FUEL_SYMBOL, BNB_FUEL_ASSET,
    DEFAULT_TREND_POOL_FIRESTORE_COLLECTION, DEFAULT_TREND_POOL_FIRESTORE_DOCUMENT, RETIRED_TREND_POSITIONS_KEY,
    TREND_POOL_LAST_GOOD_PAYLOAD_KEY, TREND_POOL_ACTION_HISTORY_KEY, BTC_MARKET_SNAPSHOT_RETRY_DELAYS,
    _activate_execution_strategy_runtime, BalanceFetchError, get_env_int,
    get_env_bool, get_env_csv, default_trend_symbol_state,
    safe_float, infer_base_asset, is_trend_symbol_state,
    normalize_symbol_state, has_active_position, parse_trend_pool_date,
    parse_trend_universe_mapping, extract_trend_pool_symbols, get_trend_pool_contract_settings,
    validate_trend_pool_payload, get_default_live_pool_candidates, get_firestore_client,
    get_state_doc_ref, load_trend_pool_from_firestore, load_trend_pool_from_file,
    build_trend_pool_resolution, get_last_known_good_trend_pool, build_static_trend_pool_resolution,
    resolve_trend_pool_source, load_trend_universe_from_live_pool, update_trend_pool_state,
    build_default_state, normalize_trade_state, get_runtime_trend_universe,
    get_symbol_trade_state, set_symbol_trade_state, should_skip_duplicate_trend_action,
    record_trend_action, get_trade_state, set_trade_state,
    append_log, get_total_balance, ensure_asset_available,
    manage_usdt_earn_buffer, format_qty, get_periodic_report_bucket,
    build_btc_manual_hint, maybe_send_periodic_btc_status_report, fetch_daily_indicators,
    fetch_btc_market_snapshot, resolve_runtime_trend_pool, resolve_runtime_btc_snapshot,
    enrich_btc_snapshot_with_cycle_indicators, resolve_runtime_trend_indicators, ensure_asset_available_runtime,
    manage_usdt_earn_buffer_runtime, get_tradable_qty, build_live_runtime,
    _module_set_trend_universe, _set_runtime_trend_universe, _ensure_runtime_client,
    _load_cycle_state, _append_trend_pool_source_logs, _capture_market_snapshot,
    _top_up_bnb_fuel, _resolve_strategy_evaluation, _resolve_strategy_plan,
    _compute_portfolio_allocation, _build_balance_snapshot, _maybe_reset_daily_state,
    _maybe_rebase_daily_state_for_balance_change, _compute_daily_pnls, _append_portfolio_report,
    _run_daily_circuit_breaker, _append_rotation_summary, _execute_trend_sells,
    _execute_trend_buys, _append_trend_symbol_status, _execute_trend_rotation,
    _execute_btc_dca_cycle,
)
from notifications_runtime import (
    _notify_runtime_error,
    _runtime_error_notification_message,
    _send_failure_tg_message,
    _send_routine_tg_message,
    log_and_notify,
    send_tg_msg,
)
from http_routes import (
    execute_cycle,
    main,
)

__all__ = [
    'run_cli_entrypoint',
    'build_strategy_display_name',
    't',
    'dm_format_trend_pool_source_logs',
    'dm_load_trend_universe_from_live_pool',
    'dm_resolve_trend_pool_source',
    'dm_update_trend_pool_state',
    'qpk_connect_client',
    'qpk_fetch_btc_market_snapshot',
    'qpk_fetch_daily_indicators',
    'qpk_format_qty',
    'bind_trade_state_access',
    'live_get_firestore_client',
    'live_get_state_doc_ref',
    'live_send_tg_msg',
    'ms_capture_market_snapshot',
    'ms_top_up_bnb_fuel',
    'read_managed_balance',
    'append_report_error',
    'build_execution_report',
    'next_order_id',
    'runtime_call_client',
    'runtime_notify',
    'runtime_set_trade_state',
    'rc_build_live_runtime',
    'rc_get_env_bool',
    'rc_get_env_csv',
    'rc_get_env_int',
    'rc_load_cycle_execution_settings',
    'execute_strategy_cycle',
    'app_execute_trend_buys',
    'app_execute_trend_sells',
    'app_execute_btc_dca_cycle',
    'app_execute_trend_rotation',
    'app_run_daily_circuit_breaker',
    'app_append_portfolio_report',
    'app_build_balance_snapshot',
    'app_compute_daily_pnls',
    'app_maybe_rebase_daily_state_for_balance_change',
    'app_maybe_reset_daily_state',
    'app_append_trend_pool_source_logs',
    'app_load_cycle_state',
    'app_resolve_runtime_trend_pool',
    'infra_ensure_asset_available_runtime',
    'infra_ensure_runtime_client',
    'infra_manage_usdt_earn_buffer_runtime',
    'infra_resolve_runtime_btc_snapshot',
    'infra_resolve_runtime_trend_indicators',
    'infra_load_runtime_trade_state',
    'infra_save_runtime_trade_state',
    'report_append_portfolio_report',
    'report_append_rotation_summary',
    'report_append_trend_symbol_status',
    'report_build_btc_manual_hint',
    'report_get_periodic_report_bucket',
    'report_maybe_send_periodic_btc_status_report',
    'map_decision_to_allocation',
    'map_decision_to_rotation_plan',
    'public_stoploss_observation',
    'load_strategy_runtime',
    'RunContext',
    'bind_run_context',
    'get_active_run_context',
    'reset_run_context',
    'resolve_strategy_runtime',
    'resolve_trend_universe',
    'set_active_trend_universe',
    'DEFAULT_STRATEGY_PROFILE',
    'ts_build_default_state',
    'ts_default_trend_symbol_state',
    'ts_get_runtime_trend_universe',
    'ts_get_symbol_trade_state',
    'ts_has_active_position',
    'ts_infer_base_asset',
    'ts_is_trend_symbol_state',
    'ts_normalize_symbol_state',
    'ts_normalize_trade_state',
    'ts_record_trend_action',
    'ts_safe_float',
    'ts_set_symbol_trade_state',
    'ts_should_skip_duplicate_trend_action',
    'tp_build_static_trend_pool_resolution',
    'tp_build_trend_pool_resolution',
    'tp_extract_trend_pool_symbols',
    'tp_get_default_live_pool_candidates',
    'tp_get_last_known_good_trend_pool',
    'tp_get_trend_pool_contract_settings',
    'tp_load_trend_pool_from_file',
    'tp_load_trend_pool_from_firestore',
    'tp_parse_trend_pool_date',
    'tp_parse_trend_universe_mapping',
    'tp_validate_trend_pool_payload',
    'ExecutionRuntime',
    '_SAFE_RUNTIME_SETUP_REASONS',
    '_RUNTIME_ERROR_NOTIFICATION_REASONS',
    '_AUTHORITY_ERROR_REASON_CODES',
    '_runtime_setup_reason',
    '_load_import_safe_strategy_runtime',
    'SEPARATOR',
    'STATIC_TREND_UNIVERSE',
    'BNB_FUEL_SYMBOL',
    'BNB_FUEL_ASSET',
    'DEFAULT_TREND_POOL_FIRESTORE_COLLECTION',
    'DEFAULT_TREND_POOL_FIRESTORE_DOCUMENT',
    'RETIRED_TREND_POSITIONS_KEY',
    'TREND_POOL_LAST_GOOD_PAYLOAD_KEY',
    'TREND_POOL_ACTION_HISTORY_KEY',
    'BTC_MARKET_SNAPSHOT_RETRY_DELAYS',
    '_activate_execution_strategy_runtime',
    'BalanceFetchError',
    'get_env_int',
    'get_env_bool',
    'get_env_csv',
    'default_trend_symbol_state',
    'safe_float',
    'infer_base_asset',
    'is_trend_symbol_state',
    'normalize_symbol_state',
    'has_active_position',
    'parse_trend_pool_date',
    'parse_trend_universe_mapping',
    'extract_trend_pool_symbols',
    'get_trend_pool_contract_settings',
    'validate_trend_pool_payload',
    'get_default_live_pool_candidates',
    'get_firestore_client',
    'get_state_doc_ref',
    'load_trend_pool_from_firestore',
    'load_trend_pool_from_file',
    'build_trend_pool_resolution',
    'get_last_known_good_trend_pool',
    'build_static_trend_pool_resolution',
    'resolve_trend_pool_source',
    'load_trend_universe_from_live_pool',
    'update_trend_pool_state',
    'build_default_state',
    'normalize_trade_state',
    'get_runtime_trend_universe',
    'get_symbol_trade_state',
    'set_symbol_trade_state',
    'should_skip_duplicate_trend_action',
    'record_trend_action',
    'get_trade_state',
    'set_trade_state',
    'append_log',
    'get_total_balance',
    'ensure_asset_available',
    'manage_usdt_earn_buffer',
    'format_qty',
    'get_periodic_report_bucket',
    'build_btc_manual_hint',
    'maybe_send_periodic_btc_status_report',
    'fetch_daily_indicators',
    'fetch_btc_market_snapshot',
    'resolve_runtime_trend_pool',
    'resolve_runtime_btc_snapshot',
    'enrich_btc_snapshot_with_cycle_indicators',
    'resolve_runtime_trend_indicators',
    'ensure_asset_available_runtime',
    'manage_usdt_earn_buffer_runtime',
    'get_tradable_qty',
    'build_live_runtime',
    '_module_set_trend_universe',
    '_set_runtime_trend_universe',
    '_ensure_runtime_client',
    '_load_cycle_state',
    '_append_trend_pool_source_logs',
    '_capture_market_snapshot',
    '_top_up_bnb_fuel',
    '_resolve_strategy_evaluation',
    '_resolve_strategy_plan',
    '_compute_portfolio_allocation',
    '_build_balance_snapshot',
    '_maybe_reset_daily_state',
    '_maybe_rebase_daily_state_for_balance_change',
    '_compute_daily_pnls',
    '_append_portfolio_report',
    '_run_daily_circuit_breaker',
    '_append_rotation_summary',
    '_execute_trend_sells',
    '_execute_trend_buys',
    '_append_trend_symbol_status',
    '_execute_trend_rotation',
    '_execute_btc_dca_cycle',
    '_notify_runtime_error',
    '_runtime_error_notification_message',
    '_send_failure_tg_message',
    '_send_routine_tg_message',
    'log_and_notify',
    'send_tg_msg',
    'execute_cycle',
    'main',
    'DEFAULT_LIVE_POOL_LEGACY_PATH',
    'DEFAULT_TREND_POOL_ACCEPTABLE_MODES',
    'DEFAULT_TREND_POOL_MAX_AGE_DAYS',
    'STRATEGY_RUNTIME',
    'TREND_POOL_SIZE',
    'TREND_UNIVERSE',
]

_IMPL_MODULES = (
    _runtime_wiring,
    _notifications_runtime,
    _strategy_plugins_runtime,
    _http_routes,
)

# Rebound by runtime_wiring during activate / universe updates. Seeded into
# this module's __dict__ so unittest.mock.patch.object treats them as local
# (restore via setattr on exit) while writes still sync to runtime_wiring.
_MUTABLE_PROCESS_GLOBALS = frozenset({
    "DEFAULT_LIVE_POOL_LEGACY_PATH",
    "DEFAULT_TREND_POOL_ACCEPTABLE_MODES",
    "DEFAULT_TREND_POOL_MAX_AGE_DAYS",
    "STRATEGY_RUNTIME",
    "TREND_POOL_SIZE",
    "TREND_UNIVERSE",
})


class _BinanceMainFacade(types.ModuleType):
    """Forward attribute writes to the owning implementation module."""

    def __getattr__(self, name: str):
        for module in _IMPL_MODULES:
            if name in module.__dict__:
                return module.__dict__[name]
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value):
        super().__setattr__(name, value)
        for module in _IMPL_MODULES:
            if name in module.__dict__ or name in _MUTABLE_PROCESS_GLOBALS:
                setattr(module, name, value)
                return

    def __delattr__(self, name: str):
        # patch.object may delattr when the name was not local; tolerate that
        # for forwarded symbols while keeping the implementation binding.
        if name in self.__dict__:
            super().__delattr__(name)
            return
        for module in _IMPL_MODULES:
            if name in module.__dict__:
                return
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_facade = sys.modules[__name__]
_facade.__class__ = _BinanceMainFacade
for _mutable_name in _MUTABLE_PROCESS_GLOBALS:
    types.ModuleType.__setattr__(
        _facade,
        _mutable_name,
        getattr(_runtime_wiring, _mutable_name),
    )
del _facade, _mutable_name

if __name__ == "__main__":
    main()
