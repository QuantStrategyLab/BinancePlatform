from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import send_paper_notification_preview as preview

WORKFLOW = (ROOT / ".github/workflows/paper-notification-preview.yml").read_text(
    encoding="utf-8"
)
SCRIPT_PATH = ROOT / "scripts" / "send_paper_notification_preview.py"

_FORBIDDEN_IMPORT_ROOTS = (
    "binance",
    "main",
    "application",
    "strategy_runtime",
    "run_cycle_replay",
    "live_risk_authority",
    "market_snapshot_support",
    "trade_state_support",
)


def _classify_preview_text(text: str) -> str | None:
    if "熔断卖出跳过" in text or "Circuit Breaker Sell Skipped" in text:
        return "skipped"
    if "趋势买入失败" in text or "Trend Buy Failed" in text:
        return "rejected"
    if "策略运行失败" in text or "strategy run failed" in text.lower():
        return "error"
    if "API 连接失败" in text or "API Connection Failed" in text or "未知状态" in text:
        return "unknown_status"
    if "加密调仓" in text or "Crypto Trade" in text:
        return "strategy_execution_summary"
    if "加密状态" in text or "Crypto Status" in text:
        return "heartbeat"
    return None


def test_build_preview_messages_covers_required_categories_with_safe_markers():
    messages = preview.build_preview_messages(locale="zh")
    assert 1 <= len(messages) <= 6
    assert len(messages) == 6

    categories = {_classify_preview_text(message) for message in messages}
    assert categories == {
        "heartbeat",
        "strategy_execution_summary",
        "error",
        "rejected",
        "unknown_status",
        "skipped",
    }

    for message in messages:
        assert message.startswith("[PAPER]")
        assert "PREVIEW" in message
        assert "synthetic" in message.lower() or "合成" in message
        assert "不会下单" in message or "No order will be" in message
        for forbidden in (
            "api.telegram.org",
            "https://",
            "Traceback",
            "BINANCE_API",
            "secret-token",
        ):
            assert forbidden not in message


def test_send_preview_calls_sender_once_per_message_without_execution_imports(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "token-preview")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "chat-preview")
    monkeypatch.setenv("NOTIFY_LANG", "zh")
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")

    before_modules = {
        name
        for name in sys.modules
        if any(name == root or name.startswith(f"{root}.") for root in _FORBIDDEN_IMPORT_ROOTS)
    }

    sent = []

    def fake_send(text):
        sent.append(text)
        return {"transport_acknowledged": True, "delivery_status": "sent"}

    delivered = preview.send_preview(send_fn=fake_send)

    assert delivered is True
    assert len(sent) == 6
    assert len(sent) <= 6

    for text in sent:
        assert text.startswith("[PAPER]")
        assert "PREVIEW" in text
        assert "token-preview" not in text
        assert "chat-preview" not in text

    categories = {_classify_preview_text(text) for text in sent}
    assert categories == {
        "heartbeat",
        "strategy_execution_summary",
        "error",
        "rejected",
        "unknown_status",
        "skipped",
    }

    after_modules = {
        name
        for name in sys.modules
        if any(name == root or name.startswith(f"{root}.") for root in _FORBIDDEN_IMPORT_ROOTS)
    }
    assert after_modules == before_modules


def test_preview_script_has_no_broker_or_execution_imports():
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)

    for forbidden in _FORBIDDEN_IMPORT_ROOTS:
        assert forbidden not in imported
        assert not any(
            name == forbidden or name.startswith(f"{forbidden}.") for name in imported
        )

    assert "live_services" in imported
    assert "notify_i18n_support" in imported


def test_send_preview_fails_closed_without_telegram_target(monkeypatch):
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("GLOBAL_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("QSL_GLOBAL_TELEGRAM_CHAT_ID", raising=False)
    sent = []
    assert preview.send_preview(send_fn=lambda text: sent.append(text) or True) is False
    assert sent == []


def test_main_refuses_enabled_runtime_target(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "true")
    monkeypatch.setenv("TG_TOKEN", "token-preview")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "chat-preview")
    assert preview.main([]) == 1


def test_main_returns_nonzero_when_delivery_fails(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("TG_TOKEN", "token-preview")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "chat-preview")
    monkeypatch.setattr(preview, "send_preview", lambda **_kwargs: False)
    assert preview.main([]) == 1


def test_workflow_static_safety_constraints():
    assert "name: PAPER Notification Preview" in WORKFLOW
    assert "workflow_dispatch:" in WORKFLOW
    assert "schedule:" not in WORKFLOW
    assert "workflow_run:" not in WORKFLOW
    assert "scripts/send_paper_notification_preview.py" in WORKFLOW
    assert 'RUNTIME_TARGET_ENABLED: "false"' in WORKFLOW
    assert "secrets.TG_TOKEN" in WORKFLOW
    assert "vars.GLOBAL_TELEGRAM_CHAT_ID" in WORKFLOW
    assert "uv sync --frozen --no-dev" in WORKFLOW
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in WORKFLOW
    assert "environment: binance-runtime" not in WORKFLOW

    for forbidden in (
        "gcloud run deploy",
        "gcloud run services",
        "gcloud run jobs",
        "gcloud scheduler",
        "Cloud Run",
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "continue-on-error: true",
        "strategy_profile",
        "main.py",
        "application/",
    ):
        assert forbidden not in WORKFLOW
