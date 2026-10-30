"""Call recordings made on main 3974b72 before the notification patch."""

import copy
import json
import socket
import os
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from notifications.digest_snapshot.digest_routing import routine_notification
from reporting.status_reports import maybe_send_periodic_btc_status_report
from runtime_support import runtime_notify


GOLDEN = Path(__file__).parent / "fixtures" / "notification_main_3974b72.json"


def record_execution_calls():
    from application.execution_service import execute_trend_buys, execute_trend_sells
    from tests.test_execution_service import _filled_response

    results = []
    for action in ("buy", "sell"):
        for size in (0, 1):
            events, state, balances = [], {}, {"ETHUSDT": 2.0}
            runtime = SimpleNamespace(client=object())
            report = {"buy_sell_intents": [], "gating_summary": {}, "gating_events": []}

            def broker(_rt, _rp, *, method_name, payload, effect_type):
                events.append(["broker", method_name, copy.deepcopy(payload), effect_type])
                return _filled_response(payload, method_name, 100.0)

            def persist(_rt, _rp, current, *, reason):
                events.append(["persist", reason, copy.deepcopy(current)])

            common = dict(should_skip_duplicate_trend_action_fn=lambda *a: False,
                append_log_fn=lambda *a: None, translate_fn=lambda key, **kw: key,
                format_qty_fn=lambda *a: size,
                ensure_asset_available_fn=lambda *a: events.append(["funding", *a[2:4]]) or True,
                runtime_call_client_fn=broker, next_order_id_fn=lambda *a: "fixture-order",
                set_symbol_trade_state_fn=lambda current, symbol, value: current.update({symbol: value}),
                record_trend_action_fn=lambda *a: events.append(["record_action", *a[1:]]),
                runtime_set_trade_state_fn=persist,
                runtime_notify_fn=lambda rt, rp, text: events.append(["notify", text]))
            if action == "buy":
                returned = execute_trend_buys(runtime, report, state,
                    {"ETHUSDT": {"weight": 1.0, "relative_score": 1.2}}, ["ETHUSDT"],
                    {"ETHUSDT": 100.0}, {"ETHUSDT": 100.0}, balances, 500.0, [], "20261016", **common)
            else:
                returned = execute_trend_sells(runtime, report, state,
                    {"ETHUSDT": {"base_asset": "ETH"}}, {"ETHUSDT": "fixture_reason"},
                    {"ETHUSDT": 100.0}, balances, 500.0, [], "20261016", **common)
            results.append([action, size, returned, events, state, balances, report])
    return results


def record_main_calls(monkeypatch, mode):
    import main
    from notify_i18n_support import translate
    from tests.test_cycle_service import CycleServiceTests

    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", mode)
    monkeypatch.delenv("QSL_DIGEST_ROUTINE_ORDERS", raising=False)
    result = {"runtime": [], "routine": [], "status": [], "funding": [], "cycles": []}
    for dry_run in (False, True):
        for urgent in (False, True):
            calls = []
            runtime = SimpleNamespace(run_id="fixture-run", dry_run=dry_run, side_effect_log=[],
                tg_token="fixture-token", tg_chat_id="fixture-chat",
                notifier=lambda **kw: calls.append(kw) or {"transport_acknowledged": True})
            report = {"notifications": [], "summary": {}, "side_effect_summary": {
                "executed_call_count": 0, "suppressed_call_count": 0}}
            returned = runtime_notify(runtime, report, "fixture notice", urgent=urgent)
            result["runtime"].append([dry_run, urgent, returned, calls, report, runtime.side_effect_log])
    for is_order in (False, True):
        calls, report = [], {"notifications": []}
        returned = routine_notification(lambda rt, rp, text: calls.append(text) or True,
            object(), report, "fixture routine", routine_order=is_order)
        result["routine"].append([is_order, returned, calls, report])
    for locale in ("zh-CN", "en"):
        monkeypatch.setenv("NOTIFY_LANG", locale)
        for acknowledged in (False, True):
            calls, state = [], {}
            returned = maybe_send_periodic_btc_status_report(state, "fixture-token", "fixture-chat",
                datetime(2026, 10, 16, 0, tzinfo=timezone.utc), 24, 1000, 250, .01, 65000,
                {"ahr999": .7, "zscore": 1.2, "regime_on": True}, .3,
                "示例策略" if locale == "zh-CN" else "Example strategy", translate_fn=translate,
                separator="---", notifier_fn=lambda text: calls.append(text) or acknowledged)
            result["status"].append([locale, acknowledged, returned, calls, state])
    with patch.object(main, "send_tg_msg", side_effect=lambda *args: result["funding"].append(args) or True):
        result["funding_result"] = main._send_routine_tg_message("fixture-token", "fixture-chat", "fixture funding")
    for permitted, earn_failure in ((False, False), (True, False), (True, True)):
        notifications, ownership = [], []
        report, events = CycleServiceTests()._run_funds_cycle(permitted, earn_failure=earn_failure,
            notifications=notifications, ownership_events=ownership)
        # Record the actual report, state write order, and sends without object reprs.
        result["cycles"].append([permitted, earn_failure, report, events, ownership,
            [[args[2], kwargs] for args, kwargs in notifications]])
    result["execution"] = record_execution_calls()
    return json.loads(json.dumps(result, default=str))


