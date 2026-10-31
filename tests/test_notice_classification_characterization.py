"""Characterize runtime-notice classification without invoking a transport."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from notifications import unified_notifications as unified


@pytest.mark.parametrize(
    "report, urgent, expected",
    [
        ({}, False, ("routine_cycle", None, 1)),
        ({"circuit_breaker_triggered": True}, False, ("routine_cycle", None, 1)),
        ({}, True, ("real_error", "runtime_failure", 1)),
        ({"diagnostics": None}, True, ("real_error", "runtime_failure", 1)),
        ({"diagnostics": []}, True, ("real_error", "runtime_failure", 1)),
        ({"diagnostics": {"cycle_failure": None}}, True, ("real_error", "runtime_failure", 1)),
        ({"circuit_breaker_triggered": "present"}, True, ("risk_escalation", "risk_escalation", 2)),
        ({"diagnostics": {"cycle_failure": {"error_type": "permission_error", "stage": 7}}},
         True, ("auth_failure", "auth_failure", 1)),
        ({"circuit_breaker_triggered": True,
          "diagnostics": {"cycle_failure": {"error_type": "permission_error"}}},
         True, ("risk_escalation", "risk_escalation", 2)),
        ({"diagnostics": {"cycle_failure": {"error_type": "execution_integrity_error"}}},
         True, ("order_status_unknown", "order_status_unknown", 1)),
        ({"diagnostics": {"cycle_failure": {"error_type": "order_reconciliation_error"}}},
         True, ("order_status_unknown", "order_status_unknown", 1)),
        ({"diagnostics": {"cycle_failure": {"stage": "snapshot", "error_type": "unavailable"}}},
         True, ("real_error", "cycle_snapshot_unavailable", 1)),
        ({"diagnostics": {"cycle_failure": {"stage": "snapshot", "error_type": ""}}},
         True, ("real_error", "runtime_failure", 1)),
        ({"diagnostics": {"cycle_failure": {"stage": "", "error_type": "unavailable"}}},
         True, ("real_error", "runtime_failure", 1)),
    ],
)
def test_public_notice_preserves_classification_and_dispatch_arguments(monkeypatch, report, urgent, expected):
    result, runtime, text = object(), object(), object()
    send, coverage = Mock(), Mock()
    route = Mock(return_value=result)
    monkeypatch.setattr(unified, "route", route)
    original = deepcopy(report)

    assert unified.runtime_notice(runtime, report, text, urgent=urgent,
                                  send=send, send_coverage=coverage) is result

    kind, reason, severity = expected
    route.assert_called_once_with(send, kind=kind, reason=reason, severity=severity,
                                  runtime=runtime, report=report, send_coverage=coverage)
    assert route.call_args.kwargs["report"] is report
    assert report == original
    send.assert_not_called()
    coverage.assert_not_called()


@pytest.mark.parametrize("result", [None, False, True, {}, object()])
def test_route_result_and_default_coverage_are_forwarded_without_coercion(monkeypatch, result):
    route = Mock(return_value=result)
    monkeypatch.setattr(unified, "route", route)
    assert unified.runtime_notice(object(), {}, object(), urgent=False, send=object()) is result
    assert route.call_args.kwargs["send_coverage"] is None


def test_nonurgent_notice_never_reads_report_or_formats_text(monkeypatch):
    class Unused:
        def get(self, *args):
            pytest.fail("nonurgent report was read")

        def __str__(self):
            pytest.fail("notification text was formatted")

    route = Mock(return_value=True)
    monkeypatch.setattr(unified, "route", route)
    report, text = Unused(), Unused()
    assert unified.runtime_notice(object(), report, text, urgent=False, send=object()) is True
    assert route.call_args.kwargs["report"] is report


class _LookupProbe:
    def __init__(self, name, values, events, *, fail_at=None, error=None):
        self.name, self.values, self.events = name, values, events
        self.fail_at, self.error = fail_at, error

    def _record(self, operation, key):
        event = (self.name, operation, key)
        self.events.append(event)
        if event == self.fail_at:
            raise self.error

    def get(self, key, default=None):
        self._record("get", key)
        return self.values.get(key, default)

    def __getitem__(self, key):
        self._record("getitem", key)
        return self.values[key]


def _observed_report(events, *, fail_at=None, error=None):
    failure = _LookupProbe("failure", {"stage": "snapshot", "error_type": "unavailable"}, events,
                           fail_at=fail_at, error=error)
    diagnostics = _LookupProbe("diagnostics", {"cycle_failure": failure}, events,
                               fail_at=fail_at, error=error)
    return _LookupProbe("report", {"diagnostics": diagnostics}, events, fail_at=fail_at, error=error)


def test_lookup_order_and_repeated_reads_precede_dispatch(monkeypatch):
    events = []
    report = _observed_report(events)
    result = object()

    def route(*args, **kwargs):
        events.append(("route", kwargs["kind"], kwargs["reason"]))
        return result

    monkeypatch.setattr(unified, "route", route)
    assert unified.runtime_notice(object(), report, object(), urgent=True, send=object()) is result
    assert events == [
        ("report", "get", "diagnostics"),
        ("diagnostics", "get", "cycle_failure"),
        ("report", "get", "circuit_breaker_triggered"),
        ("failure", "get", "error_type"),
        ("failure", "get", "error_type"),
        ("failure", "get", "stage"),
        ("failure", "get", "error_type"),
        ("failure", "getitem", "stage"),
        ("failure", "getitem", "error_type"),
        ("route", "real_error", "cycle_snapshot_unavailable"),
    ]


@pytest.mark.parametrize("fail_at", [
    ("report", "get", "diagnostics"),
    ("diagnostics", "get", "cycle_failure"),
    ("report", "get", "circuit_breaker_triggered"),
    ("failure", "get", "error_type"),
    ("failure", "get", "stage"),
    ("failure", "getitem", "stage"),
    ("failure", "getitem", "error_type"),
])
def test_lookup_exception_propagates_before_any_dispatch(monkeypatch, fail_at):
    events, error = [], RuntimeError("synthetic lookup failure")
    report = _observed_report(events, fail_at=fail_at, error=error)
    route = Mock()
    monkeypatch.setattr(unified, "route", route)
    with pytest.raises(RuntimeError) as caught:
        unified.runtime_notice(object(), report, object(), urgent=True, send=object())
    assert caught.value is error
    assert events[-1] == fail_at
    route.assert_not_called()


@pytest.mark.parametrize("report, exception", [
    (None, AttributeError),
    ({"circuit_breaker_triggered": True, "diagnostics": ["malformed"]}, AttributeError),
    ({"diagnostics": {"cycle_failure": {"stage": 7, "error_type": "unavailable"}}}, TypeError),
    ({"diagnostics": {"cycle_failure": {"stage": "snapshot", "error_type": ["malformed"]}}}, TypeError),
])
def test_malformed_truthy_fields_are_not_normalized_or_hidden(monkeypatch, report, exception):
    route = Mock()
    monkeypatch.setattr(unified, "route", route)
    with pytest.raises(exception):
        unified.runtime_notice(object(), report, object(), urgent=True, send=object())
    route.assert_not_called()


def test_urgency_truth_conversion_occurs_once_and_propagates_errors(monkeypatch):
    route = Mock()
    monkeypatch.setattr(unified, "route", route)
    events, error = [], ValueError("synthetic truth conversion")

    class Urgency:
        def __bool__(self):
            events.append("urgent")
            raise error

    with pytest.raises(ValueError) as caught:
        unified.runtime_notice(object(), object(), object(), urgent=Urgency(), send=object())
    assert caught.value is error
    assert events == ["urgent"]
    route.assert_not_called()


def test_dispatch_exception_is_propagated_without_retry(monkeypatch):
    error = OSError("synthetic route failure")
    route = Mock(side_effect=error)
    monkeypatch.setattr(unified, "route", route)
    with pytest.raises(OSError) as caught:
        unified.runtime_notice(object(), {}, object(), urgent=True, send=object())
    assert caught.value is error
    route.assert_called_once()
