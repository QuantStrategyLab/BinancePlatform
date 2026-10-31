"""Characterize completion reporting through the public, offline cycle."""

import builtins
from types import SimpleNamespace

import pytest

from application import cycle_service
from notifications import routing, unified_notifications
from runtime_support import ExecutionIntegrityError


class SyntheticStop(BaseException):
    pass


@pytest.fixture
def completion_cycle(monkeypatch):
    observations = []
    original_builder = cycle_service._build_platform_execution_result

    def run(*, exit_at="owner_busy", dry_run=False, profiles=("record-profile", "export-profile"),
            trace_values=False, failures=None, monitor="present", release_uncertain=False,
            rebind_sinks=False, report_fields=None):
        events = []
        payloads = []
        failures = failures or {}

        def step(name, *details):
            events.append([name, *details])
            if name in failures:
                raise failures[name]

        class ValueProbe:
            def __init__(self, name, value):
                self.name, self.value = name, value

            def __bool__(self):
                step(f"{self.name}.bool")
                return bool(self.value)

            def __str__(self):
                step(f"{self.name}.str")
                return str(self.value)

        class Runtime(SimpleNamespace):
            profile_reads = 0

            @property
            def dry_run(self):
                step("runtime.dry_run")
                return ValueProbe("dry_run", dry_run) if trace_values else dry_run

            @property
            def strategy_profile(self):
                self.profile_reads += 1
                name = f"profile.{self.profile_reads}"
                step(name)
                value = profiles[self.profile_reads - 1]
                return ValueProbe(name, value) if trace_values else value

        class Report(dict):
            def get(self, key, default=None):
                step(f"report.get.{key}")
                return super().get(key, default)

        runtime = Runtime(standard_execution_permitted=True, state_owner_held=True)
        interval = {"start_at": "synthetic-start", "end_at": "synthetic-end"}
        report = Report(status="ok", external_cash_flow_interval=interval)
        report.update(report_fields or {})
        observation = {"report": report, "events": events, "payloads": payloads}
        observations.append(observation)

        def record(name, result=None):
            def call(*_args, **_kwargs):
                step(name)
                return result
            return call

        def unexpected(*_args, **_kwargs):
            raise AssertionError("the synthetic cycle must stop before funds actions")

        def release(_runtime):
            step("owner_release")
            if release_uncertain:
                raise ExecutionIntegrityError("synthetic-release-uncertain")

        def build_lifecycle(current, *, state_healthy, state_owner_release_uncertain):
            assert current is report
            step("build_lifecycle", state_healthy, state_owner_release_uncertain)
            return original_builder(
                current, state_healthy=state_healthy,
                state_owner_release_uncertain=state_owner_release_uncertain,
            )

        def replacement_export(profile, payload):
            payloads.append(payload)
            step("replacement_export", profile)
            monkeypatch.setattr(
                builtins, "_qsl_health_monitor",
                SimpleNamespace(beat=lambda **kwargs: step("replacement_heartbeat", kwargs)),
                raising=False,
            )

        def record_execution(profile, payload, *, domain):
            payloads.append(payload)
            step("record_execution", profile, domain)
            if rebind_sinks:
                monkeypatch.setattr(cycle_service, "_write_lifecycle_export", replacement_export)

        def export_lifecycle(profile, payload):
            payloads.append(payload)
            step("export_lifecycle", profile)

        class Monitor:
            @property
            def beat(self):
                step("monitor.beat")
                return lambda **kwargs: step("heartbeat", kwargs)

        monkeypatch.setattr(cycle_service, "acquire_runtime_state_owner", record("owner_claim", exit_at != "owner_busy"))
        monkeypatch.setattr(cycle_service, "release_runtime_state_owner", release)
        monkeypatch.setattr(cycle_service, "reconcile_pending_funding_submission", record("reconcile_pending"))
        monkeypatch.setattr(cycle_service, "finalize_notification_delivery", record("finalize_notifications"))
        monkeypatch.setattr(cycle_service, "attach_execution_receipt_from_report", record("attach_receipt"))
        monkeypatch.setattr(cycle_service, "_build_platform_execution_result", build_lifecycle)
        monkeypatch.setattr(cycle_service, "try_record_platform_execution", record_execution)
        monkeypatch.setattr(cycle_service, "_write_lifecycle_export", export_lifecycle)
        monkeypatch.setattr(unified_notifications, "bind_cycle", record("bind_notifications", "synthetic-context"))
        monkeypatch.setattr(unified_notifications, "reset_cycle", record("reset_notifications"))
        monkeypatch.setattr(routing, "daily_digest_enabled", record("digest_gate", False))
        if monitor == "absent":
            monkeypatch.delattr(builtins, "_qsl_health_monitor", raising=False)
        else:
            monkeypatch.setattr(builtins, "_qsl_health_monitor", Monitor() if monitor == "present" else None, raising=False)

        result = cycle_service.execute_strategy_cycle(
            runtime,
            build_execution_report=record("build_report", report),
            ensure_runtime_client=record("client", exit_at != "client_unavailable"),
            load_cycle_execution_settings=record("settings", SimpleNamespace(
                btc_status_report_interval_hours=24, allow_new_trend_entries_on_degraded=False,
            )),
            load_cycle_state=record("state", ({}, {"degraded": False}, {"SYNTHETIC": {}}, True)),
            append_trend_pool_source_logs=record("pool_diagnostics"),
            capture_market_snapshot=record("snapshot", {
                "u_total": 100.0, "fuel_val": 0.0, "dynamic_usdt_buffer": 10.0,
                "prices": {}, "balances": {}, "btc_snapshot": {}, "trend_indicators": {},
            }),
            compute_portfolio_allocation=record("allocation", {
                "total_equity": 100.0, "trend_val": 0.0, "execution_permitted": False,
            }),
            top_up_bnb_fuel=unexpected,
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
            append_report_error=lambda _report, message, **kwargs: step("append_error", message, kwargs),
            runtime_notify=unexpected,
            translate_fn=unexpected,
            traceback_module=SimpleNamespace(),
        )
        assert result is report
        return observation

    run.observations = observations
    return run


