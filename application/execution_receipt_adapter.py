"""Privacy-safe execution-receipt observations for Binance runtime reports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from quant_platform_kit.common.execution_receipts import (
    attach_runtime_execution_receipt,
    resolve_execution_receipt_fact,
)


_OBSERVATION_KEY = "execution_receipt_observation"
_ORDER_EVENTS_KEY = "execution_order_events"
_ORDER_EVENT_SCHEMA_VERSION = "binance.order_response_event.v1"
_COUNTER_FIELDS = (
    "submission_attempted_count",
    "broker_acknowledged_count",
    "partially_filled_count",
    "filled_count",
    "transport_uncertain_count",
    "failed_count",
)
_FILLED_STATUSES = frozenset({"FILLED"})
_PARTIAL_FILL_STATUSES = frozenset({"PARTIALLY_FILLED"})
_FAILED_STATUSES = frozenset({"CANCELED", "EXPIRED", "REJECTED"})


def record_order_submission_attempt(report: dict[str, Any]) -> None:
    """Record a broker-facing attempt without retaining an order payload."""

    _observation(report)["submission_attempted_count"] += 1


def record_order_response(
    report: dict[str, Any],
    response: object,
    *,
    client_order_id: object = None,
    ordered_quantity: object = None,
    event_source: object = None,
) -> None:
    """Record an explicit Binance status and a bounded order event when bound."""

    observation = _observation(report)
    status = str(response.get("status") or "").strip().upper() if isinstance(response, Mapping) else ""
    appended = _append_order_event(
        report,
        response,
        client_order_id=client_order_id,
        ordered_quantity=ordered_quantity,
        event_source=event_source,
        status=status,
    )
    if appended is False:
        return
    if status in _FILLED_STATUSES:
        observation["filled_count"] += 1
    elif status in _PARTIAL_FILL_STATUSES:
        observation["partially_filled_count"] += 1
    elif status in _FAILED_STATUSES:
        observation["failed_count"] += 1
    elif status:
        observation["broker_acknowledged_count"] += 1


def record_order_transport_uncertainty(report: dict[str, Any]) -> None:
    """Remember that a failed request may still have reached the broker."""

    _observation(report)["transport_uncertain_count"] += 1


def record_order_failure(report: dict[str, Any]) -> None:
    """Record final retry exhaustion without exposing transport details."""

    _observation(report)["failed_count"] += 1


def attach_execution_receipt_from_report(report: dict[str, Any]) -> bool:
    """Attach a receipt from bounded observations, returning false if unattested.

    This deliberately does not make report persistence fail for an older
    runtime target that lacks a release identity; the control plane will show
    such a report as evidence-missing instead.
    """

    observation = _observation(report)
    submissions = observation["submission_attempted_count"]
    acknowledged = observation["broker_acknowledged_count"]
    partial_fills = observation["partially_filled_count"]
    fills = observation["filled_count"]
    failures = observation["failed_count"]
    transport_uncertain = observation["transport_uncertain_count"]
    report_error = str(report.get("status") or "").strip().lower() == "error"
    reconciliation_required = bool(
        transport_uncertain
        or (report_error and (acknowledged or partial_fills or fills))
    )
    outcome, confirmation = resolve_execution_receipt_fact(
        dry_run=bool(report.get("dry_run")),
        submission_attempted=bool(submissions),
        broker_acknowledged=bool(acknowledged),
        partially_filled=bool(partial_fills),
        filled=bool(fills) and fills == submissions and not acknowledged and not partial_fills,
        reconciliation_required=reconciliation_required,
        risk_blocked=not submissions and bool(report.get("execution_blocked_reason") or report.get("circuit_breaker_triggered")),
        failed=bool(failures) or (report_error and not reconciliation_required),
    )
    try:
        attach_runtime_execution_receipt(
            report,
            outcome=outcome,
            broker_confirmation=confirmation,
        )
    except ValueError:
        return False
    return True


def _observation(report: dict[str, Any]) -> dict[str, int]:
    raw = report.get(_OBSERVATION_KEY)
    observation = dict(raw) if isinstance(raw, Mapping) else {}
    for field in _COUNTER_FIELDS:
        value = observation.get(field, 0)
        observation[field] = int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    report[_OBSERVATION_KEY] = observation
    return observation


def _append_order_event(
    report: dict[str, Any],
    response: object,
    *,
    client_order_id: object,
    ordered_quantity: object,
    event_source: object,
    status: str,
) -> bool | None:
    if not isinstance(response, Mapping):
        return None
    stable_client_order_id = str(client_order_id or "").strip()
    source = str(event_source or "").strip()
    if not stable_client_order_id or not source or not status:
        return None

    response_ordered_quantity = response.get("origQty")
    if response_ordered_quantity in (None, ""):
        response_ordered_quantity = ordered_quantity
    payload = {
        "schema_version": _ORDER_EVENT_SCHEMA_VERSION,
        "client_order_id": stable_client_order_id,
        "venue_order_id": _optional_text(response.get("orderId")),
        "ordered_quantity": _quantity_text(response_ordered_quantity),
        "cumulative_filled_quantity": _quantity_text(response.get("executedQty")),
        "status": status,
        "event_source": source,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    event = {
        "event_id": f"binance-order-event.{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        **payload,
    }
    raw_events = report.get(_ORDER_EVENTS_KEY)
    events = list(raw_events) if isinstance(raw_events, list) else []
    if any(isinstance(existing, Mapping) and existing.get("event_id") == event["event_id"] for existing in events):
        return False
    events.append(event)
    report[_ORDER_EVENTS_KEY] = events
    return True


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _quantity_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        quantity = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not quantity.is_finite() or quantity < 0:
        return None
    if not quantity:
        return "0"
    return format(quantity.normalize(), "f")
