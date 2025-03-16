from __future__ import annotations

import csv
import sys
import typing as t
from contextlib import contextmanager, nullcontext
from pathlib import Path


@contextmanager
def use_csv_writer(
    dest: Path | None,
    fieldnames: t.Sequence[str],
    delimiter: str | None = None,
) -> t.Iterator[csv.DictWriter[str]]:
    with dest.open("w") if dest is not None else nullcontext(sys.stdout) as fd:
        writer = csv.DictWriter(
            fd,
            fieldnames=fieldnames,
            delimiter=delimiter or ",",
        )
        writer.writeheader()

        yield writer
