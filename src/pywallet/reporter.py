from __future__ import annotations

import csv
import logging
import typing as t
from argparse import ArgumentParser, BooleanOptionalAction
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from functools import singledispatch
from itertools import batched
from pathlib import Path

from no_log_tears import get_logger
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from pywallet.client import Client, ClientConfig, IncomesExpensesReportOptions, create_client
from pywallet.date import DatePeriod, DatePeriodKind, get_relative_date, get_utc_today, period_range

if t.TYPE_CHECKING:
    from pywallet.money import Money


class Config(ClientConfig, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
    )


@dataclass(frozen=True, kw_only=True)
class IncomesExpensesTableByPeriodsOptions(IncomesExpensesReportOptions):
    start: date
    end: date
    by: DatePeriodKind
    output: Path


type ReportOptions = IncomesExpensesTableByPeriodsOptions


class CLIOptions(BaseModel):
    filter: str | None
    verbose: int
    report: str
    output: Path
    by: DatePeriodKind
    last: int
    since: date | None
    period: tuple[date, date] | None


def parse_report_options_from_args() -> ReportOptions:
    def parse_date_period(value: str) -> tuple[date, date]:
        start_value, _, end_value = value.partition(":")
        return date.fromisoformat(start_value), date.fromisoformat(end_value)

    parser = ArgumentParser(description="Get reports from https://web.budgetbakers.com site.")
    parser.add_argument("--filter", type=str, default=None, help="Specify custom filter on report page.")
    parser.add_argument(
        "--headless",
        action=BooleanOptionalAction,
        default=False,
        help="Enable / disable run in headless mode. Default: %(default)s",
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
        aliases=["ie"],
        help="Generates incomes & expenses report CSV file. "
        "Each date period in separate column and each category in separate row. "
        f"By default generates report for last {ie_default_last} {ie_default_by}.",
    )
    ie_table_by_periods.add_argument("output", type=Path, help="Specify CSV output file.")
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

    ns = CLIOptions.model_validate(parser.parse_args().__dict__)

    get_logger().setLevel(logging.WARNING - ns.verbose * 10)

    if ns.report in {"incomes-expenses-by-periods", "ie"}:
        if ns.period is not None:
            start, end = ns.period

        elif ns.since is not None:
            start, end = ns.since, get_utc_today()

        else:
            end = get_utc_today()
            start = get_relative_date(end, ns.by, -ns.last)

        return IncomesExpensesTableByPeriodsOptions(
            filter_name=ns.filter,
            start=start,
            end=end,
            by=ns.by,
            output=ns.output,
        )

    msg = "unknown report"
    raise ValueError(msg, ns.report)


@singledispatch
def make_report(
    options: ReportOptions,
    config: Config,  # noqa: ARG001
) -> None:
    msg = "unsupported report"
    raise TypeError(msg, options)


@dataclass(frozen=True, kw_only=True)
class IncomesExpensesTableByPeriods:
    @dataclass(frozen=True, kw_only=True)
    class Cell:
        category: str
        period: DatePeriod
        money: Money

    columns: t.Sequence[DatePeriod]
    cells: t.Sequence[Cell]

    def iter_rows(self) -> t.Iterable[t.Sequence[Cell]]:
        return batched(self.cells, len(self.columns))


@make_report.register
def make_incomes_expenses_table_by_months_report(
    options: IncomesExpensesTableByPeriodsOptions,
    config: Config,
) -> None:
    with create_client(config) as client:
        report = get_incomes_expenses_table_by_months_report(client, options)

    dump_csv(report, options.output)


def get_incomes_expenses_table_by_months_report(
    client: Client,
    options: IncomesExpensesTableByPeriodsOptions,
) -> IncomesExpensesTableByPeriods:
    periods = list(period_range(options.start, options.end, "month"))
    if periods[-1].end > options.end:
        periods = periods[:-1]

    reports = {period: client.read_incomes_expenses_report_for_month(period.start, options) for period in periods}

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
    output: Path,  # noqa: ARG001
) -> None:
    msg = "unsupported type"
    raise TypeError(msg, obj)


@dump_csv.register
def dump_csv_incomes_expenses_by_categories_and_periods_report(
    obj: IncomesExpensesTableByPeriods,
    output: Path,
) -> None:
    period_titles = OrderedDict((col, get_period_title(col)) for col in obj.columns)

    with output.open("w") as fd:
        writer = csv.DictWriter(fd, fieldnames=("category", *period_titles.values()))
        writer.writeheader()

        for row in obj.iter_rows():
            writer.writerow(
                {
                    "category": row[0].category,
                    **{period_titles[cell.period]: str(cell.money.amount) for cell in row},
                }
            )


def get_period_title(period: DatePeriod) -> str:
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
    config = Config()

    options = parse_report_options_from_args()

    make_report(options, config)


if __name__ == "__main__":
    main()
