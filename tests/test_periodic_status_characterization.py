"""Characterize the public heartbeat helper without transport or runtime access."""

import sys
import unittest
from datetime import datetime, timezone
from types import ModuleType
from unittest.mock import patch

from reporting.status_reports import maybe_send_periodic_btc_status_report


TRANSLATIONS = {
    "gate_on": "on",
    "gate_off": "off",
    "heartbeat_title": "heartbeat",
    "strategy_label": "strategy={name}",
    "total_equity": "equity",
    "trend_equity": "trend",
    "btc_price": "price",
    "btc_gate": "gate",
    "btc_target": "target",
    "zscore": "z",
}
TRANSLATION_ORDER = [
    "gate_on", "heartbeat_title", "strategy_label", "total_equity",
    "trend_equity", "btc_price", "btc_gate", "btc_target", "zscore",
]
EXPECTED_TEXT = (
    "heartbeat\n"
    "strategy=Example strategy\n"
    "equity: $1,234 | trend: $250 (+1.0%)\n"
    "price: $65,000 | gate: on\n"
    "target: 30.0% | AHR999: 0.70 | z: 1.2"
)


class PeriodicStatusCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.state = {"unrelated": "preserved"}
        self.arguments = {
            "state": self.state,
            "tg_token": "fixture-token",
            "tg_chat_id": "fixture-chat",
            "now_utc": datetime(2026, 10, 31, 8, tzinfo=timezone.utc),
            "interval_hours": 4,
            "total_equity": 1234.0,
            "trend_holdings_equity": 250.0,
            "trend_daily_pnl": 0.01,
            "btc_price": 65000.0,
            "btc_snapshot": {"regime_on": True, "ahr999": 0.7, "zscore": 1.2},
            "btc_target_ratio": 0.3,
            "strategy_display_name": "Example strategy",
            "translate_fn": self.translate,
            "separator": "unused separator",
            "notifier_fn": self.notify,
        }

    def translate(self, key, **kwargs):
        self.events.append(("translate", key, kwargs))
        return TRANSLATIONS[key].format(**kwargs)

    def notify(self, text):
        self.events.append(("notify", text))
        return {"transport_acknowledged": True}

    def invoke(self, **overrides):
        return maybe_send_periodic_btc_status_report(**(self.arguments | overrides))

    def translated_keys(self):
        return [event[1] for event in self.events if event[0] == "translate"]

    def test_exact_text_translation_order_and_acknowledged_bucket(self):
        self.assertIs(self.invoke(), True)
        self.assertEqual(self.translated_keys(), TRANSLATION_ORDER)
        self.assertEqual(self.events[2], ("translate", "strategy_label", {"name": "Example strategy"}))
        self.assertEqual(self.events[-1], ("notify", EXPECTED_TEXT))
        self.assertEqual(self.state, {"unrelated": "preserved", "last_btc_status_report_bucket": "2026103108"})

    def test_snapshot_lookup_order_is_preserved(self):
        events = self.events

        class TracedSnapshot(dict):
            def get(self, key, default=None):
                events.append(("get", key, default))
                return super().get(key, default)

            def __getitem__(self, key):
                events.append(("index", key))
                return super().__getitem__(key)

        self.invoke(btc_snapshot=TracedSnapshot(self.arguments["btc_snapshot"]))
        observed = [(event[0], event[1]) for event in self.events]
        expected = [("get", "regime_on")]
        expected.extend(("translate", key) for key in TRANSLATION_ORDER[:-1])
        expected.extend([("index", "ahr999"), ("translate", "zscore"), ("index", "zscore"), ("notify", EXPECTED_TEXT)])
        self.assertEqual(observed, expected)

    def test_false_and_missing_regime_render_the_same_gate(self):
        for regime in (False, None, 0, "", []):
            with self.subTest(regime=regime):
                self.events.clear()
                snapshot = {"regime_on": regime, "ahr999": 0.7, "zscore": 1.2}
                self.assertIs(self.invoke(state={}, btc_snapshot=snapshot), True)
                self.assertEqual(self.events[0], ("translate", "gate_off", {}))
                self.assertEqual(self.events[-1], ("notify", EXPECTED_TEXT.replace("gate: on", "gate: off")))
        self.events.clear()
        self.invoke(state={}, btc_snapshot={"ahr999": 0.7, "zscore": 1.2})
        self.assertEqual(self.events[0], ("translate", "gate_off", {}))

    def test_unicode_and_negative_numeric_rendering(self):
        self.invoke(
            strategy_display_name="示例策略 · alpha",
            total_equity=-1234.0,
            trend_holdings_equity=0.0,
            trend_daily_pnl=-0.0123,
            btc_price=12345.0,
            btc_target_ratio=0.125,
            btc_snapshot={"regime_on": False, "ahr999": 1.234, "zscore": -2.34},
        )
        self.assertEqual(self.events[-1], ("notify", (
            "heartbeat\nstrategy=示例策略 · alpha\n"
            "equity: $-1,234 | trend: $0 (-1.2%)\n"
            "price: $12,345 | gate: off\n"
            "target: 12.5% | AHR999: 1.23 | z: -2.3"
        )))

    def test_disabled_interval_does_not_read_time_or_format(self):
        for interval in (0, -1, -24):
            with self.subTest(interval=interval):
                self.assertIsNone(self.invoke(interval_hours=interval, now_utc=object(), btc_snapshot=object()))
                self.assertEqual(self.events, [])
                self.assertEqual(self.state, {"unrelated": "preserved"})

    def test_unscheduled_and_duplicate_reports_do_not_format(self):
        self.assertIsNone(self.invoke(now_utc=datetime(2026, 10, 31, 7, tzinfo=timezone.utc), btc_snapshot=object()))
        self.state["last_btc_status_report_bucket"] = "2026103108"
        self.assertIsNone(self.invoke(btc_snapshot=object()))
        self.assertEqual(self.events, [])
        self.assertEqual(self.state, {"unrelated": "preserved", "last_btc_status_report_bucket": "2026103108"})

    def test_delivery_acknowledgment_identity_and_bucket_update(self):
        for receipt, acknowledged in (
            (False, False), (True, True), (None, True), (0, True), ([], True),
            ({}, False), ({"transport_acknowledged": True}, True),
            ({"transport_acknowledged": False}, False),
            ({"transport_acknowledged": 1}, False),
            ({"transport_acknowledged": "true"}, False),
        ):
            with self.subTest(receipt=receipt):
                state = {"unrelated": "preserved"}
                self.events.clear()

                def notify(text):
                    self.events.append(("notify", text))
                    return receipt

                self.assertIs(self.invoke(state=state, notifier_fn=notify), acknowledged)
                self.assertEqual(self.events[-1], ("notify", EXPECTED_TEXT))
                expected = {"unrelated": "preserved"}
                if acknowledged:
                    expected["last_btc_status_report_bucket"] = "2026103108"
                self.assertEqual(state, expected)

    def test_translator_exceptions_preserve_identity_and_stop_side_effects(self):
        for failing_key in TRANSLATION_ORDER:
            with self.subTest(failing_key=failing_key):
                self.events.clear()
                error = LookupError("synthetic translation failure")

                def translate(key, **kwargs):
                    self.events.append(("translate", key, kwargs))
                    if key == failing_key:
                        raise error
                    return TRANSLATIONS[key].format(**kwargs)

                with self.assertRaises(LookupError) as observed:
                    self.invoke(translate_fn=translate)
                self.assertIs(observed.exception, error)
                self.assertEqual(self.translated_keys(), TRANSLATION_ORDER[:TRANSLATION_ORDER.index(failing_key) + 1])
                self.assertFalse(any(event[0] == "notify" for event in self.events))
                self.assertEqual(self.state, {"unrelated": "preserved"})

    def test_missing_snapshot_values_fail_at_the_original_format_position(self):
        for missing, expected_keys in (("ahr999", TRANSLATION_ORDER[:-1]), ("zscore", TRANSLATION_ORDER)):
            with self.subTest(missing=missing):
                self.events.clear()
                snapshot = dict(self.arguments["btc_snapshot"])
                del snapshot[missing]
                with self.assertRaises(KeyError) as observed:
                    self.invoke(btc_snapshot=snapshot)
                self.assertEqual(observed.exception.args, (missing,))
                self.assertEqual(self.translated_keys(), expected_keys)
                self.assertFalse(any(event[0] == "notify" for event in self.events))
                self.assertEqual(self.state, {"unrelated": "preserved"})

    def test_numeric_format_error_does_not_send_or_advance_bucket(self):
        with self.assertRaises(TypeError):
            self.invoke(total_equity=object())
        self.assertEqual(self.translated_keys(), TRANSLATION_ORDER[:4])
        self.assertFalse(any(event[0] == "notify" for event in self.events))
        self.assertEqual(self.state, {"unrelated": "preserved"})

    def test_notifier_exception_preserves_identity_and_bucket(self):
        error = ConnectionError("synthetic notification failure")

        def notify(text):
            self.events.append(("notify", text))
            raise error

        with self.assertRaises(ConnectionError) as observed:
            self.invoke(notifier_fn=notify)
        self.assertIs(observed.exception, error)
        self.assertEqual(self.events[-1], ("notify", EXPECTED_TEXT))
        self.assertEqual(self.state, {"unrelated": "preserved"})

    def test_default_route_preserves_kind_callbacks_and_transport_arguments(self):
        fake = ModuleType("notifications.unified_notifications")
        fake._coverage_message = lambda: "fixture coverage"

        def external_notice(send, *, kind, send_coverage):
            self.events.append(("route", kind))
            self.assertEqual(send_coverage(), "coverage receipt")
            return send()

        def send(token, chat, text):
            self.events.append(("transport", token, chat, text))
            if text == "fixture coverage":
                return "coverage receipt"
            return {"transport_acknowledged": True}

        fake.external_notice = external_notice
        with patch.dict(sys.modules, {fake.__name__: fake}):
            self.assertIs(self.invoke(notifier_fn=None, send_tg_msg_fn=send), True)
        self.assertEqual(self.events[-3:], [
            ("route", "healthy_heartbeat"),
            ("transport", "fixture-token", "fixture-chat", "fixture coverage"),
            ("transport", "fixture-token", "fixture-chat", EXPECTED_TEXT),
        ])
        self.assertEqual(self.state, {"unrelated": "preserved", "last_btc_status_report_bucket": "2026103108"})


if __name__ == "__main__":
    unittest.main()
