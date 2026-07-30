import hashlib
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from quant_platform_kit.common.runtime_reports import build_runtime_report_base

# Binance rate limits (public API: 1200 weight/min, order placement: 50 orders/10s)
_BINANCE_ORDER_RATE_LIMIT_INTERVAL_SEC = 0.25  # max ~4 orders/sec
_LAST_API_CALL_TS: float = 0.0


def _rate_limit_pause():
    """Enforce minimum interval between Binance API calls."""
    global _LAST_API_CALL_TS
    elapsed = time.monotonic() - _LAST_API_CALL_TS
    if elapsed < _BINANCE_ORDER_RATE_LIMIT_INTERVAL_SEC:
        time.sleep(_BINANCE_ORDER_RATE_LIMIT_INTERVAL_SEC - elapsed)
    _LAST_API_CALL_TS = time.monotonic()


@dataclass
class ExecutionRuntime:
    dry_run: bool = False
    run_id: str = ""
    now_utc: Optional[datetime] = None
    strategy_profile: str = ""
    strategy_domain: str = ""
    strategy_display_name: str = ""
    strategy_display_name_localized: str = ""
    client: Any = None
    api_key: str = ""
    api_secret: str = ""
    tg_token: str = ""
    tg_chat_id: str = ""
    state_loader: Optional[Callable[..., Any]] = None
    state_writer: Optional[Callable[[dict[str, Any]], Any]] = None
    notifier: Optional[Callable[..., Any]] = None
    runtime_target: Any = None
    trend_pool_payload: Optional[dict[str, Any]] = None
    btc_market_snapshot: Optional[dict[str, Any]] = None
    trend_indicator_snapshots: Optional[dict[str, Any]] = None
    print_traceback: bool = True
    order_sequence: int = 0
    side_effect_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if self.now_utc is None:
            self.now_utc = datetime.now(timezone.utc)
        if not self.run_id:
            self.run_id = self.now_utc.strftime("%Y%m%dT%H%M%SZ")


def build_execution_report(runtime):
    runtime_target = getattr(runtime, "runtime_target", None)
    runtime_service_name = (
        getattr(runtime_target, "service_name", None)
        or os.getenv("SERVICE_NAME")
        or "binance-platform"
    )
    report = build_runtime_report_base(
        platform="binance",
        deploy_target=os.getenv("LOG_DEPLOY_TARGET", "vps"),
        service_name=runtime_service_name,
        strategy_profile=str(runtime.strategy_profile or os.getenv("STRATEGY_PROFILE", "crypto_live_pool_rotation")),
        strategy_domain=str(runtime.strategy_domain or os.getenv("STRATEGY_DOMAIN", "crypto")),
        run_id=str(runtime.run_id),
        run_source="github_actions" if os.getenv("GITHUB_RUN_ID") or os.getenv("GITHUB_ACTIONS") else "runtime",
        dry_run=bool(runtime.dry_run),
        started_at=runtime.now_utc,
        status="ok",
    )
    report.update({
        "status": "ok",
        "run_id": str(runtime.run_id),
        "dry_run": bool(runtime.dry_run),
        "selected_symbols": {
            "active_trend_pool": [],
            "selected_candidates": [],
        },
        "buy_sell_intents": [],
        "btc_dca_intents": [],
        "redemption_subscription_intents": [],
        "notifications": [],
        "state_write_intents": [],
        "side_effect_summary": {
            "executed_call_count": 0,
            "suppressed_call_count": 0,
        },
        "gating_summary": {},
        "gating_events": [],
        "error_summary": {
            "errors": [],
        },
        "log_lines": [],
        "total_equity_usdt": None,
        "trend_equity_usdt": None,
        "circuit_breaker_triggered": False,
        "degraded_mode_level": None,
        "upstream_pool_symbols": [],
        "summary": {
            "strategy_display_name": str(runtime.strategy_display_name or ""),
            "strategy_display_name_localized": str(runtime.strategy_display_name_localized or ""),
        },
    })
    if runtime_target is not None:
        report["runtime_target"] = runtime_target.to_dict()
    return report


def append_report_error(report, message, *, stage="runtime"):
    report["error_summary"]["errors"].append({"stage": str(stage), "message": str(message)})


def record_gating_event(report, *, gate, category, symbol=None, detail=None):
    gate_name = str(gate)
    category_name = str(category)
    summary = report.setdefault("gating_summary", {})
    events = report.setdefault("gating_events", [])
    summary[gate_name] = int(summary.get(gate_name, 0) or 0) + 1

    event = {
        "gate": gate_name,
        "category": category_name,
    }
    if symbol:
        event["symbol"] = str(symbol)
    if detail is not None:
        event["detail"] = detail
    events.append(event)


def record_side_effect(runtime, report, *, effect_type, target, payload, executed):
    entry = {
        "effect_type": str(effect_type),
        "target": str(target),
        "payload": payload,
        "executed": bool(executed),
    }
    runtime.side_effect_log.append(entry)
    summary_key = "executed_call_count" if executed else "suppressed_call_count"
    report["side_effect_summary"][summary_key] += 1


