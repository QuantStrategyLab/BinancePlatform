import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from quant_platform_kit.common.runtime_reports import build_runtime_report_base


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
    report = build_runtime_report_base(
        platform="binance",
        deploy_target=os.getenv("LOG_DEPLOY_TARGET", "vps"),
        service_name=os.getenv("SERVICE_NAME", "binance-platform"),
        strategy_profile=str(runtime.strategy_profile or os.getenv("STRATEGY_PROFILE", "crypto_leader_rotation")),
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
    payload = {
        "token": str(runtime.tg_token),
        "chat_id": str(runtime.tg_chat_id),
        "text": str(text),
        "run_id": str(runtime.run_id),
        "dry_run": bool(runtime.dry_run),
    }
    report["notifications"].append(payload)
    if runtime.dry_run:
        record_side_effect(runtime, report, effect_type="notify", target="telegram", payload=payload, executed=False)
        return
    if runtime.notifier is None:
        raise RuntimeError("runtime.notifier is not configured")
    runtime.notifier(**payload)
    record_side_effect(runtime, report, effect_type="notify", target="telegram", payload=payload, executed=True)


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


def runtime_call_client(runtime, report, *, method_name, payload, effect_type):
    if runtime.dry_run:
        record_side_effect(
            runtime,
            report,
            effect_type=effect_type,
            target=method_name,
            payload=dict(payload),
            executed=False,
        )
        return {"status": "suppressed", "method": method_name, "payload": dict(payload)}
    if runtime.client is None:
        raise RuntimeError("runtime.client is not configured")
    response = getattr(runtime.client, method_name)(**payload)
    record_side_effect(
        runtime,
        report,
        effect_type=effect_type,
        target=method_name,
        payload=dict(payload),
        executed=True,
    )
    return response
