"""Live strategy orchestration entrypoint.

The live cycle remains here so the execution flow is easy to follow in one file.
Pure strategy math, state normalization, upstream contract handling, exchange
helpers, and live service adapters live in dedicated modules.
"""

import time
import sys
from pathlib import Path
import traceback
from entrypoints.cli import run_cli_entrypoint
from notify_i18n_support import translate as t
from degraded_mode_support import (
    format_trend_pool_source_logs as dm_format_trend_pool_source_logs,
    load_trend_universe_from_live_pool as dm_load_trend_universe_from_live_pool,
    resolve_trend_pool_source as dm_resolve_trend_pool_source,
    update_trend_pool_state as dm_update_trend_pool_state,
)
from quant_platform_kit.binance import (
    connect_client as qpk_connect_client,
    ensure_asset_available as qpk_ensure_asset_available,
    fetch_btc_market_snapshot as qpk_fetch_btc_market_snapshot,
    fetch_daily_indicators as qpk_fetch_daily_indicators,
    format_qty as qpk_format_qty,
    get_total_balance as qpk_get_total_balance,
    manage_usdt_earn_buffer as qpk_manage_usdt_earn_buffer,
)
from live_services import (
    get_firestore_client as live_get_firestore_client,
    get_state_doc_ref as live_get_state_doc_ref,
    send_tg_msg as live_send_tg_msg,
)
from market_snapshot_support import (
    capture_market_snapshot as ms_capture_market_snapshot,
)
from runtime_support import (
    ExecutionRuntime as _ExecutionRuntime,
    append_report_error,
    build_execution_report,
    next_order_id,
    runtime_call_client,
    runtime_notify,
    runtime_set_trade_state,
)
from runtime_config_support import (
    build_live_runtime as rc_build_live_runtime,
    get_env_bool as rc_get_env_bool,
    get_env_csv as rc_get_env_csv,
    get_env_int as rc_get_env_int,
    load_cycle_execution_settings as rc_load_cycle_execution_settings,
)
from application.cycle_service import execute_strategy_cycle
from application.execution_service import (
    execute_trend_buys as app_execute_trend_buys,
    execute_trend_sells as app_execute_trend_sells,
    execute_btc_dca_cycle as app_execute_btc_dca_cycle,
    execute_trend_rotation as app_execute_trend_rotation,
    run_daily_circuit_breaker as app_run_daily_circuit_breaker,
)
from application.portfolio_service import (
    append_portfolio_report as app_append_portfolio_report,
    build_balance_snapshot as app_build_balance_snapshot,
    compute_daily_pnls as app_compute_daily_pnls,
    compute_portfolio_allocation as app_compute_portfolio_allocation,
    maybe_rebase_daily_state_for_balance_change as app_maybe_rebase_daily_state_for_balance_change,
    maybe_reset_daily_state as app_maybe_reset_daily_state,
)
from application.state_service import (
    append_trend_pool_source_logs as app_append_trend_pool_source_logs,
    load_cycle_state as app_load_cycle_state,
)
from application.trend_pool_service import (
    resolve_runtime_trend_pool as app_resolve_runtime_trend_pool,
)
from infra.binance_runtime import (
    ensure_asset_available_runtime as infra_ensure_asset_available_runtime,
    ensure_runtime_client as infra_ensure_runtime_client,
    manage_usdt_earn_buffer_runtime as infra_manage_usdt_earn_buffer_runtime,
    resolve_runtime_btc_snapshot as infra_resolve_runtime_btc_snapshot,
    resolve_runtime_trend_indicators as infra_resolve_runtime_trend_indicators,
)
from infra.state_store import (
    load_runtime_trade_state as infra_load_runtime_trade_state,
    save_runtime_trade_state as infra_save_runtime_trade_state,
)
from reporting.status_reports import (
    append_portfolio_report as report_append_portfolio_report,
    append_rotation_summary as report_append_rotation_summary,
    append_trend_symbol_status as report_append_trend_symbol_status,
    build_btc_manual_hint as report_build_btc_manual_hint,
    get_periodic_report_bucket as report_get_periodic_report_bucket,
    maybe_send_periodic_btc_status_report as report_maybe_send_periodic_btc_status_report,
)
from strategy_core import (
    DEFAULT_POOL_SCORE_WEIGHTS,
    allocate_trend_buy_budget as shared_allocate_trend_buy_budget,
    build_stable_quality_pool as shared_build_stable_quality_pool,
    compute_allocation_budgets,
    get_dynamic_btc_base_order as shared_get_dynamic_btc_base_order,
    get_dynamic_btc_target_ratio as shared_get_dynamic_btc_target_ratio,
    rank_normalize as shared_rank_normalize,
    select_rotation_weights as shared_select_rotation_weights,
)
from strategy.rotation import (
    get_trend_sell_reason as strategy_get_trend_sell_reason,
    plan_trend_buys as strategy_plan_trend_buys,
    refresh_rotation_pool as strategy_refresh_rotation_pool,
)
from trade_state_support import (
    build_default_state as ts_build_default_state,
    default_trend_symbol_state as ts_default_trend_symbol_state,
    get_runtime_trend_universe as ts_get_runtime_trend_universe,
    get_symbol_trade_state as ts_get_symbol_trade_state,
    has_active_position as ts_has_active_position,
    infer_base_asset as ts_infer_base_asset,
    is_trend_symbol_state as ts_is_trend_symbol_state,
    normalize_symbol_state as ts_normalize_symbol_state,
    normalize_trade_state as ts_normalize_trade_state,
    record_trend_action as ts_record_trend_action,
    safe_float as ts_safe_float,
    set_symbol_trade_state as ts_set_symbol_trade_state,
    should_skip_duplicate_trend_action as ts_should_skip_duplicate_trend_action,
)
from trend_pool_support import (
    build_static_trend_pool_resolution as tp_build_static_trend_pool_resolution,
    build_trend_pool_resolution as tp_build_trend_pool_resolution,
    extract_trend_pool_symbols as tp_extract_trend_pool_symbols,
    get_default_live_pool_candidates as tp_get_default_live_pool_candidates,
    get_last_known_good_trend_pool as tp_get_last_known_good_trend_pool,
    get_trend_pool_contract_settings as tp_get_trend_pool_contract_settings,
    load_trend_pool_from_file as tp_load_trend_pool_from_file,
    load_trend_pool_from_firestore as tp_load_trend_pool_from_firestore,
    parse_trend_pool_date as tp_parse_trend_pool_date,
    parse_trend_universe_mapping as tp_parse_trend_universe_mapping,
    validate_trend_pool_payload as tp_validate_trend_pool_payload,
)

