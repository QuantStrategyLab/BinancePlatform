"""Send stdin through pinned QPK; credentials and provider errors never leave delivery."""
import os
import sys


def main():
    from quant_platform_kit.notifications import (
        NotificationIntent, install_log_redaction, redact_sensitive_text, send_notification_intent,
    )
    install_log_redaction(wrap_streams=True)
    chat = os.environ.get('QSL_GLOBAL_TELEGRAM_CHAT_ID') or os.environ.get('GLOBAL_TELEGRAM_CHAT_ID', '')
    try:
        intent = NotificationIntent(text=redact_sensitive_text(sys.stdin.read()),
            run_id=os.environ.get('GITHUB_RUN_ID', 'dispatch-guard'), chat_ids=(chat,))
        receipt = send_notification_intent(intent, bot_token_env='TG_TOKEN', timeout=15)
    except (ValueError, TypeError):
        return 1
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
