import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests
from quant_platform_kit.common.runtime_reports import build_runtime_report_base
from application.execution_receipt_adapter import (
    record_order_failure,
    record_order_response,
    record_order_submission_attempt,
    record_order_transport_uncertainty,
)

# Binance rate limits (public API: 1200 weight/min, order placement: 50 orders/10s)
_BINANCE_ORDER_RATE_LIMIT_INTERVAL_SEC = 0.25  # max ~4 orders/sec
_BINANCE_ORDER_TRANSPORT_UNCERTAINTY_CODES = frozenset({-1001, -1006, -1007})
_BINANCE_ORDER_NOT_FOUND_CODE = -2013
_BINANCE_ORDER_FILLED_STATUS = "FILLED"
_BINANCE_ORDER_FAILED_STATUSES = frozenset({"CANCELED", "EXPIRED", "REJECTED"})
_ORDER_SUBMISSION_STATE_KEY = "order_submission"
_ORDER_SUBMISSION_RESERVED = "RESERVED"
_ORDER_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
_ORDER_SUBMISSION_TERMINAL = "TERMINAL"
_ORDER_CLIENT_ID_PREFIX = "QSL_"
_LAST_API_CALL_TS: float = 0.0
RUNTIME_EVIDENCE_CONTRACT_VERSION = "qsl.runtime_evidence_aggregate.v1"
RECONCILIATION_STATUSES = frozenset({"MISSING", "MATCHED", "MISMATCHED"})
_RUNTIME_EVIDENCE_FORBIDDEN_FIELDS = frozenset(
    {
        "api_key",
        "api_secret",
        "authorization",
        "balances",
        "credentials",
        "headers",
        "orders",
        "positions",
        "provider_rows",
        "secret",
        "token",
    }
)


class ExecutionIntegrityError(RuntimeError):
    """Execution integrity is uncertain and the cycle must stop."""


class StatePersistenceError(ExecutionIntegrityError):
    """State persistence did not complete durably."""


class OrderReconciliationError(ExecutionIntegrityError):
    """An uncertain order could not be reconciled safely."""


class ClientCallError(RuntimeError):
    """A client call failed without exposing provider details."""


def _rate_limit_pause():
    """Enforce minimum interval between Binance API calls."""
    global _LAST_API_CALL_TS
    elapsed = time.monotonic() - _LAST_API_CALL_TS
    if elapsed < _BINANCE_ORDER_RATE_LIMIT_INTERVAL_SEC:
        time.sleep(_BINANCE_ORDER_RATE_LIMIT_INTERVAL_SEC - elapsed)
    _LAST_API_CALL_TS = time.monotonic()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value.strip()))


def _is_git_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value.strip()))


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _append_missing_fields(payload: Mapping[str, Any], fields: tuple[str, ...], errors: list[str], label: str) -> None:
    for field_name in fields:
        if field_name not in payload:
            errors.append(f"{label} missing field: {field_name}")