@pytest.mark.parametrize("flag", [None, "", "false", "invalid"])
@pytest.mark.parametrize("mode", ["immediate", "daily_digest"])
def test_flags_off_matches_main_entrypoint_and_renderer_call_recordings(monkeypatch, flag, mode):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in notification tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    if flag is None:
        monkeypatch.delenv("BINANCE_UNIFIED_NOTIFICATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BINANCE_UNIFIED_NOTIFICATION_ENABLED", flag)
    from notifications import unified_notifications as unified
    original_route = unified.route
    deferred = []
    def route_with_confirmed_candidate(send, *, kind, **kwargs):
        if kind in {"routine_cycle", "healthy_heartbeat", "order_submitted", "fill"}:
            deferred.append(kind)
            return False
        return original_route(send, kind=kind, **kwargs)
    monkeypatch.setattr(unified, "route", route_with_confirmed_candidate)
    golden = json.loads(GOLDEN.read_text())
    expected = copy.deepcopy(golden[mode])
    observed = record_main_calls(monkeypatch, mode)
    assert deferred
    # The confirmed candidate replaces only routine transport. Keep the
    # original broker, accounting, state and urgent call recordings exact.
    expected["runtime"][0] = [False, False, False, [], {
        "notifications": [], "summary": {}, "side_effect_summary": {
            "executed_call_count": 0, "suppressed_call_count": 0}}, []]
    expected["funding"] = []
    expected["funding_result"] = False
    expected["status"] = copy.deepcopy(golden["immediate"]["status"])
    for item in expected["execution"]:
        item[3] = [event for event in item[3] if event[0] != "notify"]
        notices = item[6].get("notifications")
        if notices and all(notice.get("delivery_status") == "deferred_digest" for notice in notices):
            item[6].pop("notifications")
    for actual, original in zip(observed["cycles"], expected["cycles"]):
        actual_summary = actual[2].get("summary", {})
        original_summary = original[2].get("summary", {})
        if "notification_digest" not in original_summary and "notification_digest" in actual_summary:
            assert actual_summary["notification_digest"]["schema_version"] == "qsl.notification_cycle.v1"
            actual_summary.pop("notification_digest")
            if not actual_summary and "summary" not in original[2]:
                actual[2].pop("summary")
    assert observed == expected


@pytest.mark.parametrize("flag", [None, "", "false", "invalid"])
@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_flags_off_transport_receipts_and_http_calls_match_main(monkeypatch, flag, locale):
    import live_services
    scope = dict(vars(live_services))
    exec(compile((GOLDEN.parent / "send_tg_msg_main_3974b72.source").read_text(), "<main-transport>", "exec"), scope)
    monkeypatch.setenv("NOTIFY_LANG", locale)
    if flag is None:
        monkeypatch.delenv("BINANCE_UNIFIED_NOTIFICATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BINANCE_UNIFIED_NOTIFICATION_ENABLED", flag)
    for status, payload in [(200, {"ok": True}), (200, {"ok": True, "result": []}),
            (200, {"ok": True, "result": {"message_id": 1}}), (200, {"ok": False}), (500, {})]:
        def record(sender):
            calls = []
            monkeypatch.setattr(live_services.requests, "post", lambda *a, **kw: calls.append([a, kw]) or
                SimpleNamespace(status_code=status, json=lambda: payload))
            result = sender("fixture-token", "fixture-chat", "示例" if locale == "zh-CN" else "Example")
            return calls, result
        assert record(live_services.send_tg_msg) == record(scope["send_tg_msg"])


@pytest.mark.parametrize("flag", [None, "", "false", "invalid"])
@pytest.mark.parametrize("locale", ["zh", "en"])
def test_flags_off_workflow_failure_script_matches_main_sends(monkeypatch, tmp_path, flag, locale):
    root = Path(__file__).resolve().parents[1]
    current = (root / ".github/workflows/runtime-protected.yml").read_text().split(
        "      - name: 6. Notify Telegram on runtime workflow failure", 1)[1]
    current = current.split("        run: |\n", 1)[1].split("        env:\n", 1)[0]
    current = "\n".join(line[10:] if line.startswith("          ") else line for line in current.splitlines())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['FIXTURE_CALLS']).write_text(json.dumps(sys.argv[1:]))\nprint('{\"ok\":true}')\n")
    curl.chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)
    env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "TG_TOKEN": "fixture-token", "GLOBAL_TELEGRAM_CHAT_ID": "fixture-chat",
        "GITHUB_WORKSPACE": str(tmp_path), "NOTIFY_LANG": locale, "GITHUB_RUN_NUMBER": "1",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "1", "GITHUB_SERVER_URL": "https://example.invalid",
        "GITHUB_REPOSITORY": "example/repository", "FIXTURE_CALLS": str(tmp_path / "calls.json")}
    env.pop("BINANCE_UNIFIED_NOTIFICATION_ENABLED", None)
    if flag is not None:
        env["BINANCE_UNIFIED_NOTIFICATION_ENABLED"] = flag
    recordings = []
    for script in ((GOLDEN.parent / "workflow_failure_main_3974b72.sh").read_text(), current):
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        recordings.append([result.stdout, result.stderr, json.loads((tmp_path / "calls.json").read_text())])
    assert recordings[0] == recordings[1]

