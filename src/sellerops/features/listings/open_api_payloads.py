"""官方建卡与跨店更新的纯载荷转换；不复制其他店的标识或条码。"""
import copy
import math
from typing import Any
from urllib.parse import urlsplit

DEFAULT_FIELDS = 'title,description,brand,dimensions,characteristics,photos'
SYNC_FIELDS = set(DEFAULT_FIELDS.split(',')) | {'sizes', 'documents', 'video', 'gtin', 'kizMarked'}
DOCUMENT_KEYS = {'id', 'type', 'number', 'productNumber', 'tradeName', 'applicant',
                 'startDate', 'endDate', 'isEndless'}


def positive_id(value: Any) -> int:
    if type(value) not in (str, int) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('商品、规格、类目或店铺 ID 必须是正整数')
    return int(value)


def card_index(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    ids = set()
    for card in cards:
        nm = positive_id(card.get('nmID'))
        vc = card.get('vendorCode')
        if not isinstance(vc, str) or not vc.strip() or vc != vc.strip() or vc in result or nm in ids:
            raise ValueError('商品列表的完整 VC 或商品码缺失、重复或无效')
        result[vc] = card
        ids.add(nm)
    return result


def dimensions(raw: Any) -> dict[str, int | float]:
    if not isinstance(raw, dict):
        raise ValueError('包装尺寸缺失')
    result = {}
    for key in ('length', 'width', 'height', 'weightBrutto'):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'包装 {key} 必须是正有限数，单位 cm/kg')
        result[key] = float(value) if key == 'weightBrutto' else math.ceil(value)
    return result


def documents(raw: Any, *, keep_ids: bool) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get('items', []), list):
        raise ValueError('证书 documents 数据不完整')
    result: dict[str, Any] = {'items': []}
    if 'excludeDocuments' in raw:
        if type(raw['excludeDocuments']) is not bool:
            raise ValueError('excludeDocuments 必须是布尔值')
        result['excludeDocuments'] = raw['excludeDocuments']
    for row in raw.get('items', []):
        if not isinstance(row, dict):
            raise ValueError('证书项目无效')
        result['items'].append({k: copy.deepcopy(v) for k, v in row.items()
                                if k in DOCUMENT_KEYS and (keep_ids or k != 'id')})
    return result


