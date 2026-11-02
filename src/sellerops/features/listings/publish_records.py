"""publish records。"""


import os


from datetime import datetime
from typing import Dict, Any, List, Optional

from sellerops.domain.models import PublishItem
from sellerops.integrations.bcs import client as bcs
from sellerops.core.files import safe_load_json
from sellerops.core.files import atomic_dump_json


from sellerops.settings import paths as config


RECORDS_JSON = os.path.join(config.STATE_DIR, "复制上架记录.json")

def load_records() -> Dict[str, Any]:
    """读取上架记录（键包含 vc 与原始 nmId）"""
    return safe_load_json(RECORDS_JSON, default={})

def save_records(records: Dict[str, Any]):
    """原子写入上架记录"""
    atomic_dump_json(RECORDS_JSON, records, indent=2, use_lock=True)

def record_publish_success(records: Dict[str, Any], item: PublishItem, target_sids: List[int]):
    """记录上架成功条目（同时记录 vc、nm_id 与原始 k 三条防线，确保跨账号/跨接口查重一致性）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nm_key = str(item.nm_id)
    k_key = str(item.extra.get("k")) if item.extra and item.extra.get("k") else None
    for sid in target_sids:
        s_sid = str(sid)
        records.setdefault(nm_key, {})[s_sid] = now_str
        if k_key:
            records.setdefault(k_key, {})[s_sid] = now_str
        if item.vendor_code:
            records.setdefault(item.vendor_code, {})[s_sid] = now_str

def vc_exists_in_shop(vc: str, sid: int, records: Optional[Dict[str, Any]] = None) -> bool:
    """查重：先查本地提交记录，再调 BCS 接口全状态查重 (filter=ALL)"""
    if records and str(sid) in (records.get(vc) or {}):
        return True
    url = (
        f"{bcs.client.base_url}/shopKeeper/productList/list?filter=ALL&pageNum=1&pageSize=10"
        f"&shopId={sid}&vendorCodeMulti={vc}"
    )
    try:
        d = bcs.client.get(url)
        return bool(d.get("code") == 200 and d.get("rows"))
    except Exception:
        return False
