"""官方建卡阶段与同店上架查重记录。"""
import json
import sqlite3
from pathlib import Path
from typing import Any

from sellerops.core.files import FileLock
from sellerops.persistence import db
from sellerops.settings import paths


def operation_lock() -> FileLock:
    return FileLock(Path(paths.current.state_dir) / 'wb_openapi上架记录.json')


def item_key(shop_id: int, vc: str) -> str:
    return db.dumps([shop_id, vc])


def validate_state(state: Any) -> None:
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


def load_state() -> dict[str, Any]:
    state = {'version': 1, 'items': {row['dedup_key']: json.loads(row['payload_json'])
                                    for row in db.plans('publish', 'openapi:')}}
    validate_state(state)
    return state


def write_state(connection: sqlite3.Connection, state: dict) -> None:
    validate_state(state)
    for key, entry in state['items'].items():
        stages = {stage['status'] for stage in entry['stages'].values()}
        status = entry['status']
        if 'submitting' in stages or status == 'submitting':
            status = 'submitting'
        elif 'unknown' in stages or status == 'unknown':
            status = 'unknown'
        else:
            status = {'finished': 'done', 'accepted': 'partial'}.get(status, 'draft')
        db.save_plan(connection, 'openapi:' + key, 'publish', status, key, entry)


def save_state(state: dict[str, Any]) -> None:
    with db.connect(write=True) as connection:
        write_state(connection, state)


def write_shared(connection: sqlite3.Connection, records: dict, *, status: str = 'done') -> int:
    if not isinstance(records, dict):
        raise ValueError('复制上架记录损坏')
    count = 0
    for key, shops in records.items():
        if not isinstance(key, str) or not key or not isinstance(shops, dict):
            raise ValueError('复制上架记录损坏')
        for sid, stamp in shops.items():
            if not str(sid).isdecimal() or not isinstance(stamp, str):
                raise ValueError('复制上架记录的店铺或时间无效')
            identity = item_key(int(sid), key)
            db.save_plan(connection, 'legacy-publish:' + identity, 'publish', status, identity,
                         {'key': key, 'shopId': sid, 'timestamp': stamp}, stamp)
            count += 1
    return count


def load_shared_records(*, include_open_api: bool = False) -> dict[str, Any]:
    with db.connect() as connection:
        rows = db.plans('publish', 'legacy-publish:', connection)
        if include_open_api:
            rows += db.plans('publish', 'openapi:', connection)
    records: dict[str, dict] = {}
    for row in rows:
        if row['status'] not in {'submitting', 'unknown', 'done', 'partial'}:
            continue
        sid, key = json.loads(row['dedup_key'])
        records.setdefault(key, {})[str(sid)] = row['updated_at']
    return records


def legacy_submitted(shop_id: int, vc: str, records: dict[str, Any] | None = None) -> bool:
    if records is None:
        records = load_shared_records()
    return str(shop_id) in records.get(vc, {})


def record_shared(shop_id: int, vcs: list[str]) -> None:
    stamp = db.now()
    with db.connect(write=True) as connection:
        write_shared(connection, {vc: {str(shop_id): stamp} for vc in vcs})
