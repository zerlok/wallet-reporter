from __future__ import annotations

import logging
import math
import typing as t
from argparse import ArgumentParser, BooleanOptionalAction
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from functools import singledispatch
from itertools import batched
from pathlib import Path

import tqdm
from no_log_tears import get_logger
from pydantic import BaseModel, RootModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from pywallet.client import Client, ClientConfig, IncomesExpensesReportOptions, create_client
from pywallet.csv import use_csv_writer
from pywallet.date import DatePeriod, DatePeriodKind, get_relative_date, get_utc_today, period_range

if t.TYPE_CHECKING:
    from pywallet.money import Money


class Config(ClientConfig, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
    )


class BaseOptions(BaseModel):
    headless: bool
    progress: bool
    verbose: int

    @property
    def logging_level(self) -> int:
        return max(logging.WARNING - self.verbose * 10, logging.DEBUG)


class CSVOptions(BaseModel):
    progress: bool
    date_format: str | None
    delimiter: str | None
    output: Path | None


class PeriodOptions(BaseModel):
    by: DatePeriodKind
    last: int | None
    since: date | None
    period: tuple[date, date] | None

    @property
    def period_range(self) -> t.Iterable[DatePeriod]:
        if self.period is not None:
            start, end = self.period
            return period_range(start, end, self.by)

        elif self.since is not None:
            return period_range(self.since, get_utc_today(), self.by)

        elif self.last is not None:
            end = get_utc_today()
            start = get_relative_date(end, self.by, -self.last)
            return period_range(start, end, self.by)

        else:
            msg = "invalid parameters"
            raise ValueError(msg, self)


class IncomesExpensesTableByPeriodsOptions(BaseOptions, PeriodOptions, CSVOptions):
    filter: str | None


type ReportOptions = IncomesExpensesTableByPeriodsOptions | None


class CLIOptions(RootModel[ReportOptions]):
    pass


def build_parser() -> ArgumentParser:
    def parse_date_period(value: str) -> tuple[date, date]:
        start_value, _, end_value = value.partition(":")
        start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
        if end <= start:
            msg = "invalid date period, end should be greater than start"
            raise ValueError(msg, start, end)

        return start, end

    parser = ArgumentParser(description="Get reports from https://web.budgetbakers.com site.")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Disable all outputs. Default: %(default)s",
    )
    parser.add_argument(
        "--headless",
        action=BooleanOptionalAction,
        default=True,
        help="Show selenium browser window. Default: %(default)s",
    )
    parser.add_argument(
        "--progress",
        action=BooleanOptionalAction,
        default=True,
        help="Display progress bar. Default: %(default)s",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable verbose / debug messages.",
    )

    sub = parser.add_subparsers(dest="report", help="Choose report kind.")

    ie_default_last = 1
    ie_default_by: DatePeriodKind = "month"
    ie_table_by_periods = sub.add_parser(
        "incomes-expenses-by-periods",
        aliases=["iep"],
        help="Generates incomes & expenses report CSV file. "
        "Each date period in separate column and each category in separate row. "
        f"By default generates report for last {ie_default_last} {ie_default_by}.",
    )
    ie_table_by_periods.add_argument("-o", "--output", type=Path, default=None, help="Specify CSV output file.")
    ie_table_by_periods.add_argument("--filter", type=str, default=None, help="Specify custom filter on report page.")
    ie_table_by_periods.add_argument("--date-format", type=str, default=None, help="Specify custom date format.")
    ie_table_by_periods.add_argument("--delimiter", type=str, default=None, help="Specify custom date format.")
    ie_table_by_periods.add_argument(
        "-b",
        "--by",
        choices=t.get_args(DatePeriodKind.__value__),
        default=ie_default_by,
        help="Specify date period kind for report. Default: %(default)s.",
    )
    period_group = ie_table_by_periods.add_mutually_exclusive_group()
    period_group.add_argument(
        "-n",
        "--last",
        type=int,
        default=1,
        help="Generate report for last specified amount of periods (e.g. for last 3 months or last 2 years). "
        "Default: %(default)s",
    )
    period_group.add_argument(
        "-s",
        "--since",
        type=date.fromisoformat,
        default=None,
        help="Generate report since the specified date and until today.",
    )
    period_group.add_argument(
        "-p",
        "--period",
        type=parse_date_period,
        default=None,
        help="Specify exact date period to generate report for.",
    )

    return parser