ExecutionRuntime = _ExecutionRuntime

SEPARATOR = "━━━━━━━━━━━━━━━━━━"


STATIC_TREND_UNIVERSE = {
    "ETHUSDT": {"base_asset": "ETH"},
    "SOLUSDT": {"base_asset": "SOL"},
    "XRPUSDT": {"base_asset": "XRP"},
    "LINKUSDT": {"base_asset": "LINK"},
    "AVAXUSDT": {"base_asset": "AVAX"},
    "ADAUSDT": {"base_asset": "ADA"},
    "DOGEUSDT": {"base_asset": "DOGE"},
    "TRXUSDT": {"base_asset": "TRX"},
    "ATOMUSDT": {"base_asset": "ATOM"},
    "LTCUSDT": {"base_asset": "LTC"},
    "BCHUSDT": {"base_asset": "BCH"},
}
TREND_UNIVERSE = STATIC_TREND_UNIVERSE.copy()

TREND_POOL_SIZE = 5
ROTATION_TOP_N = 2
MIN_HISTORY_DAYS = 365
MIN_AVG_QUOTE_VOL_180 = 8_000_000
POOL_MEMBERSHIP_BONUS = 0.10

BNB_FUEL_SYMBOL = "BNBUSDT"
BNB_FUEL_ASSET = "BNB"

DEFAULT_LIVE_POOL_LEGACY_PATH = (
    Path(__file__).resolve().parents[1]
    / "CryptoLeaderRotation"
    / "data"
    / "output"
    / "live_pool_legacy.json"
)
DEFAULT_TREND_POOL_FIRESTORE_COLLECTION = "strategy"
DEFAULT_TREND_POOL_FIRESTORE_DOCUMENT = "CRYPTO_LEADER_ROTATION_LIVE_POOL"
RETIRED_TREND_POSITIONS_KEY = "retired_trend_positions"
TREND_POOL_LAST_GOOD_PAYLOAD_KEY = "trend_pool_last_good_payload"
TREND_POOL_ACTION_HISTORY_KEY = "trend_action_history"
DEFAULT_TREND_POOL_MAX_AGE_DAYS = 45
DEFAULT_TREND_POOL_ACCEPTABLE_MODES = ("core_major",)


class BalanceFetchError(RuntimeError):
    pass


def get_env_int(name, default):
    return rc_get_env_int(name, default)


def get_env_bool(name, default=False):
    return rc_get_env_bool(name, default)


def get_env_csv(name, default_values):
    return rc_get_env_csv(name, default_values)


def default_trend_symbol_state():
    return ts_default_trend_symbol_state()


def safe_float(value, default=0.0):
    return ts_safe_float(value, default)


def infer_base_asset(symbol):
    return ts_infer_base_asset(symbol)


def is_trend_symbol_state(value):
    return ts_is_trend_symbol_state(value)


def normalize_symbol_state(value):
    return ts_normalize_symbol_state(value)


def has_active_position(position_state):
    return ts_has_active_position(position_state)


def parse_trend_pool_date(value):
    return tp_parse_trend_pool_date(value)


