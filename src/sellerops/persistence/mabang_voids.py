"""Shared Mabang fulfillment lock and durable cancellation receipts."""
from pathlib import Path

from sellerops.core.errors import DataConsistencyError
from sellerops.core.files import FileLock, atomic_dump_json, safe_load_json
from sellerops.settings import paths


ATTEMPT_STATES = frozenset({'SUBMITTING', 'ACCEPTED', 'FAILED', 'UNKNOWN'})


def ledger_path() -> Path:
    return Path(paths.current.state_dir) / 'mabang-void-wb.json'


def fulfillment_lock() -> FileLock:
    return FileLock(ledger_path())


def load_attempts(path: Path) -> dict[tuple[int, int], dict]:
    data = safe_load_json(path, default={'version': 1, 'attempts': []})
    if (not isinstance(data, dict) or type(data.get('version')) is not int
            or data['version'] != 1 or not isinstance(data.get('attempts'), list)):
        raise DataConsistencyError('马帮作废账本格式无效')
    attempts = {}
    for row in data['attempts']:
        if (not isinstance(row, dict)
                or any(type(row.get(k)) is not int or row[k] <= 0
                       for k in ('shop_id', 'platform_order_id', 'mabang_order_id'))
                or any(not isinstance(row.get(k), str) or not row[k]
                       for k in ('status', 'supply_id', 'run_id', 'updated_at', 'reason'))
                or row['status'] not in ATTEMPT_STATES):
            raise DataConsistencyError('马帮作废账本含无效记录')
        key = (row['shop_id'], row['platform_order_id'])
        if key in attempts:
            raise DataConsistencyError('马帮作废账本含重复订单')
        attempts[key] = row
    return attempts


def save_attempts(path: Path, attempts: dict[tuple[int, int], dict]) -> None:
    atomic_dump_json(path, {'version': 1, 'attempts': list(attempts.values())}, use_lock=False)
