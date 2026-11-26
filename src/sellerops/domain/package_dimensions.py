"""Translate the packaging format shared by publishing and price-book diagnostics."""
import re


_DIMENSIONS = re.compile(r"([\d.]+)\s*[*xX]\s*([\d.]+)\s*[*xX]\s*([\d.]+)(?:\s*/\s*([\d.]+))?")


def parse_package_dimensions(value: object) -> tuple[float, float, float, float | None] | None:
    match = _DIMENSIONS.fullmatch(str(value).strip())
    if match is None:
        return None
    length, width, height, weight = match.groups()
    try:
        return float(length), float(width), float(height), float(weight) if weight is not None else None
    except ValueError:
        return None