def next_order_id(runtime, prefix, symbol):
    runtime.order_sequence += 1
    safe_run_id = "".join(ch if ch.isalnum() else "_" for ch in str(runtime.run_id))[:24] or "run"
    return f"{prefix}_{symbol}_{safe_run_id}_{runtime.order_sequence:03d}"


def runtime_notify(runtime, report, text):
    message = str(text)
    safe_event = {
        "sink": "telegram",
        "compact_text_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "compact_text_length": len(message),
        "run_id": str(runtime.run_id),
        "dry_run": bool(runtime.dry_run),
    }
    if runtime.dry_run:
        safe_event.update(
            {
                "delivery_status": "suppressed",
                "transport_acknowledged": False,
            }
        )
        report["notifications"].append(safe_event)
        record_side_effect(
            runtime,
            report,
            effect_type="notify",
            target="telegram",
            payload=safe_event,
            executed=False,
        )
        return False
    if runtime.notifier is None:
        raise RuntimeError("runtime.notifier is not configured")
    receipt = runtime.notifier(
        token=str(runtime.tg_token),
        chat_id=str(runtime.tg_chat_id),
        text=message,
        run_id=str(runtime.run_id),
        dry_run=False,
    )
    if isinstance(receipt, Mapping):
        for key in (
            "sink",
            "delivery_status",
            "transport_acknowledged",
            "error_type",
            "compact_text_sha256",
            "compact_text_length",
        ):
            if key in receipt:
                safe_event[key] = receipt[key]
        acknowledged = receipt.get("transport_acknowledged") is True
    else:
        acknowledged = receipt is True
    safe_event.setdefault("delivery_status", "sent" if acknowledged else "failed")
    safe_event["transport_acknowledged"] = acknowledged
    report["notifications"].append(safe_event)
    delivery_events = [
        event
        for event in report["notifications"]
        if event.get("delivery_status") != "suppressed"
    ]
    report.setdefault("summary", {})["notification_delivery_summary"] = {
        "event_count": len(delivery_events),
        "sent_count": sum(
            event.get("transport_acknowledged") is True for event in delivery_events
        ),
        "failed_count": sum(
            event.get("transport_acknowledged") is not True for event in delivery_events
        ),
        "all_acknowledged": all(
            event.get("transport_acknowledged") is True for event in delivery_events
        ),
    }
    record_side_effect(
        runtime,
        report,
        effect_type="notify",
        target="telegram",
        payload=safe_event,
        executed=acknowledged,
    )
    return acknowledged


def finalize_notification_delivery(report):
    delivery_summary = report.get("summary", {}).get("notification_delivery_summary")
    if not isinstance(delivery_summary, dict) or delivery_summary.get("all_acknowledged") is not False:
        return
    errors = report.setdefault("error_summary", {}).setdefault("errors", [])
    if not any(error.get("stage") == "notification_delivery" for error in errors if isinstance(error, dict)):
        errors.append(
            {
                "stage": "notification_delivery",
                "message": "Telegram delivery was not acknowledged.",
            }
        )
    if report.get("status") == "ok":
        report["status"] = "error"


def runtime_set_trade_state(runtime, report, state, *, reason):
    payload = {"reason": str(reason)}
    report["state_write_intents"].append(payload)
    if runtime.dry_run:
        record_side_effect(runtime, report, effect_type="state_write", target="firestore", payload=payload, executed=False)
        return
    if runtime.state_writer is None:
        raise RuntimeError("runtime.state_writer is not configured")
    runtime.state_writer(state)
    record_side_effect(runtime, report, effect_type="state_write", target="firestore", payload=payload, executed=True)


def runtime_call_client(runtime, report, *, method_name, payload, effect_type,
                        max_retries: int = 3, retry_base_sec: float = 1.0):
    if runtime.dry_run:
        record_side_effect(
            runtime, report, effect_type=effect_type,
            target=method_name, payload=dict(payload), executed=False,
        )
        return {"status": "suppressed", "method": method_name, "payload": dict(payload)}
    if runtime.client is None:
        raise RuntimeError("runtime.client is not configured")

    _rate_limit_pause()
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = getattr(runtime.client, method_name)(**payload)
            record_side_effect(
                runtime, report, effect_type=effect_type,
                target=method_name, payload=dict(payload), executed=True,
            )
            return response
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                delay = retry_base_sec * (2 ** attempt)
                time.sleep(delay)
    # All retries exhausted — log and raise
    record_side_effect(
        runtime, report,
        effect_type=f"{effect_type}_failed",
        target=method_name,
        payload={"payload": dict(payload), "error": str(last_error), "retries": max_retries},
        executed=False,
    )
    raise RuntimeError(
        f"Binance API call {method_name} failed after {max_retries} retries"
    ) from last_error
