"""JSON-safe value normalization for API responses consumed by the browser."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def json_safe_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, Decimal):
        as_float = float(value)
        if math.isnan(as_float) or math.isinf(as_float):
            return None
        return as_float
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def json_safe_rows(rows: list[list[Any]]) -> list[list[Any]]:
    return [[json_safe_value(cell) for cell in row] for row in rows]
