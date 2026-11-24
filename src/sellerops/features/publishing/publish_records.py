"""BCS 上架查重及提交回执。"""
import json
from collections.abc import Callable, Iterable

from sellerops.domain.models import PublishItem
from sellerops.domain.vendor_codes import extract_vc_source_code
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence import db, open_api_publish


def load_records(*, vc_only: bool = False) -> dict:
    return open_api_publish.load_shared_records(include_open_api=True, vc_only=vc_only)


def publish_vc_key(vc: str | None) -> str:
    return extract_vc_source_code(vc or '', min_digits=6) or vc or ''


def vendor_code_index(vcs: Iterable[str | None]) -> dict[str, str]:
    return {publish_vc_key(vc): vc for vc in vcs if vc}


def records_by_shop(records: dict) -> dict[str, dict[str, str]]:
    indexed = {}
    for vc, shops in records.items():
        key = publish_vc_key(vc)
        for sid in shops:
            indexed.setdefault(str(sid), {})[key] = vc
    return indexed


def today_publish_counts(day: str | None = None) -> dict[int, int]:
    """按店统计当天已发起提交的不同商品卡。"""
    day = day or db.now()[:10]
    vcs_by_shop: dict[int, set[str]] = {}
    with db.connect() as connection:
        # P 单号表示整批计划，逐卡提交状态保存在下列两类回执中。
        rows = connection.execute("""
            SELECT dedup_key, payload_json FROM plans
            WHERE action_type = 'publish' AND status IN ('submitting', 'unknown', 'done', 'partial')
              AND substr(updated_at, 1, 10) = ? AND (plan_id GLOB 'legacy-publish:*' OR plan_id GLOB 'openapi:*')
        """, (day,))
        for row in rows:
            sid, key = json.loads(row['dedup_key'])
            vc = open_api_publish.record_vc(key, row['payload_json'])
            if vc:
                vcs_by_shop.setdefault(int(sid), set()).add(vc)
    return {sid: len(vcs) for sid, vcs in vcs_by_shop.items()}


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
    vc_keys = set(actual_vcs) if actual_vcs else {item.vendor_code for item in items if item.vendor_code}
    keys = vc_keys.union(*(publish_keys(item) for item in items))
    candidates = vc_keys | {item.vendor_code for item in items if item.vendor_code}
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
        submitted = records_by_shop(load_records(vc_only=True))
        for sid in shops:
            for vc in sorted(candidates):
                matched = submitted.get(str(sid), {}).get(publish_vc_key(vc))
                if matched:
                    raise ValueError(f'店铺 {sid} 命中目标店已有同源商品 VC：{matched}')
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
            open_api_publish.write_shared(connection, claims, status='submitting', vc_keys=vc_keys)
        try:
            response = send()
        except Exception:
            with db.connect(write=True) as connection:
                open_api_publish.write_shared(connection, claims, status='unknown', vc_keys=vc_keys)
            raise
        code = response.get('code') if isinstance(response, dict) else None
        state = 'done' if code == 200 else 'draft' if type(code) is int else 'unknown'
        with db.connect(write=True) as connection:
            open_api_publish.write_shared(connection, claims, status=state, vc_keys=vc_keys)
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
