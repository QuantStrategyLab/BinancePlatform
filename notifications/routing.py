"""Notification facade; OFF preserves the e632 release's direct sends."""

from .digest_snapshot import digest_routing as legacy
from . import unified_notifications as unified


def daily_digest_enabled(mode=None):
    if not unified.enabled():
        return False
    return legacy.daily_digest_enabled(unified.routing_mode(mode))


def routine_notification(sender, runtime, report, text, *, routine_order=True):
    if unified.enabled():
        return unified.routine_notice(sender, runtime, report, text, routine_order=routine_order)
    return sender(runtime, report, text)


def immediate_notification(sender, runtime, report, text, *, reason, kind="real_error", severity=1):
    if not unified.enabled():
        return sender(runtime, report, text)
    token = unified._ORDER_SEND.set(True)
    try:
        digest = unified.current_order_digest(runtime)
        if digest and kind in {"real_error", "order_status_unknown"}:
            reason += ":" + digest
        return unified.route(lambda: sender(runtime, report, text), kind=kind, reason=reason,
            severity=severity, runtime=runtime, report=report)
    finally:
        unified._ORDER_SEND.reset(token)