def characteristics(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError('商品属性 characteristics 缺失')
    result, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('商品属性项目无效')
        cid = positive_id(row.get('id'))
        value = row.get('value')
        if cid in seen or value is None or isinstance(value, bool):
            raise ValueError('商品属性 ID 重复或属性值无效')
        if not isinstance(value, (list, int, float)):
            raise ValueError('商品属性值须为数组或数值')
        if isinstance(value, list) and any(not isinstance(v, str) for v in value):
            raise ValueError('文本商品属性须为字符串数组')
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError('数值商品属性必须有限')
        seen.add(cid)
        result.append({'id': cid, 'value': copy.deepcopy(value)})
    return result


def validate_category(card: dict[str, Any], schema: list[dict[str, Any]]) -> None:
    values = {row['id']: row['value'] for row in characteristics(card.get('characteristics'))}
    schema_by_id = {positive_id(row.get('charcID')): row for row in schema}
    for cid, row in schema_by_id.items():
        if (row.get('required') and not row.get('existNamedField')
                and (cid not in values or values[cid] in (None, '', []))):
            raise ValueError(f'缺少类目必需属性：{row.get("name", cid)}')
    unknown = set(values) - schema_by_id.keys()
    if unknown:
        raise ValueError('商品含当前类目未声明的属性 ID')


def photo_urls(card: dict[str, Any]) -> list[str]:
    photos = card.get('photos')
    if not isinstance(photos, list):
        raise ValueError('商品 photos 缺失，无法确定图片替换范围')
    urls = []
    for photo in photos:
        url = photo.get('big') if isinstance(photo, dict) else None
        if not isinstance(url, str) or not url:
            raise ValueError('缺少高清图片链接，不能用缩略图替代')
        urls.append(url)
    if len(urls) > 30 or len(set(urls)) != len(urls):
        raise ValueError('图片超过 30 张或链接重复')
    return urls


def media_urls(photos: list[str], video: Any = '') -> list[str]:
    urls = list(photos)
    if video:
        if not isinstance(video, str):
            raise ValueError('视频链接无效')
        urls.append(video)
    for i, url in enumerate(urls):
        parts = urlsplit(url)
        allowed = ('.mp4', '.mov') if video and i == len(urls) - 1 else ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp')
        if (parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password
                or not parts.path.lower().endswith(allowed)):
            raise ValueError('媒体必须为可公开访问的图片或 MP4/MOV 文件；HLS/m3u8 不能直接上传')
    if not urls:
        raise ValueError('无法用空媒体清单替换商品图片/视频')
    return urls


def sizes(raw: Any, *, keep_ids: bool) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError('商品缺少完整规格')
    result, ids, barcodes, keys = [], set(), set(), set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('商品规格无效')
        item = {k: copy.deepcopy(row[k]) for k in ('techSize', 'wbSize') if k in row}
        key = (str(item.get('techSize', '')), str(item.get('wbSize', '')))
        if key in keys:
            raise ValueError('商品规格名称重复，不能安全匹配')
        keys.add(key)
        if keep_ids:
            cid = positive_id(row.get('chrtID'))
            skus = row.get('skus')
            if (cid in ids or not isinstance(skus, list) or not skus
                    or any(not isinstance(s, str) or not s for s in skus)
                    or len(set(skus)) != len(skus) or barcodes.intersection(skus)):
                raise ValueError('规格 ID 或条码缺失、重复')
            ids.add(cid)
            barcodes.update(skus)
            item.update(chrtID=cid, skus=list(skus))
        result.append(item)
    return result


def content_fields(card: dict[str, Any], *, keep_document_ids: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ('vendorCode', 'title', 'description', 'brand'):
        if not isinstance(card.get(key), str):
            raise ValueError(f'商品 {key} 缺失或类型无效')
        result[key] = card[key]
    if not result['vendorCode'].strip() or len(result['vendorCode']) > 72:
        raise ValueError('完整 VC 不能为空或超过 72 字符')
    if not result['title'] or len(result['title']) > 60:
        raise ValueError('标题须为 1 到 60 字符，请先调整源标题')
    result['dimensions'] = dimensions(card.get('dimensions'))
    result['characteristics'] = characteristics(card.get('characteristics'))
    if 'documents' in card:
        result['documents'] = documents(card['documents'], keep_ids=keep_document_ids)
    for key in ('gtin', 'kizMarked'):
        if key in card:
            result[key] = copy.deepcopy(card[key])
    return result


def create_payload(card: dict[str, Any], price: float) -> dict[str, Any]:
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0 or int(price) != price:
        raise ValueError('官方建卡 price 必须为正整数（目标店币种），不自动换算或截断')
    variant = content_fields(card, keep_document_ids=False)
    variant['sizes'] = [dict(row, price=int(price)) for row in sizes(card.get('sizes'), keep_ids=False)]
    if 'wholesale' in card:
        variant['wholesale'] = copy.deepcopy(card['wholesale'])
    return {'subjectID': positive_id(card.get('subjectID')), 'variants': [variant]}


def update_payload(source: dict[str, Any], target: dict[str, Any], fields: set[str]
                   ) -> tuple[dict[str, Any] | None, list[str] | None]:
    if not fields or not fields <= SYNC_FIELDS:
        raise ValueError('同步字段为空或不受支持')
    if source.get('vendorCode') != target.get('vendorCode'):
        raise ValueError('跨店匹配必须使用相同的完整 VC')
    if positive_id(source.get('subjectID')) != positive_id(target.get('subjectID')):
        raise ValueError('源店与目标店类目不同，不能直接覆盖')
    merged = copy.deepcopy(target)
    for field in fields - {'photos', 'video', 'sizes'}:
        if field not in source:
            raise ValueError(f'源商品缺少 {field}，不能把缺失当清空')
        merged[field] = (documents(source[field], keep_ids=False) if field == 'documents'
                         else copy.deepcopy(source[field]))
    target_sizes = sizes(target.get('sizes'), keep_ids=True)
    if 'sizes' in fields:
        source_sizes = sizes(source.get('sizes'), keep_ids=False)
        by_key = {(r.get('techSize', ''), r.get('wbSize', '')): r for r in source_sizes}
        if len(source_sizes) != len(target_sizes):
            raise ValueError('规格数量不同，不自动增加或删除规格')
        for row in target_sizes:
            match = source_sizes[0] if len(source_sizes) == 1 else by_key.get((row.get('techSize', ''), row.get('wbSize', '')))
            if match is None:
                raise ValueError('多规格名称无法唯一匹配，不按数组顺序猜测')
            row.update(match)
    before = content_fields(target, keep_document_ids=True)
    before.update(nmID=positive_id(target.get('nmID')), sizes=sizes(target.get('sizes'), keep_ids=True))
    payload = content_fields(merged, keep_document_ids=True)
    payload.update(nmID=positive_id(target.get('nmID')), sizes=target_sizes)
    media = None
    if fields & {'photos', 'video'}:
        old_photos = photo_urls(target)
        new_photos = photo_urls(source) if 'photos' in fields else old_photos
        old_video = target.get('video') or ''
        if 'video' in fields and 'video' not in source:
            raise ValueError('源视频字段缺失，无法确定视频更新范围')
        new_video = (source.get('video') or '') if 'video' in fields else old_video
        if new_photos != old_photos or new_video != old_video:
            media = media_urls(new_photos, new_video)
    return (payload if payload != before else None), media
