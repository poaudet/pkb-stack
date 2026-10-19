from datetime import date

import pytest

from hub.capture.dates import resolve, resolve_weekday

SUNDAY = date(2026, 10, 18)


@pytest.mark.parametrize("phrase,expected", [
    ("Tuesday", "2026-10-20"),
    ("Thursday", "2026-10-22"),
    ("Sunday", "2026-10-25"),
    ("this Sunday", "2026-10-18"),
    ("next Tuesday", "2026-10-20"),
    ("next Sunday", "2026-10-25"),
    ("today", "2026-10-18"),
    ("tomorrow", "2026-10-19"),
    ("Friday 23 October", "2026-10-23"),
    ("2 November", "2026-11-02"),
    ("1 January", "2027-01-01"),
    ("mardi", "2026-10-20"),
])
def test_resolution_against_a_sunday(phrase, expected):
    assert resolve(phrase, SUNDAY)[0] == expected


def test_weekday_after_a_sunday_is_never_the_same_day():
    assert resolve_weekday("tuesday", SUNDAY) == date(2026, 10, 20)
    assert resolve_weekday("monday", SUNDAY) == date(2026, 10, 19)


def test_text_loses_weekday_words():
    text, found = resolve("Review on Tuesday at 14:00, then Thursday.", SUNDAY)
    assert text == "Review on 2026-10-20 at 14:00, then 2026-10-22."
    assert [iso for _p, iso in found] == ["2026-10-20", "2026-10-22"]


def test_plain_text_is_untouched():
    assert resolve("12 eggs at 9:30", SUNDAY)[0] == "12 eggs at 9:30"
