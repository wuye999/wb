"""显式启用的 WB Content 写入通道；只读客户端的白名单保持独立。

协议：2026-11-02 https://dev.wildberries.cn/openapi/item-management 。
写请求只发送一次，未知受理状态不重放；不查询写入最终结果。
"""
import json
import re
import time
from typing import Any

import requests

from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.integrations.wildberries.open_api import WBOpenAPI, OpenAPIResponseError, _catalog_id


class WBContentAPI(WBOpenAPI):
    """每店独立令牌，默认仅请求中国区；调用方负责 --apply 和莫斯科仓确认。"""

    def __init__(self, token: str, region: str = 'cn') -> None:
        super().__init__(token, region)
        self._content_last: dict[str, float] = {}

    def _content(self, method: str, path: str, *, body: Any = None,
                 read: bool = False) -> Any:
        routes = {
            ('POST', '/content/v2/get/cards/trash'): (True, .61, 'content'),
            ('POST', '/content/v2/barcodes'): (False, .61, 'content'),
            ('POST', '/content/v2/cards/upload'): (False, 6.1, 'cards'),
            ('POST', '/content/v2/cards/update'): (False, 6.1, 'cards'),
            ('POST', '/content/v3/media/save'): (False, .61, 'media'),
        }
        route = routes.get((method, path))
        if method == 'GET' and re.fullmatch(r'/content/v2/object/charcs/[1-9][0-9]*', path):
            route = (True, .61, 'content')
        service = 'content-api'
        if method == 'PUT' and re.fullmatch(r'/api/v3/stocks/[1-9][0-9]*', path):
            route, service = (False, .21, 'stocks'), 'marketplace-api'
        if route is None or route[0] != read:
            raise ValueError('未声明的 WB Open API 操作')
        _, interval, bucket = route
        kwargs: dict[str, Any] = {} if body is None else {'json': body}
        if body is not None and len(json.dumps(body, ensure_ascii=False).encode('utf8')) > 10_000_000:
            raise ValueError('WB 请求超过 10 MB')
        for attempt in range(3 if read else 1):
            while service in self._last:
                remaining = (.61 if service == 'content-api' else .21) - (time.monotonic() - self._last[service])
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 30))
            while bucket in self._content_last:
                remaining = interval - (time.monotonic() - self._content_last[bucket])
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 30))
            self._content_last[bucket] = time.monotonic()
            self._last[service] = time.monotonic()
            try:
                response = self.session.request(method, f'https://{service}.wildberries.{self.region}{path}',
                                                timeout=60, allow_redirects=False, **kwargs)
            except requests.RequestException:
                if read and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise NetworkError('WB Open API 读取失败' if read else
                                   'WB Open API 提交响应未知，禁止自动重放') from None
            status = response.status_code
            if status in (401, 403):
                raise AuthenticationError('WB Open API 令牌失效或缺少对应类别的写入权限')
            if status == 429:
                raise RateLimitError('WB Open API 限流；请核对令牌类型和额度')
            if read and status >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if not 200 <= status < 300:
                raise OpenAPIResponseError('WB Open API 请求未确认受理', status_code=status)
            if method == 'PUT' and status == 204:
                return {'accepted': True, 'httpStatus': 204}
            try:
                data = response.json()
            except ValueError:
                raise OpenAPIResponseError('WB Open API 响应非 JSON，受理状态未知') from None
            if (not isinstance(data, dict) or (('error' in data or not read) and data.get('error') is not False)
                    or data.get('additionalErrors')):
                raise OpenAPIResponseError('WB Open API 未返回完整成功回执，受理状态未知')
            return data
        raise NetworkError('WB Open API 读取重试耗尽')

    def characteristics(self, subject_id: int) -> list[dict[str, Any]]:
        subject = _catalog_id(subject_id)
        if subject is None:
            raise ValueError('类目 ID 无效')
        data = self._content('GET', f'/content/v2/object/charcs/{subject}', read=True).get('data')
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise OpenAPIResponseError('类目属性响应不完整')
        return data

    def trash_cards(self, *, page_size: int = 100, max_pages: int = 10000) -> list[dict[str, Any]]:
        if not 1 <= page_size <= 100 or max_pages < 1:
            raise ValueError('回收站分页参数无效')
        rows, ids, cursors = [], set(), set()
        cursor: dict[str, Any] = {'limit': page_size}
        for _ in range(max_pages):
            data = self._content('POST', '/content/v2/get/cards/trash', read=True, body={
                'settings': {'sort': {'ascending': True}, 'cursor': cursor}})
            chunk, current = data.get('cards'), data.get('cursor')
            if not isinstance(chunk, list) or not isinstance(current, dict):
                raise OpenAPIResponseError('回收站响应缺少 cards/cursor')
            total = current.get('total')
            if type(total) is not int or total != len(chunk) or not 0 <= total <= page_size:
                raise OpenAPIResponseError('回收站分页数量不一致')
            for row in chunk:
                nm = _catalog_id(row.get('nmID')) if isinstance(row, dict) else None
                if nm is None or nm in ids:
                    raise OpenAPIResponseError('回收站分页标识无效或重复')
                ids.add(nm)
                rows.append(row)
            if total < page_size:
                return rows
            stamp, nm = current.get('trashedAt'), _catalog_id(current.get('nmID'))
            if not isinstance(stamp, str) or not stamp or nm is None or (stamp, nm) in cursors:
                raise OpenAPIResponseError('回收站游标缺失或未前进')
            cursors.add((stamp, nm))
            cursor = {'limit': page_size, 'trashedAt': stamp, 'nmID': nm}
        raise OpenAPIResponseError('回收站分页超过上限')

    def barcodes(self, count: int) -> list[str]:
        if type(count) is not int or not 1 <= count <= 5000:
            raise ValueError('条码数量必须在 1 到 5000 之间')
        values = self._content('POST', '/content/v2/barcodes', body={'count': count}).get('data')
        if (not isinstance(values, list) or len(values) != count
                or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(set(values)) != count):
            raise OpenAPIResponseError('生成条码数量缺失或重复')
        return values

    def create_cards(self, cards: list[dict[str, Any]]) -> dict[str, Any]:
        if not 1 <= len(cards) <= 100:
            raise ValueError('每批建卡须为 1 到 100 组')
        return self._receipt(self._content('POST', '/content/v2/cards/upload', body=cards))

    def update_cards(self, cards: list[dict[str, Any]]) -> dict[str, Any]:
        if not 1 <= len(cards) <= 3000:
            raise ValueError('每批更新须为 1 到 3000 张卡')
        return self._receipt(self._content('POST', '/content/v2/cards/update', body=cards))

    def save_media(self, nm_id: int, urls: list[str]) -> dict[str, Any]:
        if _catalog_id(nm_id) is None or not urls or len(urls) > 31:
            raise ValueError('图片/视频提交需要有效商品码和 1 到 31 个链接')
        return self._receipt(self._content('POST', '/content/v3/media/save',
                                           body={'nmId': nm_id, 'data': urls}))

    def set_stocks(self, warehouse_id: int, stocks: list[dict[str, int]]) -> dict[str, Any]:
        if (_catalog_id(warehouse_id) is None or not 1 <= len(stocks) <= 1000
                or any(_catalog_id(s.get('chrtId')) is None or type(s.get('amount')) is not int
                       or s['amount'] < 0 for s in stocks)
                or len({s['chrtId'] for s in stocks}) != len(stocks)):
            raise ValueError('库存仓库、规格或数量无效')
        return self._receipt(self._content('PUT', f'/api/v3/stocks/{warehouse_id}', body={'stocks': stocks}))

    @staticmethod
    def _receipt(data: dict[str, Any]) -> dict[str, Any]:
        # 不持久化远端任意文本；成功回执仅含已核实状态及平台任务号。
        receipt: dict[str, Any] = {'accepted': True, 'error': False}
        if data.get('httpStatus') == 204:
            receipt['httpStatus'] = 204
        task = data.get('data')
        if isinstance(task, dict) and _catalog_id(task.get('id')) is not None:
            receipt['taskId'] = int(task['id'])
        return receipt
