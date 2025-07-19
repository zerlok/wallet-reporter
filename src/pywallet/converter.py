import typing as t
from argparse import ArgumentParser, BooleanOptionalAction
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pdfplumber
import tabula
import tqdm
import yaml
from pdfplumber import PDF
from pydantic import BaseModel


@dataclass(frozen=True, kw_only=True)
class Area:
    left: float = 0
    top: float = 0
    right: float = 0
    bottom: float = 0


class PDFOptions(BaseModel):
    class TableOptions(BaseModel):
        class ColumnOptions(BaseModel):
            class MergeOptions(BaseModel):
                class JoinOptions(BaseModel):
                    separator: str

                join: JoinOptions | None = None

            name: str
            type: t.Literal["bool", "int", "float", "str", "date", "money"] = "str"
            required: bool = True
            rename: str | None
            ignore_values: t.Sequence[object] | None = None
            merge: MergeOptions | None = None

        columns: t.Sequence[ColumnOptions]
        footers: t.Sequence[str] | None = None
        include_footer: bool = False
        offset: Area | None = None
        word_join_tolerance: int | None = None

    table: TableOptions | None = None


class ConvertOptions(BaseModel):
    class ReadOptions(BaseModel):
        pdf: PDFOptions | None = None

    class WriteOptions(BaseModel):
        class CSVOptions(BaseModel):
            columns: t.Sequence[str] | None = None
            sort_by: t.Sequence[str] | None = None
            index: bool = False

        csv: CSVOptions | None = None

    read: ReadOptions
    write: WriteOptions


@dataclass(frozen=True, kw_only=True)
class PDFWordSearchOptions:
    exact_match: str | None = None
    substr: str | None = None


@dataclass(frozen=True, kw_only=True)
class PDFWordSearchResult(Area):
    word: str
    page: int


def search_words_in_pdf(
    doc: PDF,
    searches: t.Sequence[PDFWordSearchOptions],
    word_join_tolerance: int,
) -> t.Iterable[PDFWordSearchResult]:
    for page in tqdm.tqdm(doc.pages, desc=f"Searching words in PDF {doc.path}"):
        words = page.extract_words(x_tolerance=word_join_tolerance, keep_blank_chars=True)

        for search in searches:
            for word in words:
                if (search.exact_match and word["text"] == search.exact_match) or (
                    search.substr and search.substr in word["text"]
                ):
                    yield PDFWordSearchResult(
                        word=word["text"],
                        page=page.page_number,
                        left=word["x0"],
                        top=word["top"],
                        right=word["x1"],
                        bottom=word["bottom"],
                    )


def get_table_areas_by_pages(path: Path, options: PDFOptions.TableOptions) -> t.Mapping[int, Area]:
    areas = dict[int, Area]()

    with pdfplumber.open(path) as doc:
        for search in search_words_in_pdf(
            doc,
            [PDFWordSearchOptions(exact_match=col.name) for col in options.columns if col.required],
            word_join_tolerance=options.word_join_tolerance,
        ):
            page = doc.pages[search.page - 1]
            area = areas.get(search.page)

            if area is None:
                area = areas[search.page] = Area(right=page.width, bottom=page.height)

            areas[search.page] = replace(area, top=max(area.top, search.top))

        for search in search_words_in_pdf(
            doc,
            [PDFWordSearchOptions(substr=footer) for footer in options.footers],
            word_join_tolerance=options.word_join_tolerance,
        ):
            area = areas.get(search.page)
            if area is None:
                continue

            areas[search.page] = replace(
                area,
                bottom=min(area.bottom, search.bottom if options.include_footer else search.top),
            )

    return areas


def read_pdf_table(path: Path, options: PDFOptions.TableOptions) -> pd.DataFrame:
    areas = get_table_areas_by_pages(path, options)

    return pd.concat(
        objs=(
            merge_multiline_rows(table, options)
            for page, area in tqdm.tqdm(
                areas.items(),
                desc=f"reading tables on each page of PDF {path}",
            )
            for table in tabula.read_pdf(
                input_path=path,
                pages=page,
                area=[
                    area.top + options.offset.top,
                    area.left + options.offset.left,
                    area.bottom + options.offset.bottom,
                    area.right + options.offset.right,
                ],
                multiple_tables=False,
                silent=True,
                force_subprocess=True,
                pandas_options={"header": None},
            )
        ),
        ignore_index=True,
    )


