"""Telegram and unified notification helpers for runtime routes."""


# ruff: noqa: F403, F405
from __future__ import annotations


import runtime_wiring as rw
from runtime_wiring import *  # noqa: F403, F401

def send_tg_msg(token, chat_id, text):
    return live_send_tg_msg(token, chat_id, text)
def _runtime_error_notification_message(_exc):
    strategy_name = build_strategy_display_name(t)(os.getenv("STRATEGY_PROFILE", ""))
    reason = _runtime_setup_reason(_exc)
    reason_key = _RUNTIME_ERROR_NOTIFICATION_REASONS.get(
        reason, "runtime_error_reason_generic"
    )
    return "\n".join(
        (
            t("runtime_error_title"),
            t("strategy_label", name=strategy_name) if strategy_name else "",
            t(reason_key),
            t("runtime_error_result"),
            t("runtime_error_action"),
        )
    )
def _notify_runtime_error(exc):
    token = os.getenv("TG_TOKEN", "")
    chat_id = os.getenv("QSL_GLOBAL_TELEGRAM_CHAT_ID") or os.getenv("GLOBAL_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(t("runtime_notification_missing_target"))
        return False
    try:
        from notifications.unified_notifications import external_notice
        receipt = external_notice(lambda: send_tg_msg(token, chat_id, _runtime_error_notification_message(exc)),
            kind="real_error", reason="runtime_failure")
    except Exception:
        print(t("runtime_notification_delivery_failed"))
        return False
    if isinstance(receipt, dict):
        return receipt.get("transport_acknowledged") is True
    return receipt is True
def log_and_notify(log_buffer, tg_token, tg_chat_id, text):
    append_log(log_buffer, text)
    from notifications.unified_notifications import external_notice
    external_notice(lambda: send_tg_msg(tg_token, tg_chat_id, text), kind="real_error", reason="runtime_failure")
def _send_routine_tg_message(token, chat_id, text):
    from notifications.unified_notifications import _coverage_message, external_notice
    return external_notice(lambda: send_tg_msg(token, chat_id, text),
        send_coverage=lambda: send_tg_msg(token, chat_id, _coverage_message()))
def _send_failure_tg_message(token, chat_id, text):
    from notifications.unified_notifications import external_notice
    return external_notice(lambda: send_tg_msg(token, chat_id, text),
        kind="real_error", reason="earn_redeem_failed")
