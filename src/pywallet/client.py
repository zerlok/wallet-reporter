from __future__ import annotations

import re
import typing as t
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path

from no_log_tears import LogMixin, get_logger
from pydantic import BaseModel, Field, RootModel, SecretStr
from selenium import webdriver
from selenium.webdriver.chromium.options import ChromiumOptions
from selenium.webdriver.chromium.webdriver import ChromiumDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.wait import WebDriverWait
from yarl import URL

from pywallet.date import DatePeriod
from pywallet.money import Money

if t.TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver
    from selenium.webdriver.remote.webelement import WebElement


class ClientConfig(BaseModel):
    budgetbackers_host: str = "web.budgetbakers.com"
    budgetbackers_email: str
    budgetbackers_password: SecretStr

    browser_driver: t.Literal["firefox", "chrome", "chromium", "edge", "ie", "safari"] = "firefox"
    browser_page_implicit_wait: timedelta = timedelta(seconds=30)
    browser_headless: bool = True
    browser_data_dir: Path = Path.cwd() / ".local" / "pywallet" / "browser"
    browser_args: t.Sequence[str] = Field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True)
class IncomesExpensesReport:
    @dataclass(frozen=True, kw_only=True)
    class Row:
        category: str
        total: Money

    title: str
    total: Money
    period: DatePeriod
    incomes: t.Mapping[str, Row]
    expenses: t.Mapping[str, Row]


@dataclass(frozen=True, kw_only=True)
class IncomesExpensesReportOptions:
    filter_name: str | None = None


@cache
def _get_money_pattern() -> t.Pattern[str]:
    return re.compile(r"^(?P<sign>[+-])?(?P<currency>\D+)(?P<amount>[0-9,.]+)$")


def parse_money(value: str) -> Money:
    match = _get_money_pattern().match(value)
    if match is None:
        msg = "value doesn't match money pattern"
        raise ValueError(msg, value)

    return Money(
        currency=match.group("currency"),
        amount=Decimal((match.group("sign") or "") + match.group("amount").replace(",", "")),
    )


class BrowserCookies(RootModel[t.Sequence[t.Mapping[str, object]]]):
    pass


class Client(LogMixin):
    def __init__(self, base_url: URL, driver: WebDriver) -> None:
        self.__base_url = base_url
        self.__driver = driver

    def load_cookies(self, path: Path) -> None:
        log = self._log(path=path)
        log.debug("loading cookies")

        if not path.is_file():
            log.warning("cookies file path is not valid")
            return

        self.__open_url()

        for cookie in BrowserCookies.model_validate_json((path / "cookies.json").read_bytes()).root:
            self.__driver.add_cookie(cookie)

        self.__driver.refresh()

        log.info("cookies loaded")

    def dump_cookies(self, path: Path) -> None:
        log = self._log(path=path)

        path.mkdir(parents=True, exist_ok=True)
        (path / "cookies.json").write_text(BrowserCookies(root=self.__driver.get_cookies()).model_dump_json())

        log.info("cookies dumped")

    def login(self, email: str, password: str) -> None:
        log = self._log(email=email)
        log.debug("trying to log in")

        self.__open_url("login")

        login_form = self.__driver.find_element(By.XPATH, "/html/body/div[1]/div/div/section/div/form")
        if "Log In" not in login_form.text:
            msg = "login form was not found"
            raise RuntimeError(msg, login_form.text)

        email_el = login_form.find_element(By.NAME, "email")
        password_el = login_form.find_element(By.NAME, "password")
        login_btn = login_form.find_element(By.XPATH, ".//button[@type='submit']")

        email_el.send_keys(email)
        password_el.send_keys(password)
        login_btn.click()

        log.info("logged in")

    def read_incomes_expenses_report_for_month(
        self,
        month: date,
        options: IncomesExpensesReportOptions | None = None,
    ) -> IncomesExpensesReport:
        log = self._log(month=month)
        log.debug("trying to read incomes expenses report for month")

        analytics_btn = WebDriverWait(self.__driver, 30.0).until(
            ec.presence_of_element_located((By.XPATH, "//a[@href='/analytics']"))
        )
        analytics_btn.click()

        self.__analytics_select_incomes_expenses_report()
        self.__analytics_select_month(month)
        log.debug("month selected")

        if options is not None:
            self.__analytics_select_filter_name(options.filter_name)
            log.debug("options applied", options=options)

        report_container = self.__driver.find_element(By.CLASS_NAME, "report-content")
        (incomes_report_table, expenses_report_table) = report_container.find_elements(By.CLASS_NAME, "report-table")

        title = report_container.find_element(By.CLASS_NAME, "totals").find_element(By.CLASS_NAME, "title").text
        log.debug("title was parsed", title=title)

        total_value = report_container.find_element(By.CLASS_NAME, "totals").find_element(By.CLASS_NAME, "value").text

        report = IncomesExpensesReport(
            title=title,
            total=parse_money(total_value),
            period=DatePeriod(kind="month", start=month),
            incomes=self.__analytics_read_incomes_expenses_rows(incomes_report_table),
            expenses=self.__analytics_read_incomes_expenses_rows(expenses_report_table),
        )

        log.info("incomes expenses report for month was read", report_title=report.title)

        return report

    def __open_url(self, *parts: str) -> None:
        url = str(self.__base_url.with_scheme("https").joinpath(*parts))
        self.__driver.get(url)
        self._log.debug("url was opened", url=url)

    def __analytics_read_incomes_expenses_rows(
        self,
        table: WebElement,
    ) -> OrderedDict[str, IncomesExpensesReport.Row]:
        rows = OrderedDict[str, IncomesExpensesReport.Row]()

        for table_row in table.find_elements(By.CLASS_NAME, "report-row-values"):
            category_span = table_row.find_element(By.CLASS_NAME, "category-name")
            amount = table_row.find_element(By.XPATH, "./td[2]/strong/span")

            row = IncomesExpensesReport.Row(
                category=category_span.text,
                total=parse_money(amount.text),
            )
            rows[row.category] = row

        return rows

    def __analytics_select_incomes_expenses_report(self) -> None:
        pass

    def __analytics_select_month(self, month: date) -> None:
        date_range_picker = self.__driver.find_element(By.CLASS_NAME, "date-range-picker")
        date_range_picker.click()

        date_range_picker_container = self.__driver.find_element(By.CLASS_NAME, "date-range-picker-container")
        months_btn = date_range_picker_container.find_element(By.XPATH, ".//*[text()='Months']")
        months_btn.click()

        date_range_month_picker_container = date_range_picker_container.find_element(
            By.CLASS_NAME,
            "date-range-picker-month-year-content",
        )
        date_range_year = None

        while date_range_year != month.year:
            date_range_year_name = date_range_month_picker_container.find_element(By.XPATH, "./div/div")
            date_range_year = int(date_range_year_name.text)

            if date_range_year > month.year:
                change_range = date_range_month_picker_container.find_element(By.CLASS_NAME, "left")
                change_range.click()

            elif date_range_year < month.year:
                change_range = date_range_month_picker_container.find_element(By.CLASS_NAME, "right")
                change_range.click()

            else:
                break

        month_el = date_range_month_picker_container.find_element(By.XPATH, f"./ul/li[{month.month}]")
        month_el.click()

    def __analytics_select_filter_name(self, name: str | None) -> None:
        if name:
            filter_selector = self.__driver.find_element(By.NAME, "selectFilter")
            filter_selector.click()

            specific_filter = filter_selector.find_element(By.XPATH, f".//*[text()='{name}']")
            specific_filter.click()