def parse_trend_universe_mapping(payload):
    return tp_parse_trend_universe_mapping(payload)


def extract_trend_pool_symbols(payload, symbol_map):
    return tp_extract_trend_pool_symbols(payload, symbol_map)


def get_trend_pool_contract_settings():
    return tp_get_trend_pool_contract_settings(
        max_age_days_default=DEFAULT_TREND_POOL_MAX_AGE_DAYS,
        acceptable_modes_default=DEFAULT_TREND_POOL_ACCEPTABLE_MODES,
        expected_pool_size_default=TREND_POOL_SIZE,
    )


def validate_trend_pool_payload(
    payload,
    source_label,
    *,
    now_utc=None,
    max_age_days=DEFAULT_TREND_POOL_MAX_AGE_DAYS,
    acceptable_modes=None,
    expected_pool_size=TREND_POOL_SIZE,
    enforce_freshness=True,
):
    return tp_validate_trend_pool_payload(
        payload,
        source_label,
        now_utc=now_utc,
        max_age_days=max_age_days,
        acceptable_modes=acceptable_modes,
        expected_pool_size=expected_pool_size,
        enforce_freshness=enforce_freshness,
    )


def get_default_live_pool_candidates():
    return tp_get_default_live_pool_candidates(DEFAULT_LIVE_POOL_LEGACY_PATH)


def get_firestore_client():
    return live_get_firestore_client()


def get_state_doc_ref():
    return live_get_state_doc_ref(collection="strategy", document="MULTI_ASSET_STATE")


def load_trend_pool_from_firestore(*, now_utc=None, settings=None):
    return tp_load_trend_pool_from_firestore(
        now_utc=now_utc,
        settings=settings or get_trend_pool_contract_settings(),
        default_collection=DEFAULT_TREND_POOL_FIRESTORE_COLLECTION,
        default_document=DEFAULT_TREND_POOL_FIRESTORE_DOCUMENT,
    )


def load_trend_pool_from_file(path, *, now_utc=None, settings=None):
    return tp_load_trend_pool_from_file(
        path,
        now_utc=now_utc,
        settings=settings or get_trend_pool_contract_settings(),
    )


def build_trend_pool_resolution(validated_payload, *, source_kind, degraded, now_utc=None, messages=None):
    return tp_build_trend_pool_resolution(
        validated_payload,
        source_kind=source_kind,
        degraded=degraded,
        now_utc=now_utc,
        messages=messages,
    )


def get_last_known_good_trend_pool(state, *, now_utc=None, settings=None):
    return tp_get_last_known_good_trend_pool(
        state,
        now_utc=now_utc,
        settings=settings or get_trend_pool_contract_settings(),
        last_good_payload_key=TREND_POOL_LAST_GOOD_PAYLOAD_KEY,
    )


def build_static_trend_pool_resolution(*, now_utc=None, messages=None):
    return tp_build_static_trend_pool_resolution(
        now_utc=now_utc,
        messages=messages,
        static_trend_universe=STATIC_TREND_UNIVERSE,
    )


def resolve_trend_pool_source(state=None, *, now_utc=None):
    return dm_resolve_trend_pool_source(
        state=state,
        now_utc=now_utc,
        default_live_pool_legacy_path=DEFAULT_LIVE_POOL_LEGACY_PATH,
        default_firestore_collection=DEFAULT_TREND_POOL_FIRESTORE_COLLECTION,
        default_firestore_document=DEFAULT_TREND_POOL_FIRESTORE_DOCUMENT,
        last_good_payload_key=TREND_POOL_LAST_GOOD_PAYLOAD_KEY,
        static_trend_universe=STATIC_TREND_UNIVERSE,
        max_age_days_default=DEFAULT_TREND_POOL_MAX_AGE_DAYS,
        acceptable_modes_default=DEFAULT_TREND_POOL_ACCEPTABLE_MODES,
        expected_pool_size_default=TREND_POOL_SIZE,
    )


def load_trend_universe_from_live_pool(state=None, *, now_utc=None):
    return dm_load_trend_universe_from_live_pool(
        state=state,
        now_utc=now_utc,
        default_live_pool_legacy_path=DEFAULT_LIVE_POOL_LEGACY_PATH,
        default_firestore_collection=DEFAULT_TREND_POOL_FIRESTORE_COLLECTION,
        default_firestore_document=DEFAULT_TREND_POOL_FIRESTORE_DOCUMENT,
        last_good_payload_key=TREND_POOL_LAST_GOOD_PAYLOAD_KEY,
        static_trend_universe=STATIC_TREND_UNIVERSE,
        max_age_days_default=DEFAULT_TREND_POOL_MAX_AGE_DAYS,
        acceptable_modes_default=DEFAULT_TREND_POOL_ACCEPTABLE_MODES,
        expected_pool_size_default=TREND_POOL_SIZE,
    )


