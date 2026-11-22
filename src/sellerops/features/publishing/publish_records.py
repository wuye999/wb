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


def submit_once(items: list[PublishItem], shops: list[int], send: Callable[[], dict],
                *, actual_vcs: tuple[str, ...] = ()) -> dict:
    """保留旧查重键，发送前锁定本店的提交状态。"""
    keys = set(actual_vcs).union(*(publish_keys(item) for item in items))
    with open_api_publish.operation_lock():
        records = load_records()
        entries = db.vc_entries()
        if any(db.listing_blocked(vc, sid, entries)
               for vc in {item.vendor_code for item in items if item.vendor_code}.union(actual_vcs)
               for sid in shops):
            raise ValueError('本批含禁止上架的 VC，未提交')
        if any(str(sid) in records.get(key, {}) for key in keys for sid in shops):
            raise ValueError('本批已有上架提交记录，未重发')
        stamp = db.now()
        claims = {key: {str(sid): stamp for sid in shops} for key in keys}
        with db.connect(write=True) as connection:
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
        return response


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
