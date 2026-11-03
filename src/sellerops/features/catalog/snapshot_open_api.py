"""官方商品、价格和单一莫斯科仓库存 → 兼容本地商品快照。"""
from math import isfinite
from typing import Any

from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.wildberries.open_api import WBOpenAPI, OpenAPIResponseError
from sellerops.settings import paths as config


def _id(value: Any) -> int:
    if type(value) not in (int, str) or not str(value).isdigit() or int(value) <= 0:
        raise OpenAPIResponseError('商品快照包含无效商品、规格或仓库 ID')
    return int(value)


def _number(value: Any, field: str, maximum: int | None = None) -> int | float | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise OpenAPIResponseError(f'商品快照 {field} 不是有效数值')
    try:
        number = float(value)
    except ValueError:
        raise OpenAPIResponseError(f'商品快照 {field} 不是有效数值') from None
    if not isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        raise OpenAPIResponseError(f'商品快照 {field} 超出有效范围')
    return int(number) if number.is_integer() else number


def resolve_moscow(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """只接受本店唯一可用的莫斯科仓库 id，不使用 officeId。"""
    matches = [row for row in rows if config.DEFAULT_WAREHOUSE_NAME in str(row.get('name') or '')
               and '成都' not in str(row.get('name') or '') and not row.get('isDeleting')]
    if len(matches) != 1 or matches[0].get('isProcessing'):
        raise OpenAPIResponseError('无法确认唯一可用的莫斯科仓库，已中止该店快照采集')
    return {'id': _id(matches[0].get('id')), 'name': str(matches[0]['name'])}


def normalize_rows(shop_id: int, cards: list[dict[str, Any]], prices: list[dict[str, Any]],
                   stocks: list[dict[str, Any]], warehouse: dict[str, Any]
                   ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """关联本店商品和完整规格；合法缺失保留为空，不从库存缺行推断零库存。"""
    nm_ids, chrt_ids, vcs = set(), set(), set()
    for card in cards:
        nm = _id(card.get('nmID'))
        vc = card.get('vendorCode')
        sizes = card.get('sizes')
        if (nm in nm_ids or not isinstance(vc, str) or not vc.strip() or vc.strip() in vcs
                or not isinstance(sizes, list) or not sizes):
            raise OpenAPIResponseError('商品卡缺少完整标识/规格，或包含重复商品')
        if card.get('trashedAt'):
            raise OpenAPIResponseError('非回收站商品列表意外返回回收站商品')
        nm_ids.add(nm)
        vcs.add(vc.strip())
        for size in sizes:
            chrt = _id(size.get('chrtID')) if isinstance(size, dict) else _id(None)
            if chrt in chrt_ids:
                raise OpenAPIResponseError('商品卡包含重复规格 ID')
            chrt_ids.add(chrt)

    prices_by_nm = {}
    for price in prices:
        nm = _id(price.get('nmID'))
        if nm not in nm_ids or nm in prices_by_nm or not isinstance(price.get('sizes'), list):
            raise OpenAPIResponseError('价格响应含未知/重复商品或缺少规格数组')
        prices_by_nm[nm] = price
    stocks_by_chrt = {}
    for stock in stocks:
        chrt = _id(stock.get('chrtId'))
        if (chrt not in chrt_ids or chrt in stocks_by_chrt
                or type(stock.get('amount')) is not int or stock['amount'] < 0):
            raise OpenAPIResponseError('库存响应含未知/重复规格或无效数量')
        stocks_by_chrt[chrt] = stock['amount']

    stats = {'missingPriceSpecs': 0, 'missingStockSpecs': 0, 'missingImages': 0}
    rows = []
    for card in cards:
        nm, vc = _id(card['nmID']), card['vendorCode'].strip()
        price = prices_by_nm.get(nm, {})
        if price.get('vendorCode') not in (None, vc):
            raise OpenAPIResponseError('商品卡与价格响应的供应商代码不一致')
        size_prices = {}
        specs = {_id(size['chrtID']) for size in card['sizes']}
        for size in price.get('sizes', []):
            chrt = _id(size.get('sizeID')) if isinstance(size, dict) else _id(None)
            if chrt not in specs or chrt in size_prices:
                raise OpenAPIResponseError('规格价格含未知或重复的规格 ID')
            size_prices[chrt] = size
        size_list = []
        for size in card['sizes']:
            chrt = _id(size['chrtID'])
            spec_price = size_prices.get(chrt, {})
            skus = size.get('skus', [])
            if not isinstance(skus, list) or any(not isinstance(sku, str) for sku in skus):
                raise OpenAPIResponseError('商品规格条码数组无效')
            item = {'chrtId': chrt, 'techSizeName': size.get('techSize'), 'skus': skus,
                    'stockList': ([{'warehouseId': warehouse['id'], 'amount': stocks_by_chrt[chrt]}]
                                  if chrt in stocks_by_chrt else [])}
            for key in ('price', 'discountedPrice', 'clubDiscountedPrice'):
                item[key] = _number(spec_price.get(key), key)
            stats['missingPriceSpecs'] += item['price'] is None
            stats['missingStockSpecs'] += chrt not in stocks_by_chrt
            size_list.append(item)
        photos = card.get('photos', [])
        dimensions = card.get('dimensions', {})
        if (not isinstance(photos, list) or any(not isinstance(photo, dict) for photo in photos)
                or not isinstance(dimensions, dict)):
            raise OpenAPIResponseError('商品图片或尺寸结构无效')
        images = [next((photo[key] for key in ('big', 'c516x688', 'c246x328')
                        if isinstance(photo.get(key), str) and photo[key]), '') for photo in photos]
        images = [url for url in images if url]
        stats['missingImages'] += not images
        currency = price.get('currencyIsoCode4217')
        if currency is not None and not isinstance(currency, str):
            raise OpenAPIResponseError('商品价格币种无效')
        row = {'shopId': shop_id, 'nmId': nm, 'vendorCode': vc, 'sizeList': size_list,
               'imtId': card.get('imtID'), 'nmUuid': card.get('nmUUID'),
               'subjectId': card.get('subjectID'), 'subjectName': card.get('subjectName'),
               'title': card.get('title'), 'brand': card.get('brand'),
               'repImg': images[0] if images else '', 'images': ','.join(images),
               'video': card.get('video'), 'createAt': card.get('createdAt'),
               'updateAt': card.get('updatedAt'), 'trashedAt': None,
               'discount': _number(price.get('discount'), 'discount', 100),
               'clubDiscount': _number(price.get('clubDiscount'), 'clubDiscount', 100),
               'currencyIsoCode': currency, 'editableSizePrice': price.get('editableSizePrice'),
               'dimensionsIsValid': dimensions.get('isValid')}
        for source, target in (('length', 'dimensionsLength'), ('width', 'dimensionsWidth'),
                               ('height', 'dimensionsHeight'), ('weightBrutto', 'dimensionsWeightBrutto')):
            row[target] = _number(dimensions.get(source), source)
        rows.append(row)
    return rows, stats


def collect_shop(shop: OpenAPIShop) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """完成本店全部读取后返回归一化行；本函数不写文件。"""
    api = WBOpenAPI(shop.api_key, region='cn')
    try:
        warehouse = resolve_moscow(api.warehouses())
        cards = api.cards()
        # 在库存请求前验证完整规格，避免只查询部分规格后生成成功快照。
        normalize_rows(shop.id, cards, [], [], warehouse)
        prices = api.prices([_id(card['nmID']) for card in cards])
        chrt_ids = [_id(size['chrtID']) for card in cards for size in card['sizes']]
        stocks = api.stocks(warehouse['id'], chrt_ids)
        rows, stats = normalize_rows(shop.id, cards, prices, stocks, warehouse)
        return rows, {'warehouse': warehouse, 'warnings': stats, 'region': 'cn'}
    finally:
        api.close()
