"""Characterize the public cycle's notification digest with offline boundaries."""

import builtins
from types import SimpleNamespace

import pytest

from application import cycle_service
from notifications import routing, unified_notifications


_COUNTS = (
    "submission_attempted_count", "broker_acknowledged_count", "partially_filled_count",
    "filled_count", "transport_uncertain_count", "failed_count",
)
_UNSET = object()


class _ReadError(Exception):
    pass


class _IntegerSubclass(int):
    pass


class _TraceDict(dict):
    def __init__(self, values, events, name, *, fail_get=None, fail_item=None, error=None):
        super().__init__(values)
        self.events = events
        self.name = name
        self.fail_get = fail_get
        self.fail_item = fail_item
        self.error = error

    def get(self, key, default=None):
        self.events.append([self.name, "get", key])
        if key == self.fail_get:
            raise self.error
        return super().get(key, default)

    def __getitem__(self, key):
        self.events.append([self.name, "item", key])
        if key == self.fail_item:
            raise self.error
        return super().__getitem__(key)

    def setdefault(self, key, default=None):
        self.events.append([self.name, "setdefault", key])
        return super().setdefault(key, default)


class _Runtime(SimpleNamespace):
    @property
    def dry_run(self):
        self.events.append(["runtime", "dry_run"])
        return self.dry_value


def _counts(**overrides):
    return dict.fromkeys(_COUNTS, 0) | overrides


def _digest(*, dry_run=False, order_count=None, fill_count=None, routine=False):
    return {
        "schema_version": "qsl.notification_cycle.v1",
        "dry_run": dry_run,
        "order_count": order_count,
        "fill_count": fill_count,
        "routine": routine,
    }


@pytest.fixture
def digest_cycle(monkeypatch):
    observations = []

    def run(*, counts=_UNSET, report_fields=None, dry_run=False, enabled=True,
            observation_factory=None, gate_error=None):
        events = []
        report = _TraceDict({"status": "ok", **(report_fields or {})}, events, "report")
        runtime = _Runtime(
            events=events, dry_value=dry_run, standard_execution_permitted=True,
            state_owner_held=True, strategy_profile="synthetic-profile",
        )
        receipt = _counts() if counts is _UNSET else counts
        if observation_factory is not None:
            receipt = observation_factory(events)
        elif isinstance(receipt, dict):
            receipt = _TraceDict(receipt, events, "observation")

        def record(name, result=None):
            def call(*_args, **_kwargs):
                events.append([name])
                return result
            return call

        def unexpected(*_args, **_kwargs):
            raise AssertionError("the synthetic client boundary must end this cycle")

        def attach_receipt(current):
            events.append(["attach_receipt"])
            current["execution_receipt_observation"] = receipt

        def digest_enabled(channel):
            events.append(["gate", channel])
            if gate_error is not None:
                raise gate_error
            return enabled

        def record_execution(profile, payload, **_kwargs):
            events.append(["record_execution", profile, payload])

        def export_lifecycle(profile, payload):
            events.append(["export_lifecycle", profile, payload])

        monkeypatch.setattr(cycle_service, "acquire_runtime_state_owner", record("owner_claim", True))
        monkeypatch.setattr(cycle_service, "release_runtime_state_owner", unexpected)
        monkeypatch.setattr(cycle_service, "finalize_notification_delivery", record("finalize_notifications"))
        monkeypatch.setattr(cycle_service, "attach_execution_receipt_from_report", attach_receipt)
        monkeypatch.setattr(cycle_service, "try_record_platform_execution", record_execution)
        monkeypatch.setattr(cycle_service, "_write_lifecycle_export", export_lifecycle)
        monkeypatch.setattr(unified_notifications, "bind_cycle", record("bind_notifications", "synthetic-context"))
        monkeypatch.setattr(unified_notifications, "reset_cycle", record("reset_notifications"))
        monkeypatch.setattr(routing, "daily_digest_enabled", digest_enabled)
        monkeypatch.setattr(builtins, "_qsl_health_monitor", SimpleNamespace(beat=record("heartbeat")), raising=False)

        observation = {"report": report, "events": events}
        observations.append(observation)
        try:
            result = cycle_service.execute_strategy_cycle(
                runtime,
                build_execution_report=record("build_report", report),
                ensure_runtime_client=record("client_unavailable", False),
                load_cycle_execution_settings=record("settings", SimpleNamespace(
                    btc_status_report_interval_hours=24, allow_new_trend_entries_on_degraded=False,
                )),
                load_cycle_state=unexpected,
                append_trend_pool_source_logs=unexpected,
                capture_market_snapshot=unexpected,
                top_up_bnb_fuel=unexpected,
                compute_portfolio_allocation=unexpected,
                build_balance_snapshot=unexpected,
                maybe_reset_daily_state=unexpected,
                maybe_rebase_daily_state_for_balance_change=unexpected,
                compute_daily_pnls=unexpected,
                append_portfolio_report=unexpected,
                run_daily_circuit_breaker=unexpected,
                execute_trend_rotation=unexpected,
                execute_btc_dca_cycle=unexpected,
                manage_usdt_earn_buffer_runtime=unexpected,
                maybe_send_periodic_btc_status_report=unexpected,
                runtime_set_trade_state=unexpected,
                append_report_error=unexpected,
                runtime_notify=unexpected,
                translate_fn=unexpected,
                traceback_module=SimpleNamespace(),
            )
        finally:
            observation["events"] = list(events)
        assert result is report
        return report, observation["events"]

    run.observations = observations
    return run