def update_trend_pool_state(state, resolution):
    dm_update_trend_pool_state(
        state,
        resolution,
        last_good_payload_key=TREND_POOL_LAST_GOOD_PAYLOAD_KEY,
    )


def build_default_state():
    return ts_build_default_state(
        trend_universe=TREND_UNIVERSE,
        last_good_payload_key=TREND_POOL_LAST_GOOD_PAYLOAD_KEY,
        action_history_key=TREND_POOL_ACTION_HISTORY_KEY,
        retired_positions_key=RETIRED_TREND_POSITIONS_KEY,
    )


def normalize_trade_state(state):
    return ts_normalize_trade_state(
        state,
        trend_universe=TREND_UNIVERSE,
        last_good_payload_key=TREND_POOL_LAST_GOOD_PAYLOAD_KEY,
        action_history_key=TREND_POOL_ACTION_HISTORY_KEY,
        retired_positions_key=RETIRED_TREND_POSITIONS_KEY,
    )


def get_runtime_trend_universe(state):
    return ts_get_runtime_trend_universe(
        state,
        trend_universe=TREND_UNIVERSE,
        retired_positions_key=RETIRED_TREND_POSITIONS_KEY,
    )


def get_symbol_trade_state(state, symbol):
    return ts_get_symbol_trade_state(
        state,
        symbol,
        trend_universe=TREND_UNIVERSE,
        retired_positions_key=RETIRED_TREND_POSITIONS_KEY,
    )


def set_symbol_trade_state(state, symbol, symbol_state):
    ts_set_symbol_trade_state(
        state,
        symbol,
        symbol_state,
        trend_universe=TREND_UNIVERSE,
        retired_positions_key=RETIRED_TREND_POSITIONS_KEY,
    )


def should_skip_duplicate_trend_action(state, symbol, action, action_date):
    return ts_should_skip_duplicate_trend_action(
        state,
        symbol,
        action,
        action_date,
        action_history_key=TREND_POOL_ACTION_HISTORY_KEY,
    )


def record_trend_action(state, symbol, action, action_date):
    ts_record_trend_action(
        state,
        symbol,
        action,
        action_date,
        action_history_key=TREND_POOL_ACTION_HISTORY_KEY,
    )

# ==========================================
# 1. State persistence and Telegram
# ==========================================
def get_trade_state(normalize=True):
    return infra_load_runtime_trade_state(
        normalize_fn=normalize_trade_state,
        default_state_factory=build_default_state,
        normalize=normalize,
    )


def set_trade_state(data):
    infra_save_runtime_trade_state(
        data,
        normalize_fn=normalize_trade_state,
    )


def append_log(log_buffer, message):
    if log_buffer is not None:
        log_buffer.append(str(message))


def send_tg_msg(token, chat_id, text):
    live_send_tg_msg(token, chat_id, text)

# ==========================================
# 2. Earn and balance helpers
# ==========================================
def get_total_balance(client, asset, log_buffer=None):
    """Total balance for asset (spot + flexible earn)."""
    return qpk_get_total_balance(
        client,
        asset,
        on_spot_error=lambda exc: append_log(log_buffer, t("spot_balance_lookup_failed", asset=asset, error=exc)),
        on_earn_error=lambda exc: append_log(log_buffer, t("earn_balance_lookup_failed", asset=asset, error=exc)),
        balance_error_cls=BalanceFetchError,
    )


def log_and_notify(log_buffer, tg_token, tg_chat_id, text):
    append_log(log_buffer, text)
    send_tg_msg(tg_token, tg_chat_id, text)

def ensure_asset_available(client, asset, required_amount, tg_token, tg_chat_id):
    """Redeem from flexible earn if spot balance is below required amount."""
    return qpk_ensure_asset_available(
        client,
        asset,
        required_amount,
        on_redeem=lambda amount: send_tg_msg(
            tg_token,
            tg_chat_id,
            t("execution_spot_short_redeeming_from_earn", asset=asset, amount=amount),
        ),
        on_error=lambda exc: send_tg_msg(
            tg_token,
            tg_chat_id,
            t("execution_redeem_failed_asset", asset=asset, error=exc),
        ),
        sleep_fn=time.sleep,
    )

def manage_usdt_earn_buffer(client, target_buffer, tg_token, tg_chat_id, log_buffer):
    """Keep USDT spot buffer near target by subscribing/redeeming flexible earn."""
    qpk_manage_usdt_earn_buffer(
        client,
        target_buffer,
        on_subscribe=lambda amount: append_log(log_buffer, t("cash_manager_subscribed_to_earn", amount=amount)),
        on_redeem=lambda amount: append_log(log_buffer, t("cash_manager_redeeming_to_spot", amount=amount)),
        on_error=lambda exc: append_log(log_buffer, t("usdt_earn_buffer_maintenance_failed", error=exc)),
    )