@singledispatch
def make_report(options: ReportOptions) -> None:
    msg = "unsupported report"
    raise TypeError(msg, options)


@make_report.register
def make_incomes_expenses_table_by_months_report(options: IncomesExpensesTableByPeriodsOptions) -> None:
    config = Config(browser_headless=options.headless, login_progress=options.progress)

    with create_client(config) as client:
        report = get_incomes_expenses_table_by_months_report(client, options)

    dump_csv(report, options)


@dataclass(frozen=True, kw_only=True)
class IncomesExpensesTableByPeriods:
    @dataclass(frozen=True, kw_only=True)
    class Cell:
        category: str
        period: DatePeriod
        money: Money

    columns: t.Sequence[DatePeriod]
    cells: t.Sequence[Cell]

    @property
    def size(self) -> tuple[int, int]:
        return len(self.columns), int(math.ceil(len(self.cells) / len(self.columns)))

    def iter_rows(self) -> t.Iterable[t.Sequence[Cell]]:
        return batched(self.cells, len(self.columns))


def get_incomes_expenses_table_by_months_report(
    client: Client,
    options: IncomesExpensesTableByPeriodsOptions,
) -> IncomesExpensesTableByPeriods:
    periods = list(options.period_range)

    reports = {
        period: client.read_incomes_expenses_report_for_month(
            month=period.start,
            options=IncomesExpensesReportOptions(filter_name=options.filter),
        )
        for period in tqdm.tqdm(periods, desc="reading reports", disable=not options.progress)
    }

    categories = OrderedDict[str, int]()
    for report in reports.values():
        for row in report.incomes.values():
            categories[row.category] = 1
        for row in report.expenses.values():
            categories[row.category] = -1

    def get_money_by_category(period: DatePeriod, cat: str, sign: int) -> Money:
        if sign > 0:
            return reports[period].incomes[cat].total

        elif sign < 0:
            return reports[period].expenses[cat].total

        else:
            raise ValueError(cat, sign)

    return IncomesExpensesTableByPeriods(
        columns=periods,
        cells=[
            IncomesExpensesTableByPeriods.Cell(
                category=cat,
                period=period,
                money=get_money_by_category(period, cat, sign),
            )
            for cat, sign in categories.items()
            for period in periods
        ],
    )


@singledispatch
def dump_csv(
    obj: object,
    options: CSVOptions,  # noqa: ARG001
) -> None:
    msg = "unsupported type"
    raise TypeError(msg, obj)


@dump_csv.register
def dump_csv_incomes_expenses_by_categories_and_periods_report(
    obj: IncomesExpensesTableByPeriods,
    options: CSVOptions,
) -> None:
    period_titles = OrderedDict((col, get_period_title(col, options)) for col in obj.columns)

    with use_csv_writer(
        dest=options.output,
        fieldnames=("category", *period_titles.values()),
        delimiter=options.delimiter,
    ) as writer:
        for row in tqdm.tqdm(obj.iter_rows(), desc="writing csv", total=obj.size[1], disable=not options.progress):
            writer.writerow(
                {
                    "category": row[0].category,
                    **{period_titles[cell.period]: str(cell.money.amount) for cell in row},
                }
            )


def get_period_title(period: DatePeriod, options: CSVOptions) -> str:
    if options.date_format is not None:
        return options.date_format.format(period)

    if period.kind == "day":
        return f"{period.start:%Y.%m.%d}"

    elif period.kind == "week":
        return f"{period.start:%Y.%m.%d}-{period.end:%Y.%m.%d}"

    elif period.kind == "month":
        return f"{period.start:%Y.%m}"

    elif period.kind == "quarter":
        return f"{period.start:%Y.%m}-{period.end:%Y.%m}"

    elif period.kind == "year":
        return f"{period.start:%Y}"

    else:
        t.assert_never(period.kind)


def main() -> None:
    options = CLIOptions.model_validate(build_parser().parse_args().__dict__).root

    if options is None:
        msg = "invalid options"
        raise ValueError(msg)

    get_logger(__name__).setLevel(options.logging_level)
    make_report(options)


if __name__ == "__main__":
    main()