@pytest.mark.parametrize(("counts", "fields", "expected"), [
    (_counts(), {}, _digest(order_count=0, fill_count=0, routine=True)),
    (_counts(submission_attempted_count=2, filled_count=2), {}, _digest(order_count=2, routine=True)),
    (_counts(submission_attempted_count=2, filled_count=1), {}, _digest()),
    (_counts(filled_count=1), {}, _digest()),
    (_counts(broker_acknowledged_count=1), {}, _digest()),
    (_counts(partially_filled_count=1), {}, _digest()),
    (_counts(transport_uncertain_count=1), {}, _digest()),
    (_counts(failed_count=1), {}, _digest()),
    (_counts(), {"status": "unknown"}, _digest()),
    (_counts(), {"status": "error"}, _digest()),
    (_counts(), {"status": None}, _digest()),
    (_counts(), {"execution_blocked_reason": "synthetic_block"}, _digest()),
    (_counts(), {"execution_blocked_reason": ""}, _digest(order_count=0, fill_count=0, routine=True)),
    (_counts(), {"circuit_breaker_triggered": True}, _digest()),
    (_counts(), {"circuit_breaker_triggered": False}, _digest(order_count=0, fill_count=0, routine=True)),
    (_counts(), {"error_summary": {"errors": ["synthetic"]}}, _digest()),
    (_counts(), {"error_summary": {"errors": []}}, _digest(order_count=0, fill_count=0, routine=True)),
])
def test_digest_preserves_public_counts_and_unknown_states(digest_cycle, counts, fields, expected):
    report, events = digest_cycle(counts=counts, report_fields=fields)
    assert report["summary"]["notification_digest"] == expected
    assert events.count(["gate", "daily_digest"]) == 1
    callbacks = [event[0] for event in events if event[0] not in {"report", "observation", "runtime"}]
    assert callbacks == [
        "settings", "build_report", "bind_notifications", "owner_claim", "client_unavailable",
        "reset_notifications", "finalize_notifications", "attach_receipt", "gate",
        "record_execution", "export_lifecycle", "heartbeat",
    ]


@pytest.mark.parametrize("missing", _COUNTS)
def test_each_missing_receipt_count_stays_unknown(digest_cycle, missing):
    counts = _counts()
    del counts[missing]
    report, _events = digest_cycle(counts=counts)
    assert report["summary"]["notification_digest"] == _digest()