def _tail(observation):
    names = [event[0] for event in observation["events"]]
    return names[names.index("digest_gate") + 1:]


def test_completion_preserves_access_order_and_shared_payload(completion_cycle):
    observation = completion_cycle()
    assert _tail(observation) == [
        "runtime.dry_run", "build_lifecycle", "report.get.status",
        "report.get.total_equity_usdt", "report.get.trend_equity_usdt",
        "report.get.status", "report.get.degraded_mode_level", "report.get.error",
        "profile.1", "record_execution", "profile.2", "export_lifecycle",
        "monitor.beat", "report.get.status", "report.get.error", "heartbeat",
    ]
    first, second = observation["payloads"]
    assert first is second
    assert first == {
        "platform": "binance", "status": "ok", "total_equity_usdt": None,
        "trend_equity_usdt": None, "external_cash_flow": None,
        "external_cash_flow_interval": None, "degraded_mode_level": None, "error": None,
    }
    assert ["record_execution", "record-profile", "crypto"] in observation["events"]
    assert ["export_lifecycle", "export-profile"] in observation["events"]
    assert observation["events"][-1] == ["heartbeat", {"status": "ok", "error": ""}]


@pytest.mark.parametrize(("value", "recorded"), [
    (False, True), (0, True), ("", True), (None, True), ([], True),
    (True, False), (1, False), ("false", False), ([0], False),
])
def test_dry_run_retains_truthiness_and_one_completion_lookup(completion_cycle, value, recorded):
    observation = completion_cycle(dry_run=value)
    tail = _tail(observation)
    assert tail.count("runtime.dry_run") == 1
    assert ("record_execution" in tail) is recorded
    assert ("export_lifecycle" in tail) is recorded
    assert ("profile.1" in tail) is recorded
    assert ("profile.2" in tail) is recorded
    assert tail[-1] == "heartbeat"


@pytest.mark.parametrize(("profiles", "expected"), [
    ((None, 0), ("", "")),
    (("", False), ("", "")),
    ((7, " export-profile "), ("7", " export-profile ")),
    ((" record-profile ", "second-profile"), (" record-profile ", "second-profile")),
])
def test_profile_reads_and_string_conversion_remain_separate(completion_cycle, profiles, expected):
    observation = completion_cycle(profiles=profiles, trace_values=True)
    assert ["record_execution", expected[0], "crypto"] in observation["events"]
    assert ["export_lifecycle", expected[1]] in observation["events"]
    for number, value in enumerate(profiles, 1):
        tail = _tail(observation)
        assert tail.count(f"profile.{number}") == 1
        assert tail.count(f"profile.{number}.bool") == 1
        assert tail.count(f"profile.{number}.str") == int(bool(value))


