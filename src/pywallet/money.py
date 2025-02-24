from __future__ import annotations

import typing as t
from dataclasses import dataclass

if t.TYPE_CHECKING:
    from decimal import Decimal


@dataclass(frozen=True, kw_only=True)
class Money:
    currency: str
    amount: Decimal