def _append_forbidden_field_errors(value: Any, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for field, nested_value in value.items():
            if str(field).lower() in _RUNTIME_EVIDENCE_FORBIDDEN_FIELDS:
                errors.append(f"runtime_evidence_aggregate contains forbidden field: {field}")
            _append_forbidden_field_errors(nested_value, errors)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _append_forbidden_field_errors(item, errors)


def _validate_release_identity(identity: Any, errors: list[str]) -> None:
    label = "runtime_evidence_aggregate release_identity"
    if not isinstance(identity, Mapping):
        errors.append(f"{label} must be an object")
        return
    _append_missing_fields(
        identity,
        (
            "strategy_profile",
            "mode",
            "source_revision",
            "input_timestamp",
            "artifact_contract",
            "artifact_version",
            "artifacts",
        ),
        errors,
        label,
    )
    for field_name in ("strategy_profile", "mode", "artifact_contract", "artifact_version"):
        if not isinstance(identity.get(field_name), str) or not identity[field_name].strip():
            errors.append(f"{label} {field_name} must be a non-empty string")
    if not _is_git_revision(identity.get("source_revision")):
        errors.append(f"{label} source_revision must be a 40-character lowercase git SHA")
    if not _is_utc_timestamp(identity.get("input_timestamp")):
        errors.append(f"{label} input_timestamp must be a UTC timestamp")
    artifacts = identity.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        errors.append(f"{label} artifacts must be a non-empty object")
        return
    for artifact_name, artifact in artifacts.items():
        if not isinstance(artifact_name, str) or not artifact_name.strip() or not isinstance(artifact, Mapping):
            errors.append(f"{label} artifacts must contain named objects")
            continue
        if not _is_sha256(artifact.get("sha256")):
            errors.append(f"{label} artifacts.{artifact_name}.sha256 must be a SHA-256 digest")


def _validate_reconciliation(reconciliation: Any, errors: list[str]) -> None:
    label = "runtime_evidence_aggregate reconciliation"
    if not isinstance(reconciliation, Mapping):
        errors.append(f"{label} must be an object")
        return
    status = reconciliation.get("status")
    if status not in RECONCILIATION_STATUSES:
        errors.append(f"{label} status must be one of MISSING, MATCHED, MISMATCHED")
        return
    if status == "MATCHED":
        for field in ("durable_receipt_sha256", "identity_sha256"):
            if not _is_sha256(reconciliation.get(field)):
                errors.append(f"{label}.MATCHED requires {field}")
        errors.append(f"{label}.MATCHED is not valid for static acceptance")
    elif status == "MISMATCHED":
        for field in ("durable_receipt_sha256", "identity_sha256", "observed_identity_sha256"):
            if not _is_sha256(reconciliation.get(field)):
                errors.append(f"{label}.MISMATCHED requires {field}")
        if reconciliation.get("identity_sha256") == reconciliation.get("observed_identity_sha256"):
            errors.append(f"{label}.MISMATCHED identity digests must differ")


def validate_runtime_evidence_aggregate(aggregate: Any) -> dict[str, Any]:
    """Validate a redacted, static-only runtime evidence aggregate."""
    errors: list[str] = []
    label = "runtime_evidence_aggregate"
    if not isinstance(aggregate, Mapping):
        return {"ok": False, "errors": [f"{label} must be an object"]}

    _append_forbidden_field_errors(aggregate, errors)
    _append_missing_fields(
        aggregate,
        (
            "contract_version",
            "release_identity",
            "risk_engine",
            "effective_exposure_cap",
            "stop_breaker_evaluation",
            "reconciliation",
            "static_validation_only",
            "execution_permitted",
            "verified_active",
            "fills_verified",
            "capital_use_verified",
        ),
        errors,
        label,
    )
    if aggregate.get("contract_version") != RUNTIME_EVIDENCE_CONTRACT_VERSION:
        errors.append(f"{label} contract_version must be {RUNTIME_EVIDENCE_CONTRACT_VERSION}")
    _validate_release_identity(aggregate.get("release_identity"), errors)

    risk_engine = aggregate.get("risk_engine")
    if not isinstance(risk_engine, Mapping):
        errors.append(f"{label} risk_engine must be an object")
    else:
        if risk_engine.get("outcome") != "APPROVE":
            errors.append(f"{label} risk_engine.outcome must be APPROVE")
        if not isinstance(risk_engine.get("policy_version"), str) or not risk_engine["policy_version"].strip():
            errors.append(f"{label} risk_engine.policy_version must be a non-empty string")

    cap = aggregate.get("effective_exposure_cap")
    if not isinstance(cap, Mapping):
        errors.append(f"{label} effective_exposure_cap must be an object")
    else:
        value = cap.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
            errors.append(f"{label} effective_exposure_cap.value must be in (0, 1]")
        for field in ("mandate_version", "source"):
            if not isinstance(cap.get(field), str) or not cap[field].strip():
                errors.append(f"{label} effective_exposure_cap.{field} must be a non-empty string")

    stop_breaker = aggregate.get("stop_breaker_evaluation")
    if not isinstance(stop_breaker, Mapping):
        errors.append(f"{label} stop_breaker_evaluation must be an object")
    else:
        if stop_breaker.get("stop_evaluated") is not True:
            errors.append(f"{label} stop_breaker_evaluation.stop_evaluated must be true")
        if stop_breaker.get("breaker_evaluated") is not True:
            errors.append(f"{label} stop_breaker_evaluation.breaker_evaluated must be true")
        if stop_breaker.get("outcome") != "CLEAR":
            errors.append(f"{label} stop_breaker_evaluation.outcome must be CLEAR")
        if not isinstance(stop_breaker.get("policy_version"), str) or not stop_breaker["policy_version"].strip():
            errors.append(f"{label} stop_breaker_evaluation.policy_version must be a non-empty string")

    _validate_reconciliation(aggregate.get("reconciliation"), errors)
    for field in ("static_validation_only", "execution_permitted", "verified_active", "fills_verified", "capital_use_verified"):
        expected = field == "static_validation_only"
        if aggregate.get(field) is not expected:
            errors.append(f"{label} {field} must be {str(expected).lower()} for static acceptance")
    return {"ok": not errors, "errors": errors}


def build_runtime_evidence_aggregate(
    *,
    release_identity: Mapping[str, Any],
    risk_engine: Mapping[str, Any],
    effective_exposure_cap: Mapping[str, Any],
    stop_breaker_evaluation: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a fail-closed aggregate that cannot claim runtime activity."""
    aggregate = {
        "contract_version": RUNTIME_EVIDENCE_CONTRACT_VERSION,
        "release_identity": dict(release_identity),
        "risk_engine": dict(risk_engine),
        "effective_exposure_cap": dict(effective_exposure_cap),
        "stop_breaker_evaluation": dict(stop_breaker_evaluation),
        "reconciliation": dict(reconciliation),
        "static_validation_only": True,
        "execution_permitted": False,
        "verified_active": False,
        "fills_verified": False,
        "capital_use_verified": False,
    }
    validation = validate_runtime_evidence_aggregate(aggregate)
    if not validation["ok"]:
        raise ValueError("Runtime evidence aggregate validation failed: " + "; ".join(validation["errors"]))
    return aggregate


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
    standard_execution_permitted: bool = True
    trend_pool_payload: Optional[dict[str, Any]] = None
    btc_market_snapshot: Optional[dict[str, Any]] = None
    trend_indicator_snapshots: Optional[dict[str, Any]] = None
    research_cycle_settings: Any = None
    print_traceback: bool = True
    order_sequence: int = 0
    trade_state: Optional[dict[str, Any]] = None
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
        "standard_execution_permitted": bool(getattr(runtime, "standard_execution_permitted", True)),
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
    if runtime.dry_run or not getattr(runtime, "standard_execution_permitted", True):
        record_side_effect(runtime, report, effect_type="state_write", target="firestore", payload=payload, executed=False)
        return
    if runtime.state_writer is None:
        raise StatePersistenceError("state_persistence_failed")
    try:
        persisted = runtime.state_writer(state)
    except Exception:
        raise StatePersistenceError("state_persistence_failed") from None
    if persisted is not True:
        raise StatePersistenceError("state_persistence_failed")
    runtime.trade_state = state
    record_side_effect(runtime, report, effect_type="state_write", target="firestore", payload=payload, executed=True)


def _ensure_order_logical_identity(runtime, method_name, payload):
    order_payload = dict(payload)
    supplied_identity = str(order_payload.get("newClientOrderId") or "").strip()
    logical_order = (
        {"supplied_client_order_id": supplied_identity}
        if supplied_identity
        else {
            "run_id": str(runtime.run_id),
            "method_name": str(method_name),
            "payload": order_payload,
        }
    )
    encoded = json.dumps(logical_order, sort_keys=True, separators=(",", ":"), default=str)
    identity_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    order_payload["newClientOrderId"] = _client_order_id_from_digest(identity_sha256)
    return order_payload, identity_sha256


def _client_order_id_from_digest(identity_sha256):
    if not _is_sha256(identity_sha256):
        raise StatePersistenceError("submission_state_invalid") from None
    return f"{_ORDER_CLIENT_ID_PREFIX}{identity_sha256[:28]}"


def _load_order_submission_state(runtime):
    state = runtime.trade_state
    if state is None:
        if runtime.state_loader is None:
            raise StatePersistenceError("state_persistence_unavailable") from None
        try:
            state = runtime.state_loader(normalize=False)
        except Exception:
            raise StatePersistenceError("state_persistence_failed") from None
    if not isinstance(state, dict):
        raise StatePersistenceError("submission_state_invalid") from None

    record = state.get(_ORDER_SUBMISSION_STATE_KEY, {"state": _ORDER_SUBMISSION_RESERVED})
    if not isinstance(record, Mapping):
        raise StatePersistenceError("submission_state_invalid") from None
    record = dict(record)
    status = record.get("state")
    if status in {_ORDER_SUBMISSION_RESERVED, _ORDER_SUBMISSION_TERMINAL}:
        if set(record) != {"state"}:
            raise StatePersistenceError("submission_state_invalid") from None
    elif status == _ORDER_SUBMISSION_UNKNOWN:
        if set(record) != {"state", "identity_sha256", "symbol"}:
            raise StatePersistenceError("submission_state_invalid") from None
        if not _is_sha256(record.get("identity_sha256")):
            raise StatePersistenceError("submission_state_invalid") from None
        if not re.fullmatch(r"[A-Z0-9]{3,30}", str(record.get("symbol") or "")):
            raise StatePersistenceError("submission_state_invalid") from None
    else:
        raise StatePersistenceError("submission_state_invalid") from None
    runtime.trade_state = state
    return state, record


def _persist_order_submission_state(runtime, state, record):
    if runtime.state_writer is None:
        raise StatePersistenceError("state_persistence_unavailable") from None
    updated_state = dict(state)
    updated_state[_ORDER_SUBMISSION_STATE_KEY] = dict(record)
    try:
        persisted = runtime.state_writer(updated_state)
    except Exception:
        raise StatePersistenceError("state_persistence_failed") from None
    if persisted is not True:
        raise StatePersistenceError("state_persistence_failed") from None
    state[_ORDER_SUBMISSION_STATE_KEY] = dict(record)
    runtime.trade_state = state


def _complete_order_response(runtime, report, state, response):
    record_order_response(report, response)
    status = str(response.get("status") or "").strip().upper() if isinstance(response, Mapping) else ""
    if status == _BINANCE_ORDER_FILLED_STATUS or status in _BINANCE_ORDER_FAILED_STATUSES:
        _persist_order_submission_state(
            runtime,
            state,
            {"state": _ORDER_SUBMISSION_TERMINAL},
        )
    return _require_filled_order_response(response)


def _binance_error_code(exc):
    try:
        return int(getattr(exc, "code"))
    except (AttributeError, TypeError, ValueError):
        return None


def _is_order_transport_uncertainty(exc):
    transport_exceptions = (TimeoutError, ConnectionError)
    if hasattr(requests, "exceptions"):
        transport_exceptions += (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
    return isinstance(exc, transport_exceptions) or (
        _binance_error_code(exc) in _BINANCE_ORDER_TRANSPORT_UNCERTAINTY_CODES
    )


def _reconcile_uncertain_order(client, symbol, identity_sha256):
    if not symbol or not identity_sha256:
        raise OrderReconciliationError("order_reconciliation_uncertain") from None
    try:
        response = client.get_order(
            symbol=symbol,
            origClientOrderId=_client_order_id_from_digest(identity_sha256),
        )
    except Exception as exc:
        if _binance_error_code(exc) == _BINANCE_ORDER_NOT_FOUND_CODE:
            raise OrderReconciliationError("order_reconciliation_uncertain") from None
        raise OrderReconciliationError("order_reconciliation_uncertain") from None
    if not isinstance(response, Mapping):
        raise OrderReconciliationError("order_reconciliation_uncertain") from None
    return response


def _require_filled_order_response(response):
    if not isinstance(response, Mapping):
        raise OrderReconciliationError("order_reconciliation_uncertain") from None
    status = str(response.get("status") or "").strip().upper()
    if status == _BINANCE_ORDER_FILLED_STATUS:
        return response
    if status in _BINANCE_ORDER_FAILED_STATUSES:
        raise ClientCallError("order_submission_failed") from None
    raise OrderReconciliationError("order_reconciliation_uncertain") from None


def runtime_call_client(runtime, report, *, method_name, payload, effect_type,
                        max_retries: int = 3, retry_base_sec: float = 1.0):
    if runtime.dry_run or not getattr(runtime, "standard_execution_permitted", True):
        record_side_effect(
            runtime, report, effect_type=effect_type,
            target=method_name, payload=dict(payload), executed=False,
        )
        return {"status": "suppressed", "method": method_name, "payload": dict(payload)}
    if runtime.client is None:
        raise RuntimeError("runtime.client is not configured")

    is_order_call = str(effect_type or "").startswith("order_")
    client_payload = dict(payload)
    trade_state = None
    identity_sha256 = None
    if is_order_call:
        trade_state, submission_record = _load_order_submission_state(runtime)
        submission_status = submission_record["state"]
        if submission_status == _ORDER_SUBMISSION_UNKNOWN:
            reconciled_response = _reconcile_uncertain_order(
                runtime.client,
                submission_record["symbol"],
                submission_record["identity_sha256"],
            )
            return _complete_order_response(runtime, report, trade_state, reconciled_response)
        if submission_status == _ORDER_SUBMISSION_TERMINAL:
            _persist_order_submission_state(
                runtime,
                trade_state,
                {"state": _ORDER_SUBMISSION_RESERVED},
            )
        client_payload, identity_sha256 = _ensure_order_logical_identity(runtime, method_name, payload)
        symbol = str(client_payload.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,30}", symbol):
            raise StatePersistenceError("submission_state_invalid") from None
        _persist_order_submission_state(
            runtime,
            trade_state,
            {
                "state": _ORDER_SUBMISSION_UNKNOWN,
                "identity_sha256": identity_sha256,
                "symbol": symbol,
            },
        )
        record_order_submission_attempt(report)
    _rate_limit_pause()
    retries_used = max_retries
    for attempt in range(max_retries + 1):
        try:
            response = getattr(runtime.client, method_name)(**client_payload)
        except Exception as exc:
            if is_order_call:
                if not _is_order_transport_uncertainty(exc):
                    retries_used = attempt
                    break
                record_order_transport_uncertainty(report)
                try:
                    reconciled_response = _reconcile_uncertain_order(
                        runtime.client,
                        str(client_payload.get("symbol") or "").strip().upper(),
                        identity_sha256,
                    )
                except OrderReconciliationError:
                    record_side_effect(
                        runtime,
                        report,
                        effect_type=f"{effect_type}_failed",
                        target=method_name,
                        payload={
                            "payload": dict(client_payload),
                            "reason": "order_reconciliation_uncertain",
                            "retries": attempt,
                        },
                        executed=False,
                    )
                    record_order_failure(report)
                    raise
                return _complete_order_response(runtime, report, trade_state, reconciled_response)
            if attempt < max_retries:
                delay = retry_base_sec * (2 ** attempt)
                time.sleep(delay)
        else:
            record_side_effect(
                runtime, report, effect_type=effect_type,
                target=method_name, payload=dict(client_payload), executed=True,
            )
            if is_order_call:
                return _complete_order_response(runtime, report, trade_state, response)
            return response
    # All retries exhausted — log and raise
    record_side_effect(
        runtime, report,
        effect_type=f"{effect_type}_failed",
        target=method_name,
        payload={"payload": dict(client_payload), "retries": retries_used},
        executed=False,
    )
    if is_order_call:
        record_order_failure(report)
        _persist_order_submission_state(
            runtime,
            trade_state,
            {"state": _ORDER_SUBMISSION_TERMINAL},
        )
    reason = "order_submission_failed" if is_order_call else "client_call_failed"
    raise ClientCallError(reason) from None