@pytest.mark.parametrize("failure", [
    "profile.1", "profile.1.bool", "profile.1.str",
    "profile.2", "profile.2.bool", "profile.2.str",
])
def test_profile_failure_preserves_exception_and_completed_sinks(completion_cycle, failure):
    error = RuntimeError("synthetic-profile-failure")
    with pytest.raises(RuntimeError) as caught:
        completion_cycle(trace_values=True, failures={failure: error})
    observation = completion_cycle.observations[0]
    tail = _tail(observation)
    assert caught.value is error
    assert tail[-1] == failure
    assert ("record_execution" in tail) is failure.startswith("profile.2")
    assert "export_lifecycle" not in tail
    assert "monitor.beat" not in tail


@pytest.mark.parametrize("exception_type", [RuntimeError, SyntheticStop])
@pytest.mark.parametrize("failure", [
    "runtime.dry_run", "dry_run.bool", "build_lifecycle", "record_execution", "export_lifecycle",
])
def test_lifecycle_failures_propagate_without_heartbeat(completion_cycle, exception_type, failure):
    error = exception_type("synthetic-completion-failure")
    with pytest.raises(exception_type) as caught:
        completion_cycle(trace_values=True, failures={failure: error})
    observation = completion_cycle.observations[0]
    assert caught.value is error
    assert _tail(observation)[-1] == failure
    assert "monitor.beat" not in _tail(observation)


@pytest.mark.parametrize("monitor", ["absent", "none"])
def test_missing_monitor_does_not_read_heartbeat_arguments(completion_cycle, monitor):
    observation = completion_cycle(dry_run=True, monitor=monitor)
    assert _tail(observation) == ["runtime.dry_run"]
    assert observation["report"]["execution_blocked_reason"] == "state_owner_busy"


@pytest.mark.parametrize("failure", [
    "monitor.beat", "report.get.status", "report.get.error", "heartbeat",
])
@pytest.mark.parametrize("exception_type", [RuntimeError, SyntheticStop])
def test_heartbeat_keeps_its_existing_exception_boundary(completion_cycle, failure, exception_type):
    error = exception_type("synthetic-heartbeat-failure")
    if exception_type is RuntimeError:
        observation = completion_cycle(dry_run=True, failures={failure: error})
        assert observation["report"]["status"] == "ok"
    else:
        with pytest.raises(SyntheticStop) as caught:
            completion_cycle(dry_run=True, failures={failure: error})
        assert caught.value is error
        observation = completion_cycle.observations[0]
    assert _tail(observation)[-1] == failure
    assert observation["payloads"] == []


def test_completion_resolves_exporter_and_monitor_after_previous_callback(completion_cycle):
    observation = completion_cycle(rebind_sinks=True)
    tail = _tail(observation)
    assert "export_lifecycle" not in tail
    assert "monitor.beat" not in tail
    assert tail.index("record_execution") < tail.index("replacement_export")
    assert observation["events"][-1] == ["replacement_heartbeat", {"status": "ok", "error": ""}]
    assert observation["payloads"][0] is observation["payloads"][1]


@pytest.mark.parametrize(("exit_at", "uncertain", "healthy", "status", "has_interval"), [
    ("owner_busy", False, False, "ok", False),
    ("client_unavailable", False, False, "ok", False),
    ("risk_rejection", False, True, "ok", True),
    ("risk_rejection", True, True, "error", False),
])
def test_completion_receives_original_state_flags_after_release(
    completion_cycle, exit_at, uncertain, healthy, status, has_interval,
):
    observation = completion_cycle(exit_at=exit_at, release_uncertain=uncertain)
    assert ["build_lifecycle", healthy, uncertain] in observation["events"]
    first, second = observation["payloads"]
    assert first is second
    assert first["status"] == status
    expected = observation["report"]["external_cash_flow_interval"] if has_interval else None
    assert first["external_cash_flow_interval"] == expected
    assert observation["events"][-1] == ["heartbeat", {"status": status, "error": ""}]
    if healthy:
        names = [event[0] for event in observation["events"]]
        assert names.index("owner_release") < names.index("build_lifecycle")


@pytest.mark.parametrize("failure", ["finalize_notifications", "attach_receipt", "digest_gate"])
def test_prior_finalization_failure_still_prevents_completion(completion_cycle, failure):
    error = RuntimeError("synthetic-earlier-finalization-failure")
    with pytest.raises(RuntimeError) as caught:
        completion_cycle(failures={failure: error})
    observation = completion_cycle.observations[0]
    names = [event[0] for event in observation["events"]]
    assert caught.value is error
    assert names[-1] == failure
    assert "runtime.dry_run" not in names
    assert "build_lifecycle" not in names
    assert "heartbeat" not in names
