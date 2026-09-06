from __future__ import annotations

from datetime import date, timedelta

import exchange_calendars as xcals


CALENDAR_BY_ASSET = {
    "sp500": "XNYS",
    "nasdaq": "XNYS",
    "us_banks": "XNYS",
    "nikkei": "XTKS",
    "eurostoxx": "XETR",
}


def sessions_after(asset_id: str, after: date, count: int) -> list[date]:
    """Return actual exchange sessions strictly after ``after``.

    Exchange-backed holdings use their venue calendar. Currency series use
    weekdays because ECB reference rates are not exchange sessions.
    """
    if count < 1:
        return []
    calendar_name = CALENDAR_BY_ASSET.get(asset_id)
    if not calendar_name:
        days: list[date] = []
        cursor = after
        while len(days) < count:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                days.append(cursor)
        return days

    calendar = xcals.get_calendar(calendar_name)
    start = after + timedelta(days=1)
    end = after + timedelta(days=max(30, count * 5))
    sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
    values = [stamp.date() for stamp in sessions[:count]]
    if len(values) != count:
        raise ValueError(
            f"could not resolve {count} {calendar_name} sessions after {after}"
        )
    return values