def format_qty(client, symbol, qty):
    """Round quantity to exchange LOT_SIZE to avoid filter errors."""
    return qpk_format_qty(client, symbol, qty)

def get_dynamic_btc_target_ratio(total_equity):
    """BTC target weight increases with equity; no hard minimum."""
    return shared_get_dynamic_btc_target_ratio(total_equity)


def get_dynamic_btc_base_order(total_equity):
    """Daily DCA base order scales with total equity."""
    return shared_get_dynamic_btc_base_order(total_equity)


def get_periodic_report_bucket(now_utc, interval_hours):
    return report_get_periodic_report_bucket(now_utc, interval_hours)


def build_btc_manual_hint(btc_snapshot):
    return report_build_btc_manual_hint(btc_snapshot, translate_fn=t)


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
    notifier_fn=None,
):
    return report_maybe_send_periodic_btc_status_report(
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
        translate_fn=t,
        separator=SEPARATOR,
        notifier_fn=notifier_fn,
        send_tg_msg_fn=send_tg_msg,
    )


def fetch_daily_indicators(client, symbol, lookback_days=420):
    """Daily indicators for one symbol (rotation and risk)."""
    return qpk_fetch_daily_indicators(client, symbol, lookback_days=lookback_days)


def fetch_btc_market_snapshot(client, btc_price, lookback_days=700, log_buffer=None):
    """Single BTC daily series for DCA and trend gate."""
    return qpk_fetch_btc_market_snapshot(
        client,
        btc_price,
        lookback_days=lookback_days,
        on_fetch_error=lambda exc: log_buffer.append(t("btc_daily_fetch_failed", error=exc)) if log_buffer is not None else None,
        on_empty=lambda: log_buffer.append(t("btc_daily_data_empty")) if log_buffer is not None else None,
        on_insufficient=lambda length, last_time: log_buffer.append(
            t("btc_data_insufficient", length=length, last_time=last_time)
        )
        if log_buffer is not None
        else None,
    )


def rank_normalize(values):
    return shared_rank_normalize(values)


def build_stable_quality_pool(indicators_map, btc_snapshot, previous_pool):
    return shared_build_stable_quality_pool(
        indicators_map,
        btc_snapshot,
        previous_pool,
        pool_size=TREND_POOL_SIZE,
        min_history_days=MIN_HISTORY_DAYS,
        min_avg_quote_vol_180=MIN_AVG_QUOTE_VOL_180,
        membership_bonus=POOL_MEMBERSHIP_BONUS,
        score_weights=DEFAULT_POOL_SCORE_WEIGHTS,
    )


def refresh_rotation_pool(state, indicators_map, btc_snapshot, allow_refresh=True, now_utc=None):
    return strategy_refresh_rotation_pool(
        state,
        indicators_map,
        btc_snapshot,
        trend_universe_symbols=TREND_UNIVERSE.keys(),
        trend_pool_size=TREND_POOL_SIZE,
        build_stable_quality_pool_fn=build_stable_quality_pool,
        allow_refresh=allow_refresh,
        now_utc=now_utc,
    )


def select_rotation_weights(indicators_map, prices, btc_snapshot, candidate_pool, top_n):
    """Pick final trend holdings from monthly pool by relative BTC strength."""
    return shared_select_rotation_weights(
        indicators_map,
        prices,
        btc_snapshot,
        candidate_pool,
        top_n,
        weight_mode="inverse_vol",
    )


def allocate_trend_buy_budget(selected_candidates, buyable_symbols, total_budget):
    return shared_allocate_trend_buy_budget(selected_candidates, buyable_symbols, total_budget)


def resolve_runtime_trend_pool(runtime, raw_state):
    return app_resolve_runtime_trend_pool(
        runtime,
        raw_state,
        load_trend_universe_from_live_pool_fn=load_trend_universe_from_live_pool,
        get_trend_pool_contract_settings_fn=get_trend_pool_contract_settings,
        validate_trend_pool_payload_fn=validate_trend_pool_payload,
        build_trend_pool_resolution_fn=build_trend_pool_resolution,
        translate_fn=t,
    )


def resolve_runtime_btc_snapshot(runtime, btc_price, log_buffer):
    return infra_resolve_runtime_btc_snapshot(
        runtime,
        btc_price,
        log_buffer,
        fetch_btc_market_snapshot_fn=fetch_btc_market_snapshot,
    )


def resolve_runtime_trend_indicators(runtime):
    return infra_resolve_runtime_trend_indicators(
        runtime,
        TREND_UNIVERSE,
        fetch_daily_indicators_fn=fetch_daily_indicators,
    )


def ensure_asset_available_runtime(runtime, report, asset, required_amount, log_buffer):
    return infra_ensure_asset_available_runtime(
        runtime,
        report,
        asset,
        required_amount,
        log_buffer,
        runtime_call_client_fn=runtime_call_client,
        append_log_fn=append_log,
        runtime_notify_fn=runtime_notify,
        translate_fn=t,
        sleep_fn=time.sleep,
    )


