"""적용 창(fleet `window`, manifest에 서명되어 들어온다) 판정.

- `recurring{tz, days, start, end}`: `days`의 각 날 현지 `start`에 열려 `end`에 닫힌다.
  `end ≤ start`면 자정을 넘겨 다음 날 `end`에 닫힌다(시작 요일 기준).
- `once{start, end}`: `start ≤ now < end`(RFC 3339, 시간대 필수).

서비스 영향 단계(`committed` 이후)는 창 안에서만 시작한다. 시작한 뒤에는 창이 닫혀도 끝까지 간다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pureiron_bundle.manifest import Json
from pureiron_bundle.trust import parse_timestamp

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WindowError(ValueError):
    """창 정의를 해석할 수 없다(알 수 없는 시간대 등)."""


def _hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _recurring_open(spec: dict[str, Json], now: datetime) -> bool:
    try:
        zone = ZoneInfo(cast(str, spec["tz"]))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WindowError(f"unknown time zone {spec['tz']!r}") from exc
    days = {_DAYS.index(cast(str, d)) for d in cast(list[Json], spec["days"])}
    start, end = _hhmm(cast(str, spec["start"])), _hhmm(cast(str, spec["end"]))
    local = now.astimezone(zone)
    for back in (0, 1):
        day: date = local.date() - timedelta(days=back)
        if day.weekday() not in days:
            continue
        opens = datetime.combine(day, start, zone)
        closes = datetime.combine(day + timedelta(days=1 if end <= start else 0), end, zone)
        if opens <= local < closes:
            return True
    return False


def window_open(window: Json, now: datetime) -> bool:
    """`window`(manifest 계약 형식)이 `now`(aware)에 열려 있는지. 해석 불가 → `WindowError`."""
    if not isinstance(window, dict):
        raise WindowError("window is not an object")
    if "recurring" in window:
        return _recurring_open(cast(dict[str, Json], window["recurring"]), now)
    if "once" in window:
        once = cast(dict[str, Json], window["once"])
        try:
            start = parse_timestamp(cast(str, once["start"]))
            end = parse_timestamp(cast(str, once["end"]))
        except ValueError as exc:
            raise WindowError(str(exc)) from exc
        return start <= now < end
    raise WindowError("window needs 'recurring' or 'once'")
