"""China-region WB FBS orders, supply submissions and printable files."""
import base64
import binascii
import re
import time
from typing import Any

import requests

from sellerops.integrations.wildberries.open_api import OpenAPIResponseError, WBOpenAPI


BATCH_SIZE = 100
_HOST = 'https://marketplace-api.wildberries.cn'


def valid_supply_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r'WB-GI-[0-9]{1,20}', value) is not None


class FBSRequestError(OpenAPIResponseError):
    def __init__(self, reason: str, status: int | None = None, *, uncertain: bool = False):
        super().__init__(reason, status)
        self.uncertain = uncertain


def _file(value: Any) -> bytes:
    try:
        if isinstance(value, str) and value:
            return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        pass
    raise OpenAPIResponseError('WB 返回的码图文件为空或编码无效')


def _order_rows(data: dict, key: str, id_field: str) -> dict[int, dict]:
    rows = data.get(key)
    if not isinstance(rows, list):
        raise OpenAPIResponseError(f'WB 响应缺少有效 {key} 列表')
    result = {}
    for row in rows:
        order_id = row.get(id_field) if isinstance(row, dict) else None
        if type(order_id) is not int or order_id < 1 or order_id in result:
            raise OpenAPIResponseError(f'WB {key} 包含缺失或重复的订单 ID')
        result[order_id] = row
    return result


class WBFBSAPI(WBOpenAPI):
    def __init__(self, token: str) -> None:
        super().__init__(token, region='cn')

    def _fbs(self, method: str, path: str, *, status: int = 200, write: bool = False,
             **kwargs: Any) -> dict:
        service = 'marketplace-api'
        for attempt in range(1 if write else 3):
            wait = .21 - (time.monotonic() - self._last.get(service, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[service] = time.monotonic()
            try:
                response = self.session.request(method, _HOST + path, timeout=30,
                                                allow_redirects=False, **kwargs)
            except requests.RequestException:
                if not write and attempt < 2:
                    continue
                raise FBSRequestError('WB 请求超时或连接失败', uncertain=write) from None
            received = response.status_code
            if received != status:
                if not write and received >= 500 and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                reasons = {401: '令牌失效', 403: '店铺令牌权限不足', 429: 'WB 限流'}
                raise FBSRequestError(reasons.get(received, 'WB FBS 未返回预期回执'), received,
                                      uncertain=write and received not in (400, 401, 402, 403, 404, 409, 429))
            if status == 204:
                return {}
            try:
                data = response.json()
            except ValueError:
                raise FBSRequestError('WB FBS 回执无法解析', received, uncertain=write) from None
            if not isinstance(data, dict) or data.get('error') or data.get('additionalErrors'):
                raise FBSRequestError('WB FBS 回执格式或业务结果异常', received, uncertain=write)
            return data
        raise AssertionError('WB FBS 请求未返回')

    def new_orders(self) -> list[dict]:
        data = self._fbs('GET', '/api/v3/orders/new')
        return list(_order_rows(data, 'orders', 'id').values())

    def metadata(self, ids: list[int]) -> dict[int, dict]:
        data = self._fbs('POST', '/api/marketplace/v3/orders/meta', json={'orders': ids})
        rows = _order_rows(data, 'orders', 'id')
        if set(rows) != set(ids):
            raise OpenAPIResponseError('订单必填标识响应未覆盖请求的全部订单')
        return rows

    def create_supply(self, name: str) -> str:
        data = self._fbs('POST', '/api/v3/supplies', json={'name': name}, status=201, write=True)
        supply_id = data.get('id')
        if not valid_supply_id(supply_id):
            raise FBSRequestError('建供货返回的供货号无效', 201, uncertain=True)
        return supply_id

    def add_orders(self, supply_id: str, ids: list[int]) -> None:
        if not valid_supply_id(supply_id) or not 1 <= len(ids) <= BATCH_SIZE:
            raise ValueError('供货号或加入供货的订单批量大小无效')
        self._fbs('PATCH', f'/api/marketplace/v3/supplies/{supply_id}/orders',
                  json={'orders': ids}, status=204, write=True)

    def deliver(self, supply_id: str) -> None:
        if not valid_supply_id(supply_id):
            raise ValueError('供货号无效')
        self._fbs('PATCH', f'/api/v3/supplies/{supply_id}/deliver', status=204, write=True)

    def supply_qr(self, supply_id: str) -> bytes:
        if not valid_supply_id(supply_id):
            raise ValueError('供货号无效')
        data = self._fbs('GET', f'/api/v3/supplies/{supply_id}/barcode', params={'type': 'png'})
        if data.get('barcode') != supply_id:
            raise OpenAPIResponseError('供货二维码返回的供货号不匹配')
        return _file(data.get('file'))

    def stickers(self, ids: list[int], *, cross_border: bool) -> dict[int, bytes]:
        path = '/api/v3/orders/stickers'
        if cross_border:
            data = self._fbs('POST', path + '/cross-border', json={'orders': ids})
        else:
            data = self._fbs('POST', path, json={'orders': ids},
                             params={'type': 'png', 'width': 40, 'height': 30})
        rows = _order_rows(data, 'stickers', 'orderId')
        if set(rows) != set(ids):
            raise OpenAPIResponseError('WB 返回的订单标签 ID 与本批订单不一致')
        if cross_border and any(row.get('status') != 'ready' for row in rows.values()):
            raise OpenAPIResponseError('跨境订单标签尚未就绪，请稍后续办导出')
        return {order_id: _file(row.get('file')) for order_id, row in rows.items()}
