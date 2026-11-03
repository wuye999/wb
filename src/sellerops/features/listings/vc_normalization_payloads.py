"""只修改 VC 的完整覆盖载荷；保留现值，不套用建卡的标题/包装规范化。"""
import copy
import json
import re
from typing import Any

from sellerops.domain.vc_normalization import MAX_VC_LENGTH, replacement_base
from sellerops.features.listings.open_api_payloads import characteristics, documents, positive_id


MAX_REQUEST_BYTES = 10_000_000


def encoded_size(value: Any) -> int:
    # requests 的 json= 使用默认 ASCII 转义，按实际编码计算而非 UTF-8 原文大小。
    return len(json.dumps(value, allow_nan=False).encode('utf8'))


def _retained_sizes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError('商品缺少完整规格')
    result, ids, barcodes = [], set(), set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('商品规格无效')
        cid, skus = positive_id(row.get('chrtID')), row.get('skus')
        if (cid in ids or not isinstance(skus, list) or not skus
                or any(not isinstance(code, str) or not code for code in skus)
                or len(skus) != len(set(skus)) or barcodes.intersection(skus)):
            raise ValueError('规格 ID 或条码缺失、重复')
        ids.add(cid)
        barcodes.update(skus)
        item = {key: copy.deepcopy(row[key]) for key in ('techSize', 'wbSize') if key in row}
        item.update(chrtID=cid, skus=list(skus))
        result.append(item)
    return result


def rename_payload(card: dict[str, Any], new_vc: str) -> dict[str, Any]:
    if (not isinstance(new_vc, str) or not new_vc.strip() or new_vc != new_vc.strip()
            or len(new_vc) > MAX_VC_LENGTH):
        raise ValueError('新 VC 不能为空或超过 72 字符')
    old_vc = card.get('vendorCode')
    if not isinstance(old_vc, str):
        raise ValueError('官方商品卡缺少完整旧 VC')
    base = replacement_base(old_vc, new_vc[4:8])
    if new_vc != base and not re.fullmatch(re.escape(base) + r'-[1-9][0-9]*', new_vc):
        raise ValueError('原编码尾部必须原样保留，只能在末尾追加正整数避重序号')
    result = {'nmID': positive_id(card.get('nmID')), 'vendorCode': new_vc,
              'sizes': _retained_sizes(card.get('sizes'))}
    for key in ('title', 'description', 'brand', 'gtin', 'kizMarked'):
        if key in card:
            result[key] = copy.deepcopy(card[key])
    if 'dimensions' in card:
        raw = card['dimensions']
        if not isinstance(raw, dict):
            raise ValueError('官方卡片 dimensions 结构无效，无法安全保留')
        result['dimensions'] = {key: copy.deepcopy(raw[key])
                                for key in ('length', 'width', 'height', 'weightBrutto') if key in raw}
    if 'characteristics' in card:
        result['characteristics'] = characteristics(card['characteristics'])
    if 'documents' in card:
        raw = card['documents']
        if not isinstance(raw, dict):
            raise ValueError('官方卡片 documents 不完整，无法安全保留')
        if 'items' not in raw and (type(raw.get('excludeDocuments')) is not bool
                                  or set(raw) - {'excludeDocuments', 'overallVerdict'}):
            # 官方无证书时可省略可选 items；必须有明确的布尔设置，不能把未知数据当空。
            raise ValueError('官方卡片 documents 不完整，无法安全保留')
        if 'items' in raw and not isinstance(raw['items'], list):
            raise ValueError('官方卡片 documents 不完整，无法安全保留')
        result['documents'] = documents(raw, keep_ids=True)
    if encoded_size([result]) > MAX_REQUEST_BYTES:
        raise ValueError('单张商品卡的更新请求超过 10 MB')
    return result


def batch_lengths(cards: list[dict[str, Any]]) -> list[int]:
    """预先分批；每批最多100张卡，且默认 JSON 序列化不超过10MB。"""
    lengths, count, total = [], 0, 2
    for card in cards:
        size = encoded_size(card)
        if size + 2 > MAX_REQUEST_BYTES:
            raise ValueError('单张商品卡的更新请求超过 10 MB')
        extra = size + (2 if count else 0)
        if count and (count == 100 or total + extra > MAX_REQUEST_BYTES):
            lengths.append(count)
            count, total, extra = 0, 2, size
        count += 1
        total += extra
    if count:
        lengths.append(count)
    return lengths
