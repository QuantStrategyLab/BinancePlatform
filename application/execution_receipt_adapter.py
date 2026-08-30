"""Privacy-safe execution-receipt observations for Binance runtime reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant_platform_kit.common.execution_receipts import (
    attach_runtime_execution_receipt,
    resolve_execution_receipt_fact,
)


_OBSERVATION_KEY = "execution_receipt_observation"
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


def record_order_response(report: dict[str, Any], response: object) -> None:
    """Record only an explicit Binance terminal/status fact from a response."""

    observation = _observation(report)
    status = str(response.get("status") or "").strip().upper() if isinstance(response, Mapping) else ""
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
