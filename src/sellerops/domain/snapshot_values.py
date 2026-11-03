"""本地商品快照的规格与数值语义；缺失、无效和真实零值分别处理。"""
from math import isfinite
from typing import Any


def positive_id(value: Any) -> int | None:
    if type(value) not in (int, str):
        return None
    text = str(value).strip()
    if not text.isdecimal() or int(text) <= 0:
        return None
    return int(text)


def valid_sizes(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """仅返回标识完整且不重复的全部规格；不丢弃坏规格后继续。"""
    sizes = row.get('sizeList')
    if not isinstance(sizes, list) or not sizes:
        return None
    ids = [positive_id(size.get('chrtId')) if isinstance(size, dict) else None for size in sizes]
    if None in ids or len(set(ids)) != len(ids):
        return None
    return sizes


def quantity(value: Any) -> int | None:
    """库存必须为非负整数；数字文本可规范化，空值和布尔值不是零。"""
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdecimal() else None
    if type(value) is float and isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def stock_values(row: dict[str, Any]) -> list[int] | None:
    """所有规格、库存记录均可确认时才给出数量，部分缺失不能汇总为零。"""
    sizes = valid_sizes(row)
    if sizes is None:
        return None
    values = []
    for size in sizes:
        stocks = size.get('stockList')
        if not isinstance(stocks, list) or not stocks:
            return None
        seen = set()
        for stock in stocks:
            if not isinstance(stock, dict):
                return None
            warehouse = positive_id(stock.get('warehouseId'))
            amount = quantity(stock.get('amount'))
            if warehouse is None or amount is None or warehouse in seen:
                return None
            seen.add(warehouse)
            values.append(amount)
    return values


def stock_at(size: dict[str, Any], warehouse_id: int) -> int | None:
    """指定规格在已确认仓库的唯一库存记录；缺行或重复均为未知。"""
    stocks = size.get('stockList')
    if not isinstance(stocks, list) or any(not isinstance(stock, dict) for stock in stocks):
        return None
    matches = [stock for stock in stocks if positive_id(stock.get('warehouseId')) == warehouse_id]
    return quantity(matches[0].get('amount')) if len(matches) == 1 else None


def first_price(row: dict[str, Any]) -> int | float | None:
    """保留首规格代表价契约；无完整规格时不采用占位价格零。"""
    sizes = valid_sizes(row)
    value = sizes[0].get('price') if sizes else None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number
