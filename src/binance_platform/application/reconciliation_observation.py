"""Offline synthetic reconciliation. Standard library only; no runtime clients.

The exact fixture comparison is a mock, not Binance accounting semantics. A live
adapter and its approved identity, coverage and persistence checks do not exist.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re


POLICY = {"purpose": "observation_only", "no_order": True, "execution_authority_granted": False}
CONFIG_FIELDS = {
    "mode", "source_id", "environment", "expected_account_id", "strategy_profile",
    "scope_version", "max_age_seconds", "max_window_seconds",
}


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_invalid")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp_invalid")
    return result.astimezone(timezone.utc)


def _identifier(value, prefix):
    return isinstance(value, str) and value.startswith(prefix) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value)


def _validate_config(config):
    if (not isinstance(config, dict) or set(config) != CONFIG_FIELDS
            or config["mode"] != "mock" or config["environment"] != "synthetic"
            or config["source_id"] != "binance.reconciliation_observation.mock"
            or not _identifier(config["expected_account_id"], "binance-paper-mock-")
            or not _identifier(config["strategy_profile"], "synthetic_")
            or not _identifier(config["scope_version"], "synthetic-")
            or type(config["max_age_seconds"]) is not int or not 300 <= config["max_age_seconds"] <= 3600
            or type(config["max_window_seconds"]) is not int or not 1 <= config["max_window_seconds"] <= 300):
        raise ValueError("mock_config_invalid")


def _broker_values(snapshot):
    balances = snapshot["balances"]
    if not isinstance(balances, dict) or not balances:
        raise ValueError("balances_invalid")
    normalized = {}
    for asset, value in balances.items():
        if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", asset) or not isinstance(value, str):
            raise ValueError("balances_invalid")
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError("balances_invalid")
        normalized[asset] = number
    if not isinstance(snapshot["open_orders"], list) or not isinstance(snapshot["executions"], list):
        raise ValueError("history_invalid")
    return normalized, snapshot["open_orders"], snapshot["executions"]


def collect_mock_observation(config, fixture, *, persist_report, now=None):
    """Compare explicit synthetic inputs, persist once, then return a redacted envelope.

    ``persist_report`` is an offline sink. Exceptions yield a diagnostic without a
    report ID; callers must not publish a successful observation before it returns.
    Fixture acquisition timestamps are never rewritten by serialization/replay.
    """
    _validate_config(config)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timestamp_invalid")
    if (not isinstance(fixture, dict)
            or set(fixture) != {"observation_id", "report_id", "broker_before", "broker_after",
                               "ledger_before", "ledger_after", "reference"}
            or not _identifier(fixture["observation_id"], "fixture-observation-")
            or not _identifier(fixture["report_id"], "fixture-report-")):
        raise ValueError("mock_fixture_invalid")
    inputs = deepcopy(fixture)
    reasons = []
    times = []
    broker_values = []
    ledger_values = []
    for name in ("broker_before", "broker_after", "ledger_before", "ledger_after"):
        snapshot = inputs[name]
        broker = name.startswith("broker_")
        incomplete = "broker_snapshot_incomplete" if broker else "ledger_unavailable"
        if not isinstance(snapshot, dict):
            reasons.append(incomplete)
            continue
        try:
            at = parse_time(snapshot["observed_at"])
            times.append(at)
            if (now - at).total_seconds() > config["max_age_seconds"]:
                reasons.append("input_stale")
            if (at - now).total_seconds() > 300:
                reasons.append("input_future")
            if snapshot.get("complete") is not True:
                reasons.append(incomplete)
            if broker:
                if snapshot.get("account_id") != config["expected_account_id"]:
                    reasons.append("account_identity_mismatch")
                broker_values.append(_broker_values(snapshot))
            else:
                if not _identifier(snapshot.get("version"), "synthetic-") or not isinstance(snapshot.get("entries"), list):
                    raise ValueError("ledger_invalid")
                ledger_values.append((snapshot["version"], snapshot["entries"]))
        except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
            reasons.append(incomplete)
    if len(broker_values) == 2 and broker_values[0] != broker_values[1]:
        reasons.append("broker_changed")
    if len(ledger_values) == 2 and ledger_values[0] != ledger_values[1]:
        reasons.append("ledger_changed")
    if times and (max(times) - min(times)).total_seconds() > config["max_window_seconds"]:
        reasons.append("acquisition_window_exceeded")
    for before, after in (("broker_before", "broker_after"), ("ledger_before", "ledger_after")):
        try:
            if parse_time(inputs[before]["observed_at"]) > parse_time(inputs[after]["observed_at"]):
                reasons.append("acquisition_order_invalid")
        except (KeyError, ValueError, TypeError, OverflowError):
            pass  # Already reported as a missing/incomplete surface above.
    reference = inputs["reference"]
    reference_version = None
    expected = None
    if reference is None:
        reasons.append("reference_unavailable")
    else:
        try:
            if (reference["account_id"] != config["expected_account_id"]
                    or reference["scope_version"] != config["scope_version"]
                    or not _identifier(reference["version"], "synthetic-")
                    or not isinstance(reference["ledger_entries"], list)):
                raise ValueError("reference_invalid")
            expected = _broker_values(reference), reference["ledger_entries"]
            reference_version = reference["version"]
        except (KeyError, ValueError, TypeError, InvalidOperation):
            reasons.append("reference_binding_invalid")
    reasons = sorted(set(reasons))
    complete = not reasons
    comparison = "incomparable"
    if complete:
        comparison = "matched" if (broker_values[0], ledger_values[0][1]) == expected else "mismatched"
    report = {
        "schema_version": "qsl_reconciliation_observation_mock_report.v1",
        "report_id": inputs["report_id"], "observation_id": inputs["observation_id"],
        "mode": "mock", "inputs": inputs, "comparison_status": comparison,
        "reason_codes": reasons or (["comparison_mismatch"] if comparison == "mismatched" else []),
        "policy": dict(POLICY),
    }
    report_id = inputs["report_id"]
    try:
        persist_report(report)
    except Exception:
        report_id = None
        complete = False
        comparison = "incomparable"
        reasons = ["report_persistence_failed"]
    status = "ready" if complete else ("stale" if "input_stale" in reasons else "unavailable")
    timestamp = now.astimezone(timezone.utc).isoformat()
    return {
        "schema_version": "qsl_reconciliation_observation_source_snapshot.v1",
        "source_id": config["source_id"], "generated_at": timestamp, "computed_at": timestamp,
        "data_status": status,
        "observations": [{
            "observation_id": inputs["observation_id"], "platform": "binance",
            "strategy_profile": config["strategy_profile"], "environment": "synthetic",
            "scope_version": config["scope_version"], "reference_version": reference_version,
            "first_observed_at": min(times).isoformat() if times else None,
            "last_observed_at": max(times).isoformat() if times else None,
            "evidence_sample_count": 1 if complete else 0,
            "coverage": "complete" if complete else ("partial" if times else "none"),
            "comparison_status": comparison,
            "reason_codes": reasons or (["comparison_mismatch"] if comparison == "mismatched" else []),
            "report_id": report_id,
        }],
        "errors": reasons, "policy": dict(POLICY),
    }