def manage_usdt_earn_buffer_runtime(runtime, report, target_buffer, log_buffer, spot_free_override=None):
    infra_manage_usdt_earn_buffer_runtime(
        runtime,
        report,
        target_buffer,
        log_buffer,
        runtime_call_client_fn=runtime_call_client,
        append_log_fn=append_log,
        translate_fn=t,
        spot_free_override=spot_free_override,
    )


def get_tradable_qty(symbol, total_qty, prices, min_bnb_value):
    """Reserve BNB for fees; rest is tradable."""
    if symbol != "BNBUSDT":
        return max(0.0, total_qty)

    bnb_price = prices.get("BNBUSDT", 0.0)
    if bnb_price <= 0:
        return 0.0

    reserve_qty = (min_bnb_value * 1.2) / bnb_price
    return max(0.0, total_qty - reserve_qty)

# ==========================================
# 3. Core strategy
# ==========================================
def build_live_runtime(now_utc=None):
    return rc_build_live_runtime(
        now_utc=now_utc,
        state_loader=get_trade_state,
        state_writer=set_trade_state,
        notifier=lambda **kwargs: send_tg_msg(kwargs["token"], kwargs["chat_id"], kwargs["text"]),
    )


def _set_runtime_trend_universe(resolved_trend_universe):
    global TREND_UNIVERSE
    TREND_UNIVERSE = resolved_trend_universe


def _ensure_runtime_client(runtime, report):
    return infra_ensure_runtime_client(
        runtime,
        report,
        connect_client_fn=qpk_connect_client,
        append_report_error_fn=append_report_error,
        runtime_notify_fn=runtime_notify,
        translate_fn=t,
        sleep_fn=time.sleep,
    )


def _load_cycle_state(runtime, report, allow_new_trend_entries_on_degraded):
    return app_load_cycle_state(
        runtime,
        report,
        allow_new_trend_entries_on_degraded,
        state_loader=runtime.state_loader,
        resolve_runtime_trend_pool=resolve_runtime_trend_pool,
        normalize_trade_state=normalize_trade_state,
        update_trend_pool_state=update_trend_pool_state,
        runtime_set_trade_state=runtime_set_trade_state,
        get_runtime_trend_universe=get_runtime_trend_universe,
        append_report_error=append_report_error,
        trend_universe_setter=_set_runtime_trend_universe,
    )


def _append_trend_pool_source_logs(log_buffer, trend_pool_resolution, allow_new_trend_entries):
    app_append_trend_pool_source_logs(
        log_buffer,
        trend_pool_resolution,
        allow_new_trend_entries,
        formatter=dm_format_trend_pool_source_logs,
        append_log_fn=append_log,
    )


def _capture_market_snapshot(runtime, report, runtime_trend_universe, log_buffer, min_bnb_value, buy_bnb_amount):
    return ms_capture_market_snapshot(
        runtime,
        report,
        runtime_trend_universe,
        log_buffer,
        min_bnb_value,
        buy_bnb_amount,
        get_total_balance_fn=get_total_balance,
        ensure_asset_available_fn=ensure_asset_available_runtime,
        runtime_call_client_fn=runtime_call_client,
        runtime_notify_fn=runtime_notify,
        append_log_fn=append_log,
        resolve_btc_snapshot_fn=resolve_runtime_btc_snapshot,
        resolve_trend_indicators_fn=resolve_runtime_trend_indicators,
        bnb_fuel_symbol=BNB_FUEL_SYMBOL,
        bnb_fuel_asset=BNB_FUEL_ASSET,
    )


def _compute_portfolio_allocation(runtime_trend_universe, balances, prices, u_total, fuel_val):
    return app_compute_portfolio_allocation(
        runtime_trend_universe,
        balances,
        prices,
        u_total,
        fuel_val,
        compute_allocation_budgets_fn=compute_allocation_budgets,
    )


def _build_balance_snapshot(runtime_trend_universe, balances, u_total):
    return app_build_balance_snapshot(runtime_trend_universe, balances, u_total)


def _maybe_reset_daily_state(state, runtime, report, today_utc, total_equity, trend_val_equity):
    return app_maybe_reset_daily_state(
        state,
        runtime,
        report,
        today_utc,
        total_equity,
        trend_val_equity,
        runtime_set_trade_state_fn=runtime_set_trade_state,
    )


def _maybe_rebase_daily_state_for_balance_change(
    state,
    runtime,
    report,
    total_equity,
    trend_val_equity,
    current_balance_snapshot,
    log_buffer,
):
    return app_maybe_rebase_daily_state_for_balance_change(
        state,
        runtime,
        report,
        total_equity,
        trend_val_equity,
        current_balance_snapshot,
        log_buffer,
        runtime_set_trade_state_fn=runtime_set_trade_state,
        append_log_fn=append_log,
        translate_fn=t,
    )


