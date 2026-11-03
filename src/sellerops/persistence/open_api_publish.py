"""官方建卡阶段记录，按数据根目录隔离；调用者持有整个操作的 FileLock。"""
from pathlib import Path
from typing import Any

from sellerops.core.files import atomic_dump_json, safe_load_json, FileLock
from sellerops.settings import paths


def state_path() -> Path:
    return Path(paths.STATE_DIR) / 'WB_OpenAPI上架记录.json'


def operation_lock() -> FileLock:
    return FileLock(state_path())


def load_state() -> dict[str, Any]:
    state = safe_load_json(state_path(), default={'version': 1, 'items': {}})
    if (not isinstance(state, dict) or state.get('version') != 1
            or not isinstance(state.get('items'), dict)
            or any(not isinstance(v, dict) for v in state['items'].values())):
        raise ValueError('WB Open API 上架记录损坏，不能重置后重跑')
    statuses = {'planned', 'preparing', 'prepare_failed', 'submitting', 'unknown', 'accepted', 'finished'}
    for key, entry in state['items'].items():
        sid, vc = entry.get('shopId'), entry.get('vc')
        if (type(sid) is not int or sid <= 0 or not isinstance(vc, str) or not vc.strip()
                or key != item_key(sid, vc) or entry.get('status') not in statuses
                or not isinstance(entry.get('stages'), dict)
                or any(not isinstance(stage, dict) or stage.get('status') not in {'submitting', 'unknown', 'accepted'}
                       for stage in entry['stages'].values())):
            raise ValueError('WB Open API 上架记录的店铺/VC/阶段不一致，已阻止操作')
    return state


def save_state(state: dict[str, Any]) -> None:
    atomic_dump_json(state_path(), state)


def item_key(shop_id: int, vc: str) -> str:
    # JSON 键编码，不以任意 VC 中的分隔符作为解析契约。
    import json
    return json.dumps([shop_id, vc], ensure_ascii=False, separators=(',', ':'))


def load_shared_records() -> dict[str, Any]:
    records = safe_load_json(Path(paths.STATE_DIR) / '复制上架记录.json', default={})
    if not isinstance(records, dict) or any(not isinstance(v, dict) for v in records.values()):
        raise ValueError('复制上架记录损坏')
    return records


def legacy_submitted(shop_id: int, vc: str, records: dict[str, Any] | None = None) -> bool:
    if records is None:
        records = load_shared_records()
    return str(shop_id) in records.get(vc, {})


def record_shared(shop_id: int, vcs: list[str]) -> None:
    path = Path(paths.STATE_DIR) / '复制上架记录.json'
    with FileLock(path):
        records = load_shared_records()
        from datetime import datetime
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for vc in vcs:
            records.setdefault(vc, {})[str(shop_id)] = stamp
        atomic_dump_json(path, records)
