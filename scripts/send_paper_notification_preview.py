#!/usr/bin/env python3
"""Send a bounded Binance PAPER Telegram notification preview pack.

Renders synthetic compact messages via existing notify localization and the
Telegram sender in live_services. Does not trade, read Binance accounts,
quotes, or orders, deploy, invoke Cloud Run/production runtime, or change
configuration.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_services import send_tg_msg  # noqa: E402
from notify_i18n_support import build_strategy_display_name, build_translator  # noqa: E402

_MAX_PREVIEW_MESSAGES = 6
_PREVIEW_STRATEGY_PROFILE = "crypto_equity_combo"
_PREVIEW_EXTRA_LINES = (
    "🧪 【PREVIEW】PAPER notification preview",
    "synthetic / 合成样例 · 不会下单 · No order will be placed",
)


def _resolve_locale(raw: str | None = None) -> str:
    value = str(raw or os.environ.get("QSL_NOTIFY_LANG") or os.environ.get("NOTIFY_LANG") or "zh")
    value = value.strip().lower()
    return "en" if value.startswith("en") else "zh"


def _split_chat_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [
        part.strip()
        for part in str(raw).replace(";", ",").replace("\n", ",").split(",")
        if part.strip()
    ]


def resolve_telegram_token() -> str:
    return (os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "").strip()


def resolve_telegram_chat_id() -> str:
    chats = _split_chat_ids(
        os.environ.get("QSL_GLOBAL_TELEGRAM_CHAT_ID")
        or os.environ.get("GLOBAL_TELEGRAM_CHAT_ID")
    )
    return chats[0] if chats else ""


def _with_preview_markers(body: str) -> str:
    return "\n".join(("[PAPER]", body, *_PREVIEW_EXTRA_LINES))


def build_preview_messages(*, locale: str | None = None) -> list[str]:
    """Build at most six synthetic compact PAPER preview messages."""

    translator = build_translator(_resolve_locale(locale))
    strategy_name = build_strategy_display_name(translator)(
        _PREVIEW_STRATEGY_PROFILE,
        fallback_name="Crypto Equity Combo",
    )

    heartbeat = "\n".join(
        (
            translator("heartbeat_title"),
            translator("strategy_label", name=strategy_name),
            f"{translator('total_equity')}: $0 | {translator('trend_equity')}: $0 (+0.0%)",
            f"{translator('btc_price')}: $0 | {translator('btc_gate')}: {translator('gate_off')}",
            f"{translator('btc_target')}: 0.0% | AHR999: 0.00 | {translator('zscore')}: 0.0",
        )
    )

    execution_summary = "\n".join(
        (
            translator("rebalance_title"),
            translator("strategy_label", name=strategy_name),
            translator("trend_buy_skipped"),
            f"{translator('reason_label')}: synthetic PREVIEW execution summary",
            translator("qty_zero_msg"),
        )
    )

    runtime_error = "\n".join(
        (
            translator("runtime_error_title"),
            translator("strategy_label", name=strategy_name),
            translator("runtime_error_reason_recovery_not_active"),
            translator("runtime_error_result"),
            translator("runtime_error_action"),
        )
    )

    rejected = "\n".join(
        (
            translator("trend_buy_failed"),
            translator("strategy_label", name=strategy_name),
            f"{translator('error_label')}: synthetic PREVIEW reject",
            translator("usdt_unavailable_for_trend_buy"),
        )
    )

    unknown_status = "\n".join(
        (
            translator("api_error"),
            translator("strategy_label", name=strategy_name),
            f"{translator('reason_label')}: synthetic unknown status / 未知状态 PREVIEW",
            translator("runtime_error_reason_generic"),
        )
    )

    skipped = "\n".join(
        (
            translator("circuit_breaker_sell_skipped"),
            translator("strategy_label", name=strategy_name),
            f"{translator('reason_label')}: synthetic PREVIEW skip",
            translator("asset_unavailable_for_circuit_breaker_sell", asset="BTC"),
        )
    )

    messages = [
        _with_preview_markers(heartbeat),
        _with_preview_markers(execution_summary),
        _with_preview_markers(runtime_error),
        _with_preview_markers(rejected),
        _with_preview_markers(unknown_status),
        _with_preview_markers(skipped),
    ]
    if len(messages) > _MAX_PREVIEW_MESSAGES:
        raise RuntimeError(
            f"preview message count {len(messages)} exceeds cap {_MAX_PREVIEW_MESSAGES}"
        )
    return messages


def _delivery_acknowledged(receipt: object) -> bool:
    if isinstance(receipt, dict):
        return receipt.get("transport_acknowledged") is True
    return receipt is True


def send_preview(*, locale: str | None = None, send_fn=None) -> bool:
    messages = build_preview_messages(locale=locale)
    token = resolve_telegram_token()
    chat_id = resolve_telegram_chat_id()
    if not token or not chat_id:
        print(
            "Notification preview not sent: Telegram target is not configured.",
            file=sys.stderr,
        )
        return False

    sender = send_fn or (lambda text: send_tg_msg(token, chat_id, text))
    for message in messages:
        if not _delivery_acknowledged(sender(message)):
            print("Notification preview delivery failed.", file=sys.stderr)
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a bounded Binance PAPER Telegram notification preview pack."
    )
    parser.add_argument(
        "--locale",
        default=os.environ.get("NOTIFY_LANG"),
        help="Optional notification locale override (zh/en). Defaults to NOTIFY_LANG.",
    )
    args = parser.parse_args(argv)

    # Fail closed: this path never enables a production runtime target.
    if (os.environ.get("RUNTIME_TARGET_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        print(
            "Notification preview refused: RUNTIME_TARGET_ENABLED must stay disabled.",
            file=sys.stderr,
        )
        return 1

    delivered = send_preview(locale=args.locale)
    if not delivered:
        return 1
    print(
        "Notification preview delivered bounded synthetic PAPER pack "
        f"(at most {_MAX_PREVIEW_MESSAGES} messages; no orders)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
