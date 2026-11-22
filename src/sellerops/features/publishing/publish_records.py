"""BCS 上架查重及提交回执。"""
from collections.abc import Callable

from sellerops.domain.models import PublishItem
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence import db, open_api_publish


def load_records() -> dict:
    return open_api_publish.load_shared_records(include_open_api=True)


def publish_keys(item: PublishItem) -> set[str]:
    keys = {str(item.nm_id)}
    if item.vendor_code:
        keys.add(item.vendor_code)
    if item.extra and item.extra.get('k'):
        keys.add(str(item.extra['k']))
    return keys


def publish_task_id(response: dict) -> str | int | None:
    data = response.get('data')
    task_id = response.get('taskId') or (data.get('taskId') if isinstance(data, dict) else None)
    return task_id if type(task_id) in (str, int) else None


def submit_once(items: list[PublishItem], shops: list[int], send: Callable[[], dict],
                *, actual_vcs: tuple[str, ...] = ()) -> dict:
    """保留旧查重键，发送前锁定本店的提交状态。"""
    keys = set(actual_vcs).union(*(publish_keys(item) for item in items))
    affiliations = {}
    for item, vc in zip(items, actual_vcs or tuple(it.vendor_code for it in items), strict=True):
        sku = item.extra.get('seller_sku') if item.extra.get('vc_source') else None
        if sku:
            if vc in affiliations and affiliations[vc] != sku:
                raise ValueError(f'完整 VC {vc} 对应多个卖家 SKU，未提交')
            affiliations[vc] = sku
    with open_api_publish.operation_lock():
        records = load_records()
        if any(str(sid) in records.get(key, {}) for key in keys for sid in shops):
            raise ValueError('本批已有上架提交记录，未重发')
        stamp = db.now()
        claims = {key: {str(sid): stamp for sid in shops} for key in keys}
        with db.connect(write=True) as connection:
            entries = db.vc_entries(connection)
            if any(db.listing_blocked(vc, sid, entries)
                   for vc in {item.vendor_code for item in items if item.vendor_code}.union(actual_vcs)
                   for sid in shops):
                raise ValueError('本批含禁止上架的 VC，未提交')
            conflicts = _affiliation_conflicts(connection, affiliations)
            if conflicts:
                raise ValueError(f'上架归属已变化，请核对后重新预览：{"、".join(conflicts)}')
            open_api_publish.write_shared(connection, claims, status='submitting')
        try:
            response = send()
        except Exception:
            with db.connect(write=True) as connection:
                open_api_publish.write_shared(connection, claims, status='unknown')
            raise
        code = response.get('code') if isinstance(response, dict) else None
        state = 'done' if code == 200 else 'draft' if type(code) is int else 'unknown'
        with db.connect(write=True) as connection:
            open_api_publish.write_shared(connection, claims, status=state)
            if state == 'done' and affiliations:
                conflicts = _affiliation_conflicts(connection, affiliations)
                for vc, sku in affiliations.items():
                    if vc in conflicts:
                        continue
                    existing = connection.execute('SELECT status, note FROM vc_map WHERE vc=?', (vc,)).fetchone()
                    if existing is None or existing['status'] == 'pending':
                        db.set_vc(connection, vc, 'mapped', sku=sku, source='publish',
                                  note=existing['note'] if existing else '')
                if conflicts:
                    response = {**response, 'mapping_conflicts': conflicts}
        return response


def _affiliation_conflicts(connection, affiliations: dict[str, str]) -> list[str]:
    conflicts = []
    for vc, sku in affiliations.items():
        current = connection.execute('SELECT sku, status FROM vc_map WHERE vc=?', (vc,)).fetchone()
        if (current and (current['status'] == 'excluded' or current['sku'] not in (None, sku))
                or not connection.execute('SELECT 1 FROM products WHERE sku=?', (sku,)).fetchone()):
            conflicts.append(vc)
    return conflicts


def vc_exists_in_shop(vc: str, sid: int, records: dict | None = None) -> bool:
    """先查本地提交记录，再以 BCS 全状态查询查重。"""
    if records and str(sid) in (records.get(vc) or {}):
        return True
    url = (f'{bcs.base_url}/shopKeeper/productList/list?filter=ALL&pageNum=1&pageSize=10'
           f'&shopId={sid}&vendorCodeMulti={vc}')
    try:
        result = bcs.get(url)
        return bool(result.get('code') == 200 and result.get('rows'))
    except Exception:
        return False