@pytest.mark.parametrize(("field", "value"), [
    ("submission_attempted_count", True),
    ("broker_acknowledged_count", False),
    ("partially_filled_count", None),
    ("filled_count", -1),
    ("transport_uncertain_count", 0.0),
    ("failed_count", "0"),
    ("submission_attempted_count", _IntegerSubclass(0)),
])
def test_invalid_count_types_and_negative_counts_stay_unknown(digest_cycle, field, value):
    report, _events = digest_cycle(counts=_counts(**{field: value}))
    assert report["summary"]["notification_digest"] == _digest()


@pytest.mark.parametrize(("value", "expected", "recorded"), [
    (False, _digest(order_count=0, fill_count=0, routine=True), True),
    (True, _digest(dry_run=True), False),
    (0, _digest(order_count=0, fill_count=0, routine=True), True),
    (1, _digest(dry_run=True), False),
    ("", _digest(order_count=0, fill_count=0, routine=True), True),
    ("false", _digest(dry_run=True), False),
])
def test_dry_run_keeps_existing_truthiness_and_lifecycle_condition(digest_cycle, value, expected, recorded):
    report, events = digest_cycle(dry_run=value)
    assert report["summary"]["notification_digest"] == expected
    assert any(event[0] == "record_execution" for event in events) is recorded
    assert any(event[0] == "export_lifecycle" for event in events) is recorded
    assert events[-1] == ["heartbeat"]


@pytest.mark.parametrize("value", [None, {}, [], "", 0])
def test_falsey_observations_keep_unknown_counts(digest_cycle, value):
    report, _events = digest_cycle(counts=value)
    assert report["summary"]["notification_digest"] == _digest()


@pytest.mark.parametrize("value", [[1], "synthetic", 1])
def test_truthy_non_mapping_observations_keep_finalization_error(digest_cycle, value):
    with pytest.raises(AttributeError):
        digest_cycle(counts=value)
    saved = digest_cycle.observations[0]
    assert "summary" not in saved["report"]
    assert not any(event[0] in {"record_execution", "export_lifecycle", "heartbeat"} for event in saved["events"])


def test_existing_summary_fields_and_object_are_retained(digest_cycle):
    summary = {"prior": "synthetic", "notification_digest": {"old": True}}
    report, _events = digest_cycle(report_fields={"summary": summary})
    assert report["summary"] is summary
    assert summary == {"prior": "synthetic", "notification_digest": _digest(order_count=0, fill_count=0, routine=True)}


@pytest.mark.parametrize("summary", [None, [], "synthetic"])
def test_malformed_summary_keeps_assignment_error_and_completed_value_reads(digest_cycle, summary):
    with pytest.raises(TypeError):
        digest_cycle(report_fields={"summary": summary})
    saved = digest_cycle.observations[0]
    events = saved["events"]
    assert dict.__getitem__(saved["report"], "summary") is summary
    gate = events.index(["gate", "daily_digest"])
    assignment = events.index(["report", "setdefault", "summary"])
    assert events[gate:assignment].count(["runtime", "dry_run"]) == 4
    assert not any(event[0] in {"record_execution", "export_lifecycle", "heartbeat"} for event in events)


@pytest.mark.parametrize(("fields", "counts"), [
    ({"summary": None, "error_summary": "synthetic"}, [1]),
    ({"summary": {"notification_digest": {"prior": True}}}, _counts()),
])
def test_disabled_digest_does_not_read_or_replace_projection_inputs(digest_cycle, fields, counts):
    original_summary = fields["summary"]
    report, events = digest_cycle(enabled=False, counts=counts, report_fields=fields)
    assert dict.__getitem__(report, "summary") is original_summary
    assert not any(event[0] == "observation" for event in events)
    assert ["report", "setdefault", "summary"] not in events
    assert events.count(["gate", "daily_digest"]) == 1
    assert events[-1] == ["heartbeat"]