def merge_row(row: t.Sequence[t.Sequence[object]], options: PDFOptions.TableOptions) -> t.Sequence[t.Sequence[object]]:
    if not row or any(not row[i] for i, col in enumerate(options.columns) if col.required):
        print("empty row", row)
        return []

    return [
        [merge_row_values(vals, col) for vals, col in zip(row, options.columns, strict=True)],
    ]


def merge_row_values[T](values: t.Sequence[T], column: PDFOptions.TableOptions.ColumnOptions) -> T:
    if column.merge is None:
        return flatten(values)

    elif column.merge.join is not None:
        return column.merge.join.separator.join(values)

    else:
        msg = "unknown column merge options"
        raise ValueError(msg, column)


def flatten[T](vals: t.Sequence[T]) -> T | None:
    return vals[0] if vals else None


def parse_value(value: str, column: PDFOptions.TableOptions.ColumnOptions) -> object:
    match column.type:
        case "bool":
            match value.lower().strip():
                case "true":
                    return True
                case "false":
                    return False
                case _:
                    msg = "invalid bool value"
                    raise ValueError(msg, value)

        case "int":
            return int(value)

        case "float":
            return float(value)

        case "str":
            return value

        case "date":
            return datetime.strptime(value, "%d.%m.%Y")

        case "money":
            amount, _, currency = value.partition(" ")
            return Decimal(amount.replace(",", ""))

        case _:
            msg = "unknown column type"
            raise ValueError(msg, column)


def merge_multiline_rows(table: pd.DataFrame, options: PDFOptions.TableOptions) -> pd.DataFrame:
    rows = list[t.Sequence[object]]()
    current_row: list[list[object]] | None = None

    for _, row in table.iterrows():
        if any(row[i] in col.ignore_values for i, col in enumerate(options.columns) if col.ignore_values):
            continue

        if not current_row or all(pd.notna(row[i]) for i, col in enumerate(options.columns) if col.required):
            rows.extend(merge_row(current_row, options))

            try:
                current_row = [
                    [parse_value(row[i], col)] if pd.notna(row[i]) else [] for i, col in enumerate(options.columns)
                ]

            except ValueError as err:
                print(row.values, err)
                continue

        elif current_row:
            for i, col in enumerate(options.columns):
                if pd.notna(row[i]) and col.merge is not None:
                    current_row[i].append(parse_value(row[i], col))

    rows.extend(merge_row(current_row, options))

    return pd.DataFrame(
        data=rows,
        columns=[h.rename or h.name for h in options.columns],
    )


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Converts reports between different formats.")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=False,
        help="Disable all outputs. Default: %(default)s",
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

    parser.add_argument(
        "options",
        type=Path,
        help="Path to yaml specification of converting options.",
    )

    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="Paths to reports to convert.",
    )

    return parser


def read_report(path: Path, options: ConvertOptions.ReadOptions) -> pd.DataFrame:
    if options.pdf is not None and options.pdf.table is not None:
        return read_pdf_table(path, options.pdf.table)

    else:
        msg = "read options were not set"
        raise ValueError(msg, options)


def write_report(path: Path, df: pd.DataFrame, options: ConvertOptions.WriteOptions) -> None:
    if options.csv is not None:
        if options.csv.sort_by is not None:
            df = df.sort_values(by=options.csv.sort_by)

        df.to_csv(
            path_or_buf=path.with_suffix(".csv"),
            columns=options.csv.columns,
            index=options.csv.index,
        )

    else:
        msg = "write options were not set"
        raise ValueError(msg, options)


def load_options(path: Path) -> ConvertOptions:
    return ConvertOptions.model_validate(yaml.safe_load(path.read_bytes()))


def main() -> None:
    parser = build_parser()
    ns = parser.parse_args()

    options = load_options(ns.options)
    paths: t.Sequence[Path] = ns.inputs

    for path in tqdm.tqdm(paths, desc="reading each PDF file"):
        df = read_report(path, options.read)
        write_report(path, df, options.write)


if __name__ == "__main__":
    main()
