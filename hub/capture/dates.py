"""Deterministic resolution of relative and partial dates against the capture date."""
from __future__ import annotations

import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
    "samedi": 5, "dimanche": 6,
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
}
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MO = "|".join(sorted(MONTHS, key=len, reverse=True))
_PATTERN = re.compile(
    rf"\b(?:(?P<q>next|this|prochain|ce)\s+)?(?P<wd>{_WD})\b"
    rf"(?:,?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th|er)?\s+(?P<m1>{_MO})\b(?:\s+(?P<y1>\d{{4}}))?)?"
    r"|\b(?P<rel>today|tomorrow|yesterday|aujourd'hui|demain|hier)\b"
    rf"|\b(?P<d2>\d{{1,2}})(?:st|nd|rd|th|er)?\s+(?P<m2>{_MO})\b(?:\s+(?P<y2>\d{{4}}))?",
    re.IGNORECASE,
)
_REL = {"today": 0, "aujourd'hui": 0, "tomorrow": 1, "demain": 1, "yesterday": -1, "hier": -1}


def today_in(tz: str) -> date:
    from datetime import datetime
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid timezone") from exc


def resolve_weekday(name: str, today: date, qualifier: str | None = None) -> date:
    """Bare weekday: the next such day after today. 'this': on or after today.
    'next': the first such day in the following Monday-Sunday week."""
    target = WEEKDAYS[name.casefold()]
    delta = (target - today.weekday()) % 7
    q = (qualifier or "").casefold()
    if q in ("this", "ce"):
        return today + timedelta(days=delta)
    first = delta or 7
    if q in ("next", "prochain") and today.weekday() + first < 7:
        first += 7
    return today + timedelta(days=first)


def _month_day(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        resolved = date(today.year, month, day)
        return resolved if resolved >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def resolve(text: str, today: date) -> tuple[str, list[tuple[str, str]]]:
    """Replace date phrases with ISO dates. Returns (rewritten text, [(phrase, iso)])."""
    found: list[tuple[str, str]] = []

    def sub(match: re.Match) -> str:
        resolved: date | None = None
        if match.group("wd"):
            if match.group("d1"):
                resolved = _month_day(
                    int(match.group("d1")), MONTHS[match.group("m1").casefold()],
                    int(match.group("y1")) if match.group("y1") else None, today,
                )
            if resolved is None:
                resolved = resolve_weekday(match.group("wd"), today, match.group("q"))
        elif match.group("rel"):
            resolved = today + timedelta(days=_REL[match.group("rel").casefold()])
        else:
            resolved = _month_day(
                int(match.group("d2")), MONTHS[match.group("m2").casefold()],
                int(match.group("y2")) if match.group("y2") else None, today,
            )
        if resolved is None:
            return match.group(0)
        found.append((match.group(0), resolved.isoformat()))
        return resolved.isoformat()

    return _PATTERN.sub(sub, text), found
