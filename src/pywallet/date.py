from __future__ import annotations

import typing as t
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cached_property

type DatePeriodKind = t.Literal["day", "week", "month", "quarter", "year"]


@dataclass(frozen=True, kw_only=True)
class DatePeriod:
    kind: DatePeriodKind
    start: date

    @cached_property
    def end(self) -> date:
        return get_relative_date(self.start, self.kind)

    @property
    def previous(self) -> DatePeriod:
        return DatePeriod(kind=self.kind, start=get_relative_date(self.start, self.kind, -1))

    @property
    def next(self) -> DatePeriod:
        return DatePeriod(kind=self.kind, start=self.end)


def get_relative_date(value: date, kind: DatePeriodKind, n: int = 1) -> date:
    if kind == "day":
        return value + timedelta(days=1)

    elif kind == "week":
        return value + timedelta(days=7)

    if kind == "month":
        year, month = divmod(value.year * 12 + value.month - 1 + n, 12)
        return value.replace(year=year, month=month + 1)

    if kind == "quarter":
        quarters, mod = divmod(value.year * 4 + value.month - 1 + n, 4)
        return value.replace(year=quarters * 4, month=mod + 1)

    elif kind == "year":
        return value.replace(year=value.year + n)

    else:
        t.assert_never(kind)


def date_range(start: date, end: date, step: timedelta | DatePeriodKind) -> t.Iterable[date]:
    s = start

    while True:
        e = s + step if isinstance(step, timedelta) else get_relative_date(s, step)

        if e > end:
            break

        yield s
        s = e


def period_range(start: date, end: date, kind: DatePeriodKind) -> t.Iterable[DatePeriod]:
    for period_start in date_range(start, end, kind):
        yield DatePeriod(kind=kind, start=period_start)


def get_utc_now() -> datetime:
    return datetime.now(UTC)


def get_utc_today() -> date:
    return get_utc_now().date()
