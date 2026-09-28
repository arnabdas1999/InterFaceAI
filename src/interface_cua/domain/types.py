"""Closed set of output types, their JSON Schemas, and deterministic normalizers.

An extracted value that fails normalization is a hard failure (``output_invalid``), never a
silent pass-through.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class OutputType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"
    DATE = "date"
    BOOLEAN = "boolean"
    ENUM = "enum"


class NormalizationError(ValueError):
    pass


_AMOUNT_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$")


def normalize_money(raw: str, currency: str = "USD") -> dict[str, str]:
    """``$1,234.56`` -> 1234.56; ``($12.00)``, ``$(12.00)``, ``-$12.00`` -> -12.00; ``USD 5`` -> 5.00."""
    text = "".join(raw.split())
    body = text
    if body.upper().startswith(currency.upper()):
        body = body[len(currency) :]
    negative = False
    for _ in range(3):  # peel sign, parentheses, and currency symbol in any order
        if body.startswith("-"):
            negative, body = True, body[1:]
        if body.startswith("(") and body.endswith(")"):
            negative, body = True, body[1:-1]
        if body.startswith("$"):
            body = body[1:]
    if not _AMOUNT_RE.match(body):
        raise NormalizationError(f"not a money value: {raw!r}")
    try:
        amount = Decimal(body.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise NormalizationError(f"not a money value: {raw!r}") from exc
    if negative:
        amount = -amount
    return {"amount": f"{amount:.2f}", "currency": currency}


def normalize_date(raw: str) -> str:
    text = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise NormalizationError(f"not a date: {text!r}")


def normalize(
    output_type: OutputType,
    raw: str,
    *,
    enum_values: list[str] | None = None,
    enum_labels: dict[str, str] | None = None,
    pattern: str | None = None,
    currency: str = "USD",
) -> Any:
    """Normalize a raw UI string into the typed value declared by the artifact."""
    text = " ".join(raw.split())
    match output_type:
        case OutputType.STRING:
            value: Any = text
        case OutputType.INTEGER:
            cleaned = text.replace(",", "")
            if not re.fullmatch(r"-?\d+", cleaned):
                raise NormalizationError(f"not an integer: {text!r}")
            value = int(cleaned)
        case OutputType.DECIMAL:
            cleaned = text.replace(",", "")
            try:
                value = str(Decimal(cleaned))
            except InvalidOperation as exc:
                raise NormalizationError(f"not a decimal: {text!r}") from exc
        case OutputType.MONEY:
            value = normalize_money(text, currency)
        case OutputType.DATE:
            value = normalize_date(text)
        case OutputType.BOOLEAN:
            lowered = text.lower()
            if lowered in {"yes", "y", "true", "active", "on"}:
                value = True
            elif lowered in {"no", "n", "false", "inactive", "off"}:
                value = False
            else:
                raise NormalizationError(f"not a boolean: {text!r}")
        case OutputType.ENUM:
            allowed = enum_values or []
            labels = {v.lower(): k for k, v in (enum_labels or {}).items()}
            if text in allowed:
                value = text
            elif text.lower() in labels and labels[text.lower()] in allowed:
                value = labels[text.lower()]
            else:
                raise NormalizationError(f"{text!r} is not one of {allowed}")
    if pattern is not None and output_type == OutputType.STRING and not re.fullmatch(pattern, str(value)):
        raise NormalizationError(f"{value!r} does not match {pattern}")
    return value


def json_schema_for(output_type: OutputType, *, enum_values: list[str] | None = None) -> dict[str, Any]:
    match output_type:
        case OutputType.STRING:
            return {"type": "string"}
        case OutputType.INTEGER:
            return {"type": "integer"}
        case OutputType.DECIMAL:
            return {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"}
        case OutputType.MONEY:
            return {
                "type": "object",
                "x-cua-type": "money",
                "properties": {
                    "amount": {"type": "string", "pattern": r"^-?\d+\.\d{2}$"},
                    "currency": {"type": "string", "pattern": r"^[A-Z]{3}$"},
                },
                "required": ["amount", "currency"],
                "additionalProperties": False,
            }
        case OutputType.DATE:
            return {"type": "string", "format": "date"}
        case OutputType.BOOLEAN:
            return {"type": "boolean"}
        case OutputType.ENUM:
            return {"type": "string", "enum": list(enum_values or [])}
    raise AssertionError(output_type)


def today_iso() -> str:
    return date.today().isoformat()
