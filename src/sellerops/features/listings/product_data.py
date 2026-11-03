"""操作前按需补齐本店商品标识；仅保留在内存，不刷新快照或切换写入通道。"""
from dataclasses import dataclass
from typing import Any

from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.domain.snapshot_values import first_price, positive_id, valid_sizes
from sellerops.integrations.bcs import client as bcs
from sellerops.integrations.wildberries.open_api import WBOpenAPI, OpenAPIResponseError


def safe_reason(error: Exception) -> str:
    if isinstance(error, OpenAPIResponseError):
        return error.public_reason
    if isinstance(error, AuthenticationError):
        return '官方令牌失效、缺失或权限不足，请在BCS更新该店密钥'
    if isinstance(error, RateLimitError):
        return '官方接口限流，本店停止补齐'
    if isinstance(error, NetworkError):
        return '官方接口网络读取失败'
    return f'商品数据补齐失败（{type(error).__name__}）'


def valid_discount(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 100


@dataclass
class PreparedProduct:
    row: dict
    reason: str | None = None
    repaired: bool = False


class ProductDataResolver:
    """一轮计划共享的惰性只读解析器；完整本地数据不联网，失败不借用别店数据。"""

    def __init__(self) -> None:
        self._accesses: dict | None = None
        self._access_error: str | None = None
        self._clients: dict[int, WBOpenAPI] = {}
        self._stopped: dict[int, str] = {}
        self._cache: dict[tuple, PreparedProduct] = {}

    def __enter__(self) -> 'ProductDataResolver':
        return self

    def __exit__(self, *args: Any) -> None:
        for api in self._clients.values():
            api.close()

    def _client(self, sid: int) -> WBOpenAPI:
        if sid in self._stopped:
            raise OpenAPIResponseError(self._stopped[sid])
        if self._accesses is None and self._access_error is None:
            try:
                self._accesses = {shop.id: shop for shop in bcs.client.fetch_open_api_shops()}
            except Exception as error:
                self._access_error = safe_reason(error)
        if self._access_error:
            raise OpenAPIResponseError(self._access_error)
        access = (self._accesses or {}).get(sid)
        if access is None or not access.api_key:
            raise AuthenticationError('缺少本店官方令牌')
        if sid not in self._clients:
            self._clients[sid] = WBOpenAPI(access.api_key, region='cn')
        return self._clients[sid]

    @staticmethod
    def _missing(row: dict, nm: bool, specs: bool, price: bool, discount: bool) -> str | None:
        if nm and positive_id(row.get('nmId')) is None:
            return '缺少有效nmId'
        if specs and valid_sizes(row) is None:
            return '缺少完整且唯一的规格ID'
        if price and first_price(row) is None:
            return '缺少可确认的当前价格'
        if discount and not valid_discount(row.get('discount')):
            return '缺少可确认的当前折扣'
        return None

    def ensure(self, sid: int, vc: str, row: dict, *, nm: bool = True, specs: bool = False,
               price: bool = False, discount: bool = False) -> PreparedProduct:
        """只补齐当前操作所需字段；按完整VC匹配并校验已知nmId，不读取库存或其他商品。"""
        if row.get('shopId') is not None and str(row['shopId']) != str(sid):
            return PreparedProduct(row, '商品记录归属与目标店铺不一致')
        if row.get('vendorCode') not in (None, vc):
            return PreparedProduct(row, '商品记录的vendorCode与目标不一致')
        missing = self._missing(row, nm, specs, price, discount)
        if not missing:
            return PreparedProduct(row)
        key = (sid, vc, nm, specs, price, discount)
        if key in self._cache:
            return self._cache[key]
        fixed = dict(row)
        try:
            api = self._client(sid)
            if positive_id(fixed.get('nmId')) is None or ((specs or price) and valid_sizes(fixed) is None):
                cards = api.cards(vendor_code=vc, max_pages=20)
                matches = [card for card in cards if card.get('vendorCode') == vc and not card.get('trashedAt')]
                if len(matches) != 1:
                    raise OpenAPIResponseError('本店完整VC未匹配到唯一非回收站商品')
                card = matches[0]
                live_nm, old_nm = positive_id(card.get('nmID')), positive_id(fixed.get('nmId'))
                if live_nm is None or (old_nm is not None and live_nm != old_nm):
                    raise OpenAPIResponseError('定向商品nmId无效或与本店已有标识冲突')
                fixed['nmId'] = live_nm
                if specs or price:
                    sizes = card.get('sizes')
                    fixed['sizeList'] = ([{'chrtId': size.get('chrtID')} if isinstance(size, dict) else {}
                                          for size in sizes] if isinstance(sizes, list) else [])
                    if valid_sizes(fixed) is None:
                        raise OpenAPIResponseError('官方商品仍缺少完整且唯一的规格ID')
            if price or discount:
                prices = api.prices([positive_id(fixed.get('nmId'))])
                if len(prices) != 1 or positive_id(prices[0].get('nmID')) != positive_id(fixed.get('nmId')):
                    raise OpenAPIResponseError('本店商品未返回唯一价格记录')
                goods = prices[0]
                if goods.get('vendorCode') not in (None, vc):
                    raise OpenAPIResponseError('本店价格记录的完整VC不一致')
                if discount:
                    fixed['discount'] = goods.get('discount')
                if price:
                    by_id = {}
                    for size in goods.get('sizes', []):
                        chrt = positive_id(size.get('sizeID')) if isinstance(size, dict) else None
                        if chrt is None or chrt in by_id:
                            raise OpenAPIResponseError('价格响应包含无效或重复规格ID')
                        by_id[chrt] = size.get('price')
                    ids = {positive_id(size['chrtId']) for size in fixed['sizeList']}
                    if set(by_id) - ids:
                        raise OpenAPIResponseError('价格规格与本店商品规格不一致')
                    fixed['sizeList'] = [dict(size, price=by_id.get(positive_id(size['chrtId'])))
                                         for size in fixed['sizeList']]
            reason = self._missing(fixed, nm, specs, price, discount)
            if reason:
                raise OpenAPIResponseError(f'定向补齐后仍{reason}')
        except Exception as error:
            reason = safe_reason(error)
            if isinstance(error, (AuthenticationError, RateLimitError, NetworkError)):
                self._stopped[sid] = reason
            result = PreparedProduct(row, f'{missing}；{reason}')
        else:
            print(f'[数据补齐] 店{sid} {vc}：已取得本次操作所需字段（仅本次计划使用）')
            result = PreparedProduct(fixed, repaired=True)
        self._cache[key] = result
        return result
