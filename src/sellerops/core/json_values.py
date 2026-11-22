"""Represent local result values using portable JSON types."""

import json
from datetime import date, time
from decimal import Decimal
from pathlib import PurePath


def json_value(value):
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: json.dumps(item, default=json_value, sort_keys=True))
    raise TypeError(f'Unsupported JSON type: {type(value).__name__}')
