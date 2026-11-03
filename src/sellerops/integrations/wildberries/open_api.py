"""WB 官方只读接口。协议核对：2026-11-02，dev.wildberries.cn/docs/openapi。

订单动态不是后台 order-feed 协议；固定 snapshotTime 后按实际行数翻页。
仅允许本文件列出的只读端点，不使用 Cookie、浏览器续期或跨域兜底。
"""
from datetime import datetime, timedelta
import re
import time
from typing import Any

import requests

from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError, RateLimitError


class OpenAPIResponseError(PlatformApiError):
    """只包含本客户端生成的安全说明，不附带远端响应或认证资料。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.public_reason = message
        super().__init__(message, status_code=status_code)


def _catalog_id(value: Any) -> int | None:
    if type(value) not in (int, str) or not str(value).isdigit() or int(value) <= 0:
        return None
    return int(value)


def _catalog_ids(values: list[int]) -> list[int]:
    result = [_catalog_id(value) for value in values]
    if any(value is None for value in result) or len(set(result)) != len(result):
        raise ValueError('WB 查询标识必须为不重复的正整数')
    return result


class WBOpenAPI:
    """每店独立会话；仅适用于个人、服务或带密钥的基础令牌的常规限额。"""

    def __init__(self, token: str, region: str = 'ru') -> None:
        if region not in {'ru', 'cn'}:
            raise ValueError('WB 官方主机区域必须为 ru 或 cn')
        token = token.strip()
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        if not token or '\r' in token or '\n' in token:
            raise AuthenticationError('缺少有效 WB Open API 令牌')
        self.region = region
        self.session = requests.Session()
        self.session.headers.update({'Authorization': token, 'Accept': 'application/json'})
        self._last: dict[str, float] = {}

    def close(self) -> None:
        self.session.close()

    def _request(self, service: str, method: str, path: str, **kwargs: Any) -> Any:
        allowed = {
            ('content-api', 'POST', '/content/v2/get/cards/list'): 0.61,
            ('discounts-prices-api', 'POST', '/api/v2/list/goods/filter'): 0.61,
            ('marketplace-api', 'GET', '/api/v3/warehouses'): 0.21,
            ('marketplace-api', 'GET', '/api/v3/orders'): 0.21,
            ('seller-analytics-api', 'POST', '/api/analytics/v1/order-feed'): 60.1,
            ('feedbacks-api', 'GET', '/api/v1/feedbacks'): 0.35,
            ('feedbacks-api', 'GET', '/api/v1/feedbacks/archive'): 0.35,
        }
        interval = allowed.get((service, method, path))
        if (service == 'marketplace-api' and method == 'POST'
                and re.fullmatch(r'/api/v3/stocks/[1-9][0-9]*', path)):
            interval = 0.21
        if interval is None:
            raise ValueError('该客户端仅允许已声明的只读接口')
        url = f'https://{service}.wildberries.{self.region}{path}'
        for attempt in range(3):
            while service in self._last:
                remaining = interval - (time.monotonic() - self._last[service])
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 30))
            self._last[service] = time.monotonic()
            try:
                response = self.session.request(method, url, timeout=60, allow_redirects=False, **kwargs)
            except (requests.Timeout, requests.ConnectionError):
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise NetworkError(f'WB Open API {service} 网络读取失败') from None
            except requests.RequestException:
                raise NetworkError(f'WB Open API {service} 请求失败') from None
            status = response.status_code
            if status in {401, 403}:
                raise AuthenticationError(f'WB Open API {service} 令牌失效或类别权限不足')
            if status == 429:
                # 不自动等待数小时或重复请求低限额基础令牌。
                raise RateLimitError(f'WB Open API {service} 限流；请核对令牌类型和额度')
            if status >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if not 200 <= status < 300:
                raise OpenAPIResponseError(f'WB Open API {service} 读取失败', status_code=status)
            try:
                data = response.json()
            except ValueError:
                raise OpenAPIResponseError(f'WB Open API {service} 返回非 JSON') from None
            if isinstance(data, dict) and (('error' in data and data['error'] is not False)
                                           or data.get('additionalErrors')):
                raise OpenAPIResponseError(f'WB Open API {service} 返回业务错误')
            return data
        raise NetworkError(f'WB Open API {service} 重试耗尽')

    def warehouses(self) -> list[dict[str, Any]]:
        """读取卖家仓库目录；id 为仓库标识，officeId 不可替代。"""
        rows = self._request('marketplace-api', 'GET', '/api/v3/warehouses')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise OpenAPIResponseError('WB Open API 仓库目录响应不完整')
        return rows

    def cards(self, *, page_size: int = 100, max_pages: int = 10000,
              vendor_code: str | None = None) -> list[dict[str, Any]]:
        """完整读取非回收站商品卡；可按 VC 搜索，调用方仍须按完整 VC 精确匹配。"""
        if not 1 <= page_size <= 100 or max_pages < 1:
            raise ValueError('商品卡分页参数无效')
        filters: dict[str, Any] = {'withPhoto': -1}
        if vendor_code is not None:
            if not isinstance(vendor_code, str) or not vendor_code.strip() or vendor_code != vendor_code.strip():
                raise ValueError('商品卡 VC 搜索条件无效')
            filters['textSearch'] = vendor_code
        rows: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        cursors: set[tuple[str, int]] = set()
        cursor: dict[str, Any] = {'limit': page_size}
        for _ in range(max_pages):
            response = self._request('content-api', 'POST', '/content/v2/get/cards/list', json={
                'settings': {'sort': {'ascending': True}, 'filter': dict(filters),
                             'cursor': dict(cursor)},
            })
            chunk = response.get('cards') if isinstance(response, dict) else None
            current = response.get('cursor') if isinstance(response, dict) else None
            if not isinstance(chunk, list) or not isinstance(current, dict):
                raise OpenAPIResponseError('WB 商品卡页缺少 cards/cursor')
            total = current.get('total')
            if type(total) is not int or total != len(chunk) or total > page_size:
                raise OpenAPIResponseError('WB 商品卡分页数量不一致')
            for row in chunk:
                nm = _catalog_id(row.get('nmID')) if isinstance(row, dict) else None
                if nm is None or nm in seen_ids:
                    raise OpenAPIResponseError('WB 商品卡缺少有效 nmID 或分页重复')
                seen_ids.add(nm)
                rows.append(row)
            if total < page_size:
                return rows
            stamp, nm = current.get('updatedAt'), _catalog_id(current.get('nmID'))
            if not isinstance(stamp, str) or not stamp or nm is None or (stamp, nm) in cursors:
                raise OpenAPIResponseError('WB 商品卡游标缺失或没有前进')
            cursors.add((stamp, nm))
            cursor = {'limit': page_size, 'updatedAt': stamp, 'nmID': nm}
        raise OpenAPIResponseError('WB 商品卡超过分页上限，结果不完整')

    def prices(self, nm_ids: list[int]) -> list[dict[str, Any]]:
        """按本店商品码分批读取全部规格价格；合法未收录商品由上层标为缺失。"""
        ids = _catalog_ids(nm_ids)
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for offset in range(0, len(ids), 1000):
            batch = ids[offset:offset + 1000]
            response = self._request('discounts-prices-api', 'POST', '/api/v2/list/goods/filter',
                                     json={'nmList': batch})
            data = response.get('data') if isinstance(response, dict) else None
            chunk = data.get('listGoods') if isinstance(data, dict) else None
            if not isinstance(chunk, list):
                raise OpenAPIResponseError('WB 价格响应缺少 data.listGoods')
            for row in chunk:
                nm = _catalog_id(row.get('nmID')) if isinstance(row, dict) else None
                if nm not in batch or nm in seen:
                    raise OpenAPIResponseError('WB 价格响应含未知或重复的商品码')
                seen.add(nm)
                rows.append(row)
        return rows

    def stocks(self, warehouse_id: int, chrt_ids: list[int]) -> list[dict[str, Any]]:
        """只读取调用方已确认仓库的指定规格；省略的库存记录不补零。"""
        warehouse = _catalog_id(warehouse_id)
        if warehouse is None:
            raise ValueError('WB 库存查询缺少有效仓库 ID')
        ids = _catalog_ids(chrt_ids)
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for offset in range(0, len(ids), 1000):
            batch = ids[offset:offset + 1000]
            response = self._request('marketplace-api', 'POST', f'/api/v3/stocks/{warehouse}',
                                     json={'chrtIds': batch})
            chunk = response.get('stocks') if isinstance(response, dict) else None
            if not isinstance(chunk, list):
                raise OpenAPIResponseError('WB 库存响应缺少 stocks 数组')
            for row in chunk:
                chrt = _catalog_id(row.get('chrtId')) if isinstance(row, dict) else None
                amount = row.get('amount') if isinstance(row, dict) else None
                if chrt not in batch or chrt in seen or type(amount) is not int or amount < 0:
                    raise OpenAPIResponseError('WB 库存响应含无效、未知或重复规格')
                seen.add(chrt)
                rows.append(row)
        return rows

    def orders(self, start: datetime, end: datetime, *, page_size: int = 10000,
               max_pages: int = 1000) -> list[dict[str, Any]]:
        """按状态变更时间取超集；调用方再按下单时间及莫斯科仓过滤。"""
        if not 1 <= page_size <= 10000 or max_pages < 1:
            raise ValueError('订单分页参数无效')
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        snapshot = None
        offset = 0
        for _ in range(max_pages):
            pagination: dict[str, Any] = {'limit': page_size, 'offset': offset}
            if snapshot is not None:
                pagination['snapshotTime'] = snapshot
            body = {'selectedPeriod': {'start': start.isoformat(), 'end': end.isoformat()},
                    'nmIds': [], 'subjectIds': [], 'brandNames': [], 'tagIds': [],
                    'pagination': pagination}
            response = self._request('seller-analytics-api', 'POST',
                                     '/api/analytics/v1/order-feed', json=body)
            data = response.get('data') if isinstance(response, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get('orders'), list):
                raise OpenAPIResponseError('WB Open API 订单页缺少 data.orders')
            current = data.get('snapshotTime')
            chunk = data['orders']
            # 实际接口在末页使用空字符串；满页却没有游标仍不能安全翻页。
            if (not isinstance(current, str) or (not current and len(chunk) >= page_size)
                    or (snapshot and current and current != snapshot)):
                raise OpenAPIResponseError('WB Open API 订单分页快照缺失或发生变化')
            if current:
                snapshot = current
            for row in chunk:
                srid = row.get('srid') if isinstance(row, dict) else None
                if not isinstance(srid, str) or not srid or srid in seen:
                    raise OpenAPIResponseError('WB Open API 订单分页包含缺失或重复的 srid')
                seen.add(srid)
                rows.append(row)
            if not chunk or not current:
                return rows
            # 非末页不假设服务端完整遵守 limit，继续使用首个快照。
            offset += len(chunk)
        raise OpenAPIResponseError('WB Open API 订单超过分页上限，结果不完整')

    def fbs_orders(self, start: datetime, end: datetime, *, page_size: int = 1000,
                   max_pages: int = 1000) -> list[dict[str, Any]]:
        """读取订单 rid、warehouseId 和 article，用于同店订单动态的可靠归属。"""
        if not 1 <= page_size <= 1000 or max_pages < 1 or end < start:
            raise ValueError('FBS 订单窗口或分页参数无效')
        found: dict[str, dict[str, Any]] = {}
        begin = start
        # 官方每请求最多30个UTC日；28天分段兼容本报告UTC+8日界，边界重叠去重。
        while begin < end:
            finish = min(begin + timedelta(days=28), end)
            cursor = 0
            seen: set[str] = set()
            for _ in range(max_pages):
                response = self._request('marketplace-api', 'GET', '/api/v3/orders', params={
                    'limit': page_size, 'next': cursor, 'dateFrom': int(begin.timestamp()),
                    'dateTo': int(finish.timestamp()),
                })
                if not isinstance(response, dict) or not isinstance(response.get('orders'), list):
                    raise OpenAPIResponseError('WB FBS 订单页缺少 orders 数组')
                chunk = response['orders']
                if not chunk:
                    break
                for row in chunk:
                    rid = row.get('rid') if isinstance(row, dict) else None
                    warehouse = row.get('warehouseId') if isinstance(row, dict) else None
                    if (not isinstance(rid, str) or not rid or rid in seen
                            or isinstance(warehouse, bool) or not str(warehouse).isdigit() or int(warehouse) <= 0):
                        raise OpenAPIResponseError('WB FBS 订单缺少有效 rid/warehouseId 或分页重复')
                    seen.add(rid)
                    previous = found.get(rid)
                    if previous and any(str(previous.get(key)) != str(row.get(key))
                                        for key in ('warehouseId', 'nmId', 'article')):
                        raise OpenAPIResponseError('WB FBS 订单分段边界的商品或仓库归属不一致')
                    found[rid] = row
                nxt = response.get('next')
                if isinstance(nxt, bool) or not isinstance(nxt, int) or nxt <= cursor:
                    raise OpenAPIResponseError('WB FBS 订单分页游标缺失或没有前进')
                cursor = nxt
            else:
                raise OpenAPIResponseError('WB FBS 订单超过分页上限，仓库归属不完整')
            begin = finish
        return list(found.values())
    def feedbacks(self, *, date_from: int | None = None, date_to: int | None = None,
                  page_size: int = 5000, max_pages: int = 1000) -> list[dict[str, Any]]:
        """未处理、已处理、归档三路完整读取，按评价 ID 去重；归档日期由上层过滤。"""
        if not 1 <= page_size <= 5000 or max_pages < 1:
            raise ValueError('评价分页参数无效')
        found: dict[str, dict[str, Any]] = {}
        for answered in (False, True, None):
            path = '/api/v1/feedbacks' + ('/archive' if answered is None else '')
            skip = 0
            seen: set[str] = set()
            for _ in range(max_pages):
                if answered is not None and skip > 199990:
                    raise OpenAPIResponseError('WB Open API 评价达到 skip 上限，历史覆盖不完整')
                params: dict[str, Any] = {'take': page_size, 'skip': skip, 'order': 'dateDesc'}
                if answered is not None:
                    params['isAnswered'] = 'true' if answered else 'false'
                    if date_from is not None:
                        params['dateFrom'] = date_from
                    if date_to is not None:
                        params['dateTo'] = date_to
                response = self._request('feedbacks-api', 'GET', path, params=params)
                data = response.get('data') if isinstance(response, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get('feedbacks'), list):
                    raise OpenAPIResponseError('WB Open API 评价页缺少 data.feedbacks')
                chunk = data['feedbacks']
                if not chunk:
                    break
                for row in chunk:
                    fid = row.get('id') if isinstance(row, dict) else None
                    if not isinstance(fid, str) or not fid or fid in seen:
                        raise OpenAPIResponseError('WB Open API 评价分页缺少 ID 或没有前进')
                    seen.add(fid)
                    found[fid] = row
                skip += len(chunk)
            else:
                raise OpenAPIResponseError('WB Open API 评价超过分页上限，历史覆盖不完整')
        return list(found.values())