def _compute_daily_pnls(state, total_equity, trend_equity):
    return app_compute_daily_pnls(state, total_equity, trend_equity)


def _append_portfolio_report(log_buffer, allocation, fuel_val, daily_pnl, trend_daily_pnl, btc_snapshot):
    return app_append_portfolio_report(
        log_buffer,
        allocation,
        fuel_val,
        daily_pnl,
        trend_daily_pnl,
        btc_snapshot,
        append_portfolio_report_fn=report_append_portfolio_report,
        append_log_fn=append_log,
        translate_fn=t,
        separator="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    )


def _run_daily_circuit_breaker(
    runtime,
    report,
    state,
    runtime_trend_universe,
    balances,
    u_total,
    prices,
    trend_daily_pnl,
    circuit_breaker_pct,
    log_buffer,
):
    return app_run_daily_circuit_breaker(
        runtime,
        report,
        state,
        runtime_trend_universe,
        balances,
        u_total,
        prices,
        trend_daily_pnl,
        circuit_breaker_pct,
        log_buffer,
        format_qty_fn=format_qty,
        runtime_notify_fn=runtime_notify,
        ensure_asset_available_fn=ensure_asset_available_runtime,
        runtime_call_client_fn=runtime_call_client,
        set_symbol_trade_state_fn=set_symbol_trade_state,
        runtime_set_trade_state_fn=runtime_set_trade_state,
        build_balance_snapshot_fn=_build_balance_snapshot,
        translate_fn=t,
    )


def _append_rotation_summary(log_buffer, official_trend_pool, active_trend_pool, selected_candidates):
    return report_append_rotation_summary(
        log_buffer,
        official_trend_pool,
        active_trend_pool,
        selected_candidates,
        append_log_fn=append_log,
        translate_fn=t,
    )


def _get_trend_sell_reason(state, symbol, curr_price, indicators, selected_candidates, atr_multiplier):
    return strategy_get_trend_sell_reason(
        state,
        symbol,
        curr_price,
        indicators,
        selected_candidates,
        atr_multiplier,
        get_symbol_trade_state_fn=get_symbol_trade_state,
        set_symbol_trade_state_fn=set_symbol_trade_state,
        translate_fn=t,
    )


def _execute_trend_sells(
    runtime,
    report,
    state,
    runtime_trend_universe,
    selected_candidates,
    trend_indicators,
    prices,
    balances,
    u_total,
    log_buffer,
    today_id_str,
    atr_multiplier,
):
    return app_execute_trend_sells(
        runtime,
        report,
        state,
        runtime_trend_universe,
        selected_candidates,
        trend_indicators,
        prices,
        balances,
        u_total,
        log_buffer,
        today_id_str,
        atr_multiplier,
        get_trend_sell_reason_fn=_get_trend_sell_reason,
        should_skip_duplicate_trend_action_fn=should_skip_duplicate_trend_action,
        append_log_fn=append_log,
        translate_fn=t,
        format_qty_fn=format_qty,
        ensure_asset_available_fn=ensure_asset_available_runtime,
        runtime_call_client_fn=runtime_call_client,
        next_order_id_fn=next_order_id,
        set_symbol_trade_state_fn=set_symbol_trade_state,
        record_trend_action_fn=record_trend_action,
        runtime_set_trade_state_fn=runtime_set_trade_state,
        runtime_notify_fn=runtime_notify,
    )


def _plan_trend_buys(
    state,
    runtime_trend_universe,
    selected_candidates,
    trend_indicators,
    prices,
    available_trend_buy_budget,
    allow_new_trend_entries,
):
    return strategy_plan_trend_buys(
        state,
        runtime_trend_universe,
        selected_candidates,
        trend_indicators,
        prices,
        available_trend_buy_budget,
        allow_new_trend_entries,
        get_symbol_trade_state_fn=get_symbol_trade_state,
        allocate_trend_buy_budget_fn=allocate_trend_buy_budget,
    )


def _execute_trend_buys(
    runtime,
    report,
    state,
    selected_candidates,
    eligible_buy_symbols,
    planned_trend_buys,
    prices,
    balances,
    u_total,
    log_buffer,
    today_id_str,
):
    return app_execute_trend_buys(
        runtime,
        report,
        state,
        selected_candidates,
        eligible_buy_symbols,
        planned_trend_buys,
        prices,
        balances,
        u_total,
        log_buffer,
        today_id_str,
        should_skip_duplicate_trend_action_fn=should_skip_duplicate_trend_action,
        append_log_fn=append_log,
        translate_fn=t,
        format_qty_fn=format_qty,
        ensure_asset_available_fn=ensure_asset_available_runtime,
        runtime_call_client_fn=runtime_call_client,
        next_order_id_fn=next_order_id,
        set_symbol_trade_state_fn=set_symbol_trade_state,
        record_trend_action_fn=record_trend_action,
        runtime_set_trade_state_fn=runtime_set_trade_state,
        runtime_notify_fn=runtime_notify,
    )


def _append_trend_symbol_status(log_buffer, runtime_trend_universe, prices, trend_indicators, state, btc_snapshot):
    return report_append_trend_symbol_status(
        log_buffer,
        runtime_trend_universe,
        prices,
        trend_indicators,
        state,
        btc_snapshot,
        append_log_fn=append_log,
        translate_fn=t,
        get_symbol_trade_state_fn=get_symbol_trade_state,
    )


def _execute_trend_rotation(
    runtime,
    report,
    state,
    runtime_trend_universe,
    trend_indicators,
    btc_snapshot,
    prices,
    balances,
    u_total,
    fuel_val,
    log_buffer,
    today_id_str,
    allow_new_trend_entries,
    allow_pool_refresh,
    atr_multiplier,
):
    return app_execute_trend_rotation(
        runtime,
        report,
        state,
        runtime_trend_universe,
        trend_indicators,
        btc_snapshot,
        prices,
        balances,
        u_total,
        fuel_val,
        log_buffer,
        today_id_str,
        allow_new_trend_entries,
        allow_pool_refresh,
        atr_multiplier,
        refresh_rotation_pool=refresh_rotation_pool,
        select_rotation_weights=select_rotation_weights,
        append_rotation_summary=_append_rotation_summary,
        compute_portfolio_allocation=_compute_portfolio_allocation,
        execute_trend_sells=_execute_trend_sells,
        plan_trend_buys=_plan_trend_buys,
        execute_trend_buys=_execute_trend_buys,
        append_trend_symbol_status=_append_trend_symbol_status,
        rotation_top_n=ROTATION_TOP_N,
        official_trend_pool_symbols=list(TREND_UNIVERSE.keys()),
    )


def _execute_btc_dca_cycle(
    runtime,
    report,
    state,
    balances,
    prices,
    u_total,
    total_equity,
    dca_usdt_pool,
    dca_val,
    btc_snapshot,
    btc_target_ratio,
    today_id_str,
    log_buffer,
):
    return app_execute_btc_dca_cycle(
        runtime,
        report,
        state,
        balances,
        prices,
        u_total,
        total_equity,
        dca_usdt_pool,
        dca_val,
        btc_snapshot,
        btc_target_ratio,
        today_id_str,
        log_buffer,
        append_log_fn=append_log,
        translate_fn=t,
        get_dynamic_btc_base_order=get_dynamic_btc_base_order,
        format_qty_fn=format_qty,
        ensure_asset_available_fn=ensure_asset_available_runtime,
        runtime_call_client_fn=runtime_call_client,
        next_order_id_fn=next_order_id,
        runtime_notify_fn=runtime_notify,
        runtime_set_trade_state_fn=runtime_set_trade_state,
    )


def execute_cycle(runtime):
    global TREND_UNIVERSE
    previous_trend_universe = {symbol: meta.copy() for symbol, meta in TREND_UNIVERSE.items()}

    try:
        return execute_strategy_cycle(
            runtime,
            build_execution_report=build_execution_report,
            ensure_runtime_client=_ensure_runtime_client,
            load_cycle_execution_settings=rc_load_cycle_execution_settings,
            load_cycle_state=_load_cycle_state,
            append_trend_pool_source_logs=_append_trend_pool_source_logs,
            capture_market_snapshot=_capture_market_snapshot,
            compute_portfolio_allocation=_compute_portfolio_allocation,
            build_balance_snapshot=_build_balance_snapshot,
            maybe_reset_daily_state=_maybe_reset_daily_state,
            maybe_rebase_daily_state_for_balance_change=_maybe_rebase_daily_state_for_balance_change,
            compute_daily_pnls=_compute_daily_pnls,
            append_portfolio_report=_append_portfolio_report,
            run_daily_circuit_breaker=_run_daily_circuit_breaker,
            execute_trend_rotation=_execute_trend_rotation,
            execute_btc_dca_cycle=_execute_btc_dca_cycle,
            manage_usdt_earn_buffer_runtime=manage_usdt_earn_buffer_runtime,
            maybe_send_periodic_btc_status_report=maybe_send_periodic_btc_status_report,
            runtime_set_trade_state=runtime_set_trade_state,
            append_report_error=append_report_error,
            runtime_notify=runtime_notify,
            translate_fn=t,
            traceback_module=traceback,
        )
    finally:
        TREND_UNIVERSE = previous_trend_universe


def main():
    return run_cli_entrypoint(
        runtime_builder=build_live_runtime,
        execute_cycle=execute_cycle,
        output_printer=print,
        exit_fn=sys.exit,
    )


if __name__ == "__main__":
    main()
