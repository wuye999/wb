"""操作前按需补齐本店商品标识；仅保留在内存，不刷新快照或切换写入通道。"""
from dataclasses import dataclass
from typing import Any

from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.domain.snapshot_values import positive_id, valid_sizes
from sellerops.integrations.bcs.client import client as bcs
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
        self.auth_failures: dict[int, str] = {}
        self._cache: dict[tuple, PreparedProduct] = {}

    def __enter__(self) -> 'ProductDataResolver':
        return self

    def __exit__(self, *args: Any) -> None:
        for api in self._clients.values():
            api.close()

    def block_after_auth_failure(self, sid, items, blocked):
        """同店任何一次官方鉴权失败后，连已准备好的本地商品也不再提交。"""
        if sid not in self.auth_failures:
            return
        seen = {row["vc"] for row in blocked}
        for item in items:
            vc = item.get("vc") or item["vendorCode"]
            if vc not in seen:
                blocked.append({"vc": vc, "cn": item.get("cn", ""), "reason": self.auth_failures[sid]})
                seen.add(vc)
        items.clear()

    def _client(self, sid: int) -> WBOpenAPI:
        if sid in self._stopped:
            raise OpenAPIResponseError(self._stopped[sid])
        if self._accesses is None and self._access_error is None:
            try:
                self._accesses = {shop.id: shop for shop in bcs.fetch_open_api_shops()}
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
    def _missing(row: dict, nm: bool, specs: bool, price: bool) -> str | None:
        if nm and positive_id(row.get('nmId')) is None:
            return '缺少有效nmId'
        if specs and valid_sizes(row) is None:
            return '缺少完整且唯一的规格ID'
        if price:
            sizes = row.get('sizeList')
            if not isinstance(sizes, list) or not sizes or any(
                    not isinstance(size, dict) or size.get('price') in (None, '', 0, '0') for size in sizes):
                return '缺少可确认的全部规格价格'
        return None

    def current_discounts(self, sid: int, rows: dict[str, dict]) -> tuple[dict[str, int], dict[str, str]]:
        """通过本店官方价格接口读取这次条件判断需要的折扣。"""
        if not rows:
            return {}, {}
        by_nm = {positive_id(row["nmId"]): vc for vc, row in rows.items()}
        try:
            api = self._client(sid)
            records = api.prices(list(by_nm))
            seen = set()
            values, blocked = {}, {}
            for row in records:
                nm = positive_id(row.get("nmID")) if isinstance(row, dict) else None
                if nm not in by_nm or nm in seen:
                    raise OpenAPIResponseError("本店价格响应含未知或重复的商品码")
                seen.add(nm)
                vc = by_nm[nm]
                if row.get("vendorCode") not in (None, vc):
                    blocked[vc] = "本店价格响应的完整 VC 与目标不一致"
                elif valid_discount(row.get("discount")):
                    values[vc] = row["discount"]
            for vc in rows.keys() - values.keys():
                blocked.setdefault(vc, "本店平台当前折扣缺失或无效")
        except Exception as error:
            reason = safe_reason(error)
            if isinstance(error, AuthenticationError) and sid in self._clients:
                self.auth_failures[sid] = reason
            return {}, dict.fromkeys(rows, reason)
        return values, blocked

    def ensure(self, sid: int, vc: str, row: dict, *, nm: bool = True, specs: bool = False,
               price: bool = False) -> PreparedProduct:
        """只补齐当前操作所需字段；按完整VC匹配并校验已知nmId，不读取库存或其他商品。"""
        if row.get('shopId') is not None and str(row['shopId']) != str(sid):
            return PreparedProduct(row, '商品记录归属与目标店铺不一致')
        if sid in self.auth_failures:
            return PreparedProduct(row, self.auth_failures[sid])
        if row.get('vendorCode') not in (None, vc):
            return PreparedProduct(row, '商品记录的vendorCode与目标不一致')
        missing = self._missing(row, nm, specs, price)
        if not missing:
            return PreparedProduct(row)
        key = (sid, vc, nm, specs, price)
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
            if price:
                prices = api.prices([positive_id(fixed.get('nmId'))])
                if len(prices) != 1 or positive_id(prices[0].get('nmID')) != positive_id(fixed.get('nmId')):
                    raise OpenAPIResponseError('本店商品未返回唯一价格记录')
                goods = prices[0]
                if goods.get('vendorCode') not in (None, vc):
                    raise OpenAPIResponseError('本店价格记录的完整VC不一致')
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
            reason = self._missing(fixed, nm, specs, price)
            if reason:
                raise OpenAPIResponseError(f'定向补齐后仍{reason}')
        except Exception as error:
            reason = safe_reason(error)
            if isinstance(error, AuthenticationError) and sid in self._clients:
                self.auth_failures[sid] = reason
            if isinstance(error, (AuthenticationError, RateLimitError, NetworkError)):
                self._stopped[sid] = reason
            result = PreparedProduct(row, f'{missing}；{reason}')
        else:
            print(f'[数据补齐] 店{sid} {vc}：已取得本次操作所需字段（仅本次计划使用）')
            result = PreparedProduct(fixed, repaired=True)
        self._cache[key] = result
        return result
