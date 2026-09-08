import ast
import string
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notify_i18n_support import _TEXTS, build_translator, get_notify_lang
from reporting.status_reports import maybe_send_periodic_btc_status_report


@pytest.mark.parametrize("locale", ["zh-CN", "zh_TW", "ZH-hans", "zh"])
def test_chinese_locale_variants_use_chinese(locale, monkeypatch):
    monkeypatch.setenv("NOTIFY_LANG", locale)
    assert get_notify_lang() == "zh"
    assert build_translator(locale)("heartbeat_title") == _TEXTS["zh"]["heartbeat_title"]


def test_catalog_covers_literal_runtime_keys_and_matching_placeholders():
    root = Path(__file__).resolve().parents[1]
    keys = set()
    for relative in ("main.py", "reporting/status_reports.py", "live_services.py"):
        for node in ast.walk(ast.parse((root / relative).read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"t", "translate_fn"} and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
    assert keys <= set(_TEXTS["en"])
    assert set(_TEXTS["en"]) == set(_TEXTS["zh"])
    formatter = string.Formatter()
    def fields(text):
        return {field for _, field, _, _ in formatter.parse(text) if field}
    for key in _TEXTS["en"]:
        assert fields(_TEXTS["en"][key]) == fields(_TEXTS["zh"][key]), key


@pytest.mark.parametrize("locale", ["zh", "en"])
def test_periodic_summary_is_compact_and_keeps_delivery_dedup(locale):
    messages = []
    state = {}
    def send(text):
        messages.append(text)
        return {"transport_acknowledged": True}
    args = (state, "", "", datetime(2026, 9, 8, tzinfo=timezone.utc), 24,
            1000, 200, .01, 60000,
            {"ahr999": .7, "zscore": 1.2, "sell_trigger": 3, "regime_on": True},
            .3, "Test strategy")
    kwargs = dict(translate_fn=build_translator(locale), separator="---", notifier_fn=send)
    assert maybe_send_periodic_btc_status_report(*args, **kwargs) is True
    assert len(messages[0].splitlines()) <= 5
    assert "Test strategy" in messages[0]
    assert "---" not in messages[0]
    assert "AHR999 偏低" not in messages[0]
    assert "discretionary" not in messages[0]
    maybe_send_periodic_btc_status_report(*args, **kwargs)
    assert len(messages) == 1
