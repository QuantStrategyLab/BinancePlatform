def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def infer_base_asset(symbol):
    return symbol[:-4] if isinstance(symbol, str) and symbol.endswith("USDT") else symbol


def default_trend_symbol_state():
    return {"is_holding": False, "entry_price": 0.0, "highest_price": 0.0}


def is_trend_symbol_state(value):
    return isinstance(value, dict) and any(
        key in value for key in ("is_holding", "entry_price", "highest_price")
    )


def normalize_symbol_state(value):
    state = default_trend_symbol_state()
    if not isinstance(value, dict):
        return state
    state["is_holding"] = bool(value.get("is_holding", state["is_holding"]))
    state["entry_price"] = safe_float(value.get("entry_price", state["entry_price"]))
    state["highest_price"] = safe_float(value.get("highest_price", state["highest_price"]))
    return state


def has_active_position(position_state):
    return bool(
        position_state.get("is_holding")
        or safe_float(position_state.get("entry_price", 0.0)) > 0.0
        or safe_float(position_state.get("highest_price", 0.0)) > 0.0
    )


def build_default_state(
    *,
    trend_universe,
    last_good_payload_key,
    action_history_key,
    retired_positions_key,
):
    state = {
        "BTCUSDT": {"holding_qty": 0.0, "avg_cost": 0.0},
        "daily_equity_base": 0.0,
        "daily_trend_equity_base": 0.0,
        # Where `daily_trend_equity_base` comes from (e.g. "trend_val").
        # Used to avoid false circuit-breaker triggers after strategy changes/deploys.
        "daily_trend_pnl_basis": "",
        "last_reset_date": "",
        "is_circuit_broken": False,
        "dca_last_buy_date": "",
        "dca_last_sell_date": "",
        "rotation_pool_last_month": "",
        "rotation_pool_symbols": [],
        "rotation_pool_source_version": "",
        "rotation_pool_source_as_of_date": "",
        "last_btc_status_report_bucket": "",
        "trend_pool_source": "",
        "trend_pool_source_detail": "",
        "trend_pool_is_fresh": False,
        "trend_pool_degraded": False,
        "trend_pool_as_of_date": "",
        "trend_pool_version": "",
        "trend_pool_mode": "",
        "trend_pool_source_project": "",
        "trend_pool_loaded_at": "",
        "trend_pool_messages": [],
        "last_balance_snapshot": {},
        last_good_payload_key: {},
        action_history_key: {},
        retired_positions_key: {},
    }
    for symbol in trend_universe:
        state[symbol] = default_trend_symbol_state()
    return state


def normalize_trade_state(
    state,
    *,
    trend_universe,
    last_good_payload_key,
    action_history_key,
    retired_positions_key,
):
    normalized = build_default_state(
        trend_universe=trend_universe,
        last_good_payload_key=last_good_payload_key,
        action_history_key=action_history_key,
        retired_positions_key=retired_positions_key,
    )
    if not isinstance(state, dict):
        return normalized

    for key, value in normalized.items():
        if key in trend_universe or key == retired_positions_key:
            continue
        if isinstance(value, dict):
            current = state.get(key, {})
            merged = value.copy()
            if isinstance(current, dict):
                merged.update(current)
            normalized[key] = merged
        else:
            normalized[key] = state.get(key, value)

    existing_retired = state.get(retired_positions_key, {})
    retired_positions = {}

    for symbol in trend_universe:
        merged_source = {}
        if isinstance(existing_retired, dict) and is_trend_symbol_state(existing_retired.get(symbol)):
            merged_source.update(existing_retired.get(symbol, {}))
        if is_trend_symbol_state(state.get(symbol)):
            merged_source.update(state.get(symbol, {}))
        normalized[symbol] = normalize_symbol_state(merged_source)

    if isinstance(existing_retired, dict):
        for symbol, payload in existing_retired.items():
            if symbol in trend_universe or not is_trend_symbol_state(payload):
                continue
            merged = normalize_symbol_state(payload)
            if has_active_position(merged):
                retired_positions[symbol] = {
                    **merged,
                    "base_asset": str(payload.get("base_asset") or infer_base_asset(symbol)),
                }

    for symbol, payload in state.items():
        if (
            symbol in normalized
            or symbol == retired_positions_key
            or not isinstance(symbol, str)
            or not symbol.endswith("USDT")
            or not is_trend_symbol_state(payload)
        ):
            continue
        merged = normalize_symbol_state(payload)
        if has_active_position(merged):
            retired_positions[symbol] = {
                **merged,
                "base_asset": str(payload.get("base_asset") or infer_base_asset(symbol)),
            }

    normalized[retired_positions_key] = retired_positions
    return normalized


def get_runtime_trend_universe(state, *, trend_universe, retired_positions_key):
    runtime = {symbol: meta.copy() for symbol, meta in trend_universe.items()}
    retired_positions = state.get(retired_positions_key, {})
    if isinstance(retired_positions, dict):
        for symbol, payload in retired_positions.items():
            if symbol in runtime:
                continue
            runtime[symbol] = {
                "base_asset": str(payload.get("base_asset") or infer_base_asset(symbol)),
                "retired": True,
            }
    return runtime


def get_symbol_trade_state(state, symbol, *, trend_universe, retired_positions_key):
    if symbol in trend_universe:
        return normalize_symbol_state(state.get(symbol, {}))
    retired_positions = state.get(retired_positions_key, {})
    if isinstance(retired_positions, dict):
        return normalize_symbol_state(retired_positions.get(symbol, {}))
    return default_trend_symbol_state()


def set_symbol_trade_state(state, symbol, symbol_state, *, trend_universe, retired_positions_key):
    normalized_symbol_state = normalize_symbol_state(symbol_state)
    retired_positions = state.setdefault(retired_positions_key, {})
    if symbol in trend_universe:
        state[symbol] = normalized_symbol_state
        if isinstance(retired_positions, dict):
            retired_positions.pop(symbol, None)
        return

    if not isinstance(retired_positions, dict):
        retired_positions = {}
        state[retired_positions_key] = retired_positions

    if has_active_position(normalized_symbol_state):
        existing = retired_positions.get(symbol, {})
        retired_positions[symbol] = {
            **normalized_symbol_state,
            "base_asset": str(existing.get("base_asset") or infer_base_asset(symbol)),
        }
    else:
        retired_positions.pop(symbol, None)


def should_skip_duplicate_trend_action(state, symbol, action, action_date, *, action_history_key):
    history = state.get(action_history_key, {})
    if not isinstance(history, dict):
        return False
    last_action = history.get(symbol, {})
    return (
        isinstance(last_action, dict)
        and str(last_action.get("action", "")) == str(action)
        and str(last_action.get("date", "")) == str(action_date)
    )


def record_trend_action(state, symbol, action, action_date, *, action_history_key):
    history = state.setdefault(action_history_key, {})
    if not isinstance(history, dict):
        history = {}
        state[action_history_key] = history
    history[symbol] = {"action": str(action), "date": str(action_date)}
