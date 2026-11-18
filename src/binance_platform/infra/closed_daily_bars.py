"""Validate a daily-bar time boundary without modifying market values."""

from datetime import datetime, timezone


class ClosedDailyBarsError(ValueError):
    pass


def closed_daily_bars(rows, as_of):
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ClosedDailyBarsError("daily_bar_as_of_invalid")
    cutoff = int(as_of.astimezone(timezone.utc).timestamp() * 1000)
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ClosedDailyBarsError("daily_bars_unavailable")
    closed = []
    previous_close = -1
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            raise ClosedDailyBarsError("daily_bar_timestamp_invalid")
        opened, ended = row[0], row[6]
        if (type(opened) is not int or type(ended) is not int or opened < 0
                or opened % 86400000 != 0 or ended != opened + 86399999
                or (previous_close >= 0 and opened != previous_close + 1) or opened > cutoff):
            raise ClosedDailyBarsError("daily_bar_timestamp_invalid")
        previous_close = ended
        if ended < cutoff:
            closed.append(row)
    if not closed:
        raise ClosedDailyBarsError("closed_daily_bars_unavailable")
    if closed[-1][6] != cutoff // 86400000 * 86400000 - 1:
        raise ClosedDailyBarsError("closed_daily_bars_stale")
    return closed
