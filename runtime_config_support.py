from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from notify_i18n_support import build_strategy_display_name, build_translator, get_notify_lang
from runtime_support import ExecutionRuntime
from strategy_registry import (
    BINANCE_PLATFORM,
    resolve_strategy_definition,
    resolve_strategy_metadata,
)


def get_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def get_env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def get_env_csv(name: str, default_values: list[str] | tuple[str, ...]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return list(default_values)
    return [item.strip() for item in str(raw).split(",") if item.strip()]


@dataclass(frozen=True)
class CycleExecutionSettings:
    btc_status_report_interval_hours: int
    allow_new_trend_entries_on_degraded: bool
    strategy_profile: str
    strategy_display_name: str
    strategy_display_name_localized: str
    strategy_domain: str


def load_cycle_execution_settings() -> CycleExecutionSettings:
    notify_lang = get_notify_lang()
    strategy_definition = resolve_strategy_definition(
        os.getenv("STRATEGY_PROFILE"),
        platform_id=BINANCE_PLATFORM,
    )
    strategy_metadata = resolve_strategy_metadata(
        strategy_definition.profile,
        platform_id=BINANCE_PLATFORM,
    )
    strategy_display_name_localized = build_strategy_display_name(build_translator(notify_lang))(
        strategy_definition.profile,
        fallback_name=strategy_metadata.display_name,
    )
    return CycleExecutionSettings(
        btc_status_report_interval_hours=max(1, min(24, get_env_int("BTC_STATUS_REPORT_INTERVAL_HOURS", 24))),
        allow_new_trend_entries_on_degraded=get_env_bool(
            "TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED",
            False,
        ),
        strategy_profile=strategy_definition.profile,
        strategy_display_name=strategy_metadata.display_name,
        strategy_display_name_localized=strategy_display_name_localized,
        strategy_domain=strategy_definition.domain,
    )


def build_live_runtime(
    *,
    now_utc: datetime | None = None,
    state_loader: Callable[..., Any] | None = None,
    state_writer: Callable[[dict[str, Any]], Any] | None = None,
    notifier: Callable[..., Any] | None = None,
) -> ExecutionRuntime:
    runtime_now = now_utc or datetime.now(timezone.utc)
    cycle_settings = load_cycle_execution_settings()
    return ExecutionRuntime(
        dry_run=False,
        now_utc=runtime_now,
        strategy_profile=cycle_settings.strategy_profile,
        strategy_domain=cycle_settings.strategy_domain,
        strategy_display_name=cycle_settings.strategy_display_name,
        strategy_display_name_localized=cycle_settings.strategy_display_name_localized,
        api_key=os.getenv("BINANCE_API_KEY", ""),
        api_secret=os.getenv("BINANCE_API_SECRET", ""),
        tg_token=os.getenv("TG_TOKEN", ""),
        tg_chat_id=os.getenv("GLOBAL_TELEGRAM_CHAT_ID", ""),
        state_loader=state_loader,
        state_writer=state_writer,
        notifier=notifier,
    )