def test_gate_error_propagates_without_projection_or_remaining_finalization(digest_cycle):
    error = _ReadError("synthetic gate failure")
    with pytest.raises(_ReadError) as caught:
        digest_cycle(gate_error=error)
    assert caught.value is error
    events = digest_cycle.observations[0]["events"]
    assert events[-1] == ["gate", "daily_digest"]


@pytest.mark.parametrize("operation", ["get", "item"])
def test_observation_read_error_propagates_identical_exception(digest_cycle, operation):
    error = _ReadError("synthetic observation failure")
    with pytest.raises(_ReadError) as caught:
        digest_cycle(observation_factory=lambda events: _TraceDict(
            _counts(), events, "observation", error=error,
            **{"fail_" + operation: "filled_count"},
        ))
    assert caught.value is error
    events = digest_cycle.observations[0]["events"]
    assert events[-1] == ["observation", operation, "filled_count"]


def test_dry_run_truthiness_error_keeps_eager_projection_failure(digest_cycle):
    error = _ReadError("synthetic dry-run truthiness failure")

    class DryRunValue:
        calls = 0

        def __bool__(self):
            self.calls += 1
            if self.calls == 2:
                raise error
            return False

    value = DryRunValue()
    with pytest.raises(_ReadError) as caught:
        digest_cycle(dry_run=value)
    assert caught.value is error
    assert value.calls == 2
    saved = digest_cycle.observations[0]
    assert saved["events"][-1] == ["runtime", "dry_run"]
    assert "summary" not in saved["report"]


def test_invalid_earlier_count_short_circuits_later_reads(digest_cycle):
    error = _ReadError("later count must not be read")
    report, events = digest_cycle(observation_factory=lambda trace: _TraceDict(
        _counts(submission_attempted_count=True), trace, "observation",
        fail_get="broker_acknowledged_count", fail_item="submission_attempted_count", error=error,
    ))
    assert report["summary"]["notification_digest"] == _digest()
    assert [event for event in events if event[0] == "observation"] == [
        ["observation", "get", "submission_attempted_count"],
    ]


@pytest.mark.parametrize(("status", "raises"), [("ok", True), ("unknown", False)])
def test_error_summary_is_read_only_after_prior_health_conditions(digest_cycle, status, raises):
    fields = {"status": status, "error_summary": "synthetic"}
    if raises:
        with pytest.raises(AttributeError):
            digest_cycle(report_fields=fields)
        assert ["runtime", "dry_run"] not in digest_cycle.observations[0]["events"][
            digest_cycle.observations[0]["events"].index(["gate", "daily_digest"]):
        ]
    else:
        report, _events = digest_cycle(report_fields=fields)
        assert report["summary"]["notification_digest"] == _digest()


def test_projection_access_order_and_assignment_remain_explicit(digest_cycle):
    report, events = digest_cycle()
    gate = events.index(["gate", "daily_digest"])
    assignment = events.index(["report", "setdefault", "summary"])
    assert events[gate + 1:assignment + 1] == [
        ["report", "get", "execution_receipt_observation"],
        *[event for key in _COUNTS for event in (["observation", "get", key], ["observation", "item", key])],
        ["observation", "item", "submission_attempted_count"],
        ["observation", "item", "filled_count"],
        ["report", "get", "status"],
        ["observation", "item", "broker_acknowledged_count"],
        ["observation", "item", "partially_filled_count"],
        ["observation", "item", "transport_uncertain_count"],
        ["observation", "item", "failed_count"],
        ["report", "get", "execution_blocked_reason"],
        ["report", "get", "circuit_breaker_triggered"],
        ["report", "get", "error_summary"],
        ["runtime", "dry_run"],
        ["runtime", "dry_run"],
        ["observation", "item", "filled_count"],
        ["observation", "item", "filled_count"],
        ["runtime", "dry_run"],
        ["runtime", "dry_run"],
        ["report", "setdefault", "summary"],
    ]
    assert report["summary"]["notification_digest"] == _digest(order_count=0, fill_count=0, routine=True)
