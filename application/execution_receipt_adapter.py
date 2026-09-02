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
_ORDER_EVENT_SCHEMA_VERSION = "binance.order_response_event.v2"
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
_FAILED_STATUSES = frozenset({"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"})
_TERMINAL_STATUSES = _FAILED_STATUSES | _FILLED_STATUSES


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
    reducer_state: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Record an explicit Binance status and a bounded order event when bound."""

    observation = _observation(report)
    status = str(response.get("status") or "").strip().upper() if isinstance(response, Mapping) else ""
    reduced = _reduce_order_response(
        response,
        client_order_id=client_order_id,
        ordered_quantity=ordered_quantity,
        status=status,
        reducer_state=reducer_state,
    )
    if reduced is None:
        appended = _append_order_event(
            report,
            response,
            client_order_id=client_order_id,
            ordered_quantity=ordered_quantity,
            event_source=event_source,
            status=status,
        )
        if appended is not False:
            _increment_observation_status(observation, status)
        return True
    if not reduced[0]:
        return False
    appended = _append_order_event(
        report,
        response,
        client_order_id=client_order_id,
        ordered_quantity=ordered_quantity,
        event_source=event_source,
        status=status,
    )
    if appended is not False:
        _replace_observation_status(observation, reduced[1], reduced[2])
    return True


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

    response_ordered_quantity = _response_ordered_quantity(response, ordered_quantity)
    raw_identity = {
        "schema_version": _ORDER_EVENT_SCHEMA_VERSION,
        "client_order_id": stable_client_order_id,
        "venue_order_id": _optional_text(response.get("orderId")),
        "ordered_quantity": _quantity_text(response_ordered_quantity),
        "cumulative_filled_quantity": _quantity_text(response.get("executedQty")),
        "status": status,
        "event_source": source,
    }
    canonical = json.dumps(raw_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload = {
        "schema_version": _ORDER_EVENT_SCHEMA_VERSION,
        "client_order_id_sha256": _sha256_text(stable_client_order_id),
        "venue_order_id_sha256": _sha256_text(_optional_text(response.get("orderId"))),
        "status": status,
        "event_source": source,
    }
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


def _reduce_order_response(
    response: object,
    *,
    client_order_id: object,
    ordered_quantity: object,
    status: str,
    reducer_state: dict[str, dict[str, Any]] | None,
) -> tuple[bool, str | None, str | None] | None:
    if reducer_state is None:
        return None
    if not isinstance(response, Mapping):
        return False, None, None
    stable_client_order_id = str(client_order_id or "").strip()
    if not stable_client_order_id or not status:
        return False, None, None

    state_key = _sha256_text(stable_client_order_id)
    previous = reducer_state.get(state_key, {})
    previous_status = _optional_text(previous.get("status"))
    if previous_status in _TERMINAL_STATUSES:
        return False, previous_status, previous_status
    if previous_status == "PARTIALLY_FILLED" and status not in _PARTIAL_FILL_STATUSES | _TERMINAL_STATUSES:
        return False, previous_status, previous_status

    cumulative_quantity = previous.get("cumulative_quantity")
    if status in _PARTIAL_FILL_STATUSES | _FILLED_STATUSES:
        ordered = _quantity_decimal(ordered_quantity)
        response_ordered = _quantity_decimal(response.get("origQty"))
        cumulative = _quantity_decimal(response.get("executedQty"))
        if ordered is None or response_ordered is None or response_ordered != ordered:
            return False, previous_status, previous_status
        if cumulative is None or cumulative > ordered:
            return False, previous_status, previous_status
        if status in _FILLED_STATUSES and cumulative != ordered:
            return False, previous_status, previous_status
        if cumulative_quantity is not None and cumulative < cumulative_quantity:
            return False, previous_status, previous_status
        cumulative_quantity = cumulative

    reducer_state[state_key] = {
        "status": status,
        "cumulative_quantity": cumulative_quantity,
    }
    return True, previous_status, status


def _increment_observation_status(observation: dict[str, int], status: str) -> None:
    field = _observation_field(status)
    if field is not None:
        observation[field] += 1


def _replace_observation_status(
    observation: dict[str, int],
    previous_status: str | None,
    current_status: str | None,
) -> None:
    previous_field = _observation_field(previous_status or "")
    current_field = _observation_field(current_status or "")
    if previous_field == current_field:
        return
    if previous_field is not None:
        observation[previous_field] = max(0, observation[previous_field] - 1)
    if current_field is not None:
        observation[current_field] += 1


def _observation_field(status: str) -> str | None:
    if status in _FILLED_STATUSES:
        return "filled_count"
    if status in _PARTIAL_FILL_STATUSES:
        return "partially_filled_count"
    if status in _FAILED_STATUSES:
        return "failed_count"
    return "broker_acknowledged_count" if status else None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _response_ordered_quantity(response: Mapping[str, Any], ordered_quantity: object) -> object:
    response_ordered_quantity = response.get("origQty")
    return ordered_quantity if response_ordered_quantity in (None, "") else response_ordered_quantity


def _quantity_text(value: object) -> str | None:
    quantity = _quantity_decimal(value)
    if quantity is None:
        return None
    if not quantity:
        return "0"
    return format(quantity.normalize(), "f")


def _quantity_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        quantity = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not quantity.is_finite() or quantity < 0:
        return None
    return quantity
