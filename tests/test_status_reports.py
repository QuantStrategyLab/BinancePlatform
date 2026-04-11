import unittest
from types import SimpleNamespace

from reporting.status_reports import (
    append_rotation_summary,
    build_btc_manual_hint,
    get_periodic_report_bucket,
    maybe_send_periodic_btc_status_report,
)


def fake_translate(key, **kwargs):
    if key == "rotation_no_upstream_pool":
        return "no upstream pool"
    if key == "rotation_no_execution_pool":
        return "no execution pool"
    if key == "rotation_no_candidates":
        return "no candidates"
    if key == "rotation_upstream_official_monthly_pool":
        return f"upstream={kwargs['pool_text']}"
    if key == "rotation_current_execution_pool":
        return f"execution={kwargs['pool_text']}"
    if key == "rotation_current_execution_pool_size":
        return f"size={kwargs['pool_size']}"
    if key == "rotation_current_execution_targets":
        return f"targets={kwargs['target_text']}"
    if key == "manual_hint_deep_value":
        return "deep"
    if key == "manual_hint_low_value":
        return "low"
    if key == "manual_hint_profit_taking":
        return "take-profit"
    if key == "manual_hint_near_profit_taking":
        return "near-profit"
    if key == "manual_hint_neutral":
        return "neutral"
    raise KeyError(key)


class StatusReportTests(unittest.TestCase):
    def test_get_periodic_report_bucket_respects_interval(self):
        now_utc = SimpleNamespace(hour=8, strftime=lambda fmt: "20260329")
        self.assertEqual(get_periodic_report_bucket(now_utc, 4), "2026032908")
        self.assertEqual(get_periodic_report_bucket(now_utc, 3), "")

    def test_build_btc_manual_hint_uses_thresholds(self):
        self.assertEqual(
            build_btc_manual_hint(
                {"ahr999": 0.3, "zscore": 1.0, "sell_trigger": 3.0},
                translate_fn=fake_translate,
            ),
            "deep",
        )
        self.assertEqual(
            build_btc_manual_hint(
                {"ahr999": 0.7, "zscore": 1.0, "sell_trigger": 3.0},
                translate_fn=fake_translate,
            ),
            "low",
        )
        self.assertEqual(
            build_btc_manual_hint(
                {"ahr999": 1.2, "zscore": 3.1, "sell_trigger": 3.0},
                translate_fn=fake_translate,
            ),
            "take-profit",
        )

    def test_append_rotation_summary_formats_expected_lines(self):
        log_buffer = []
        append_rotation_summary(
            log_buffer,
            ["ETHUSDT", "SOLUSDT"],
            ["ETHUSDT"],
            {},
            append_log_fn=lambda buffer, text: buffer.append(text),
            translate_fn=fake_translate,
        )
        self.assertEqual(
            log_buffer,
            [
                "upstream=ETHUSDT, SOLUSDT",
                "execution=ETHUSDT",
                "size=1",
                "targets=no candidates",
            ],
        )


    def test_periodic_status_report_includes_strategy_display_name(self):
        state = {}
        observed = []

        def translate_strategy(key, **kwargs):
            mapping = {
                "heartbeat_title": "heartbeat",
                "strategy_label": "strategy={name}",
                "time_utc": "time",
                "total_equity": "equity",
                "trend_equity": "trend",
                "btc_price": "btc",
                "ahr999": "ahr",
                "zscore": "z",
                "zscore_threshold": "threshold",
                "btc_target": "target",
                "btc_gate": "gate",
                "gate_on": "on",
                "gate_off": "off",
                "manual_hint": "hint",
                "manual_hint_low_value": "low",
            }
            template = mapping[key]
            return template.format(**kwargs) if kwargs else template

        maybe_send_periodic_btc_status_report(
            state,
            "token",
            "chat-id",
            SimpleNamespace(hour=8, strftime=lambda fmt: "2026-03-29 08:00" if "%H:%M" in fmt else "20260329"),
            4,
            1000.0,
            250.0,
            0.01,
            65000.0,
            {"ahr999": 0.7, "zscore": 1.2, "sell_trigger": 3.0, "regime_on": True},
            0.3,
            "加密领涨轮动",
            translate_fn=translate_strategy,
            separator="---",
            notifier_fn=observed.append,
        )

        self.assertTrue(observed)
        self.assertIn("strategy=加密领涨轮动", observed[0])


if __name__ == "__main__":
    unittest.main()