@contextmanager
def create_client(config: ClientConfig) -> t.Iterator[Client]:
    log = get_logger()(config=config)
    log.debug("creating the client")

    with create_webdriver(config) as driver:
        log.debug("webdriver was created", driver=driver)

        driver.implicitly_wait(config.browser_page_implicit_wait.total_seconds())

        client = Client(URL.build(host=config.budgetbackers_host), driver)
        log.debug("client was created", client=client)

        client.login(config.budgetbackers_email, config.budgetbackers_password.get_secret_value())

        yield client

        log.debug("destroying the webdriver", driver=driver)

    log.debug("client was destroyed")


def create_webdriver(config: ClientConfig) -> WebDriver:
    if config.browser_driver == "firefox":
        return create_firefox_webdriver(config)

    elif config.browser_driver == "chrome":
        return create_chrome_webdriver(config)

    elif config.browser_driver == "chromium":
        return create_chromium_webdriver(config)

    elif config.browser_driver == "edge":
        return create_edge_webdriver(config)

    elif config.browser_driver == "ie":
        return create_ie_webdriver(config)

    elif config.browser_driver == "safari":
        return create_safari_webdriver(config)

    else:
        t.assert_never(config.browser_driver)


def create_firefox_webdriver(config: ClientConfig) -> WebDriver:
    options = webdriver.FirefoxOptions()
    options.add_argument(f"--user-data-dir={config.browser_data_dir.absolute()}")
    if config.browser_headless:
        options.add_argument("--headless")
    for arg in config.browser_args:
        options.add_argument(arg)
    return webdriver.Firefox(options=options)


def create_chrome_webdriver(config: ClientConfig) -> WebDriver:
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={config.browser_data_dir.absolute()}")
    if config.browser_headless:
        options.add_argument("--headless")
    for arg in config.browser_args:
        options.add_argument(arg)

    return webdriver.Chrome(options=options)


def create_chromium_webdriver(config: ClientConfig) -> WebDriver:
    options = ChromiumOptions()
    options.add_argument(f"--user-data-dir={config.browser_data_dir.absolute()}")
    if config.browser_headless:
        options.add_argument("--headless")
    for arg in config.browser_args:
        options.add_argument(arg)

    return ChromiumDriver(options=options)


def create_edge_webdriver(config: ClientConfig) -> WebDriver:
    options = webdriver.EdgeOptions()
    options.add_argument(f"--user-data-dir={config.browser_data_dir.absolute()}")
    if config.browser_headless:
        options.add_argument("--headless")
    for arg in config.browser_args:
        options.add_argument(arg)

    return webdriver.Edge(options=options)


def create_ie_webdriver(config: ClientConfig) -> WebDriver:
    options = webdriver.IeOptions()
    options.add_argument(f"--user-data-dir={config.browser_data_dir.absolute()}")
    if config.browser_headless:
        options.add_argument("--headless")
    for arg in config.browser_args:
        options.add_argument(arg)

    return webdriver.Ie(options=options)


def create_safari_webdriver(config: ClientConfig) -> WebDriver:
    options = webdriver.SafariOptions()
    options.add_argument(f"--user-data-dir={config.browser_data_dir.absolute()}")
    if config.browser_headless:
        options.add_argument("--headless")
    for arg in config.browser_args:
        options.add_argument(arg)

    return webdriver.Safari(options=options)
