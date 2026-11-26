"""
sellerops BCS ERP 云端 API 客户端适配器 (BCSClient)
封装与 wb.bcserp.com 的全部 HTTP 通信：自动退避、限频处理、店铺列表、商品拉取、批量上品与同步。
"""
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any, Dict, List, Optional

import requests

from sellerops.core.errors import (
    AuthenticationError, DataConsistencyError, NetworkError, PlatformApiError, RateLimitError, StorageLockError,
)
from sellerops.domain.bcs_products import deduplicate_bcs_products
from sellerops.integrations.bcs.auth import auto_refresh_enabled, refresh_login
from sellerops.integrations.bcs.budget import RequestBudget, SyncInterrupted
from sellerops.integrations.bcs.session import BCS_BASE_URL, BCSSession
from sellerops.settings import credentials, paths as config

RETRY_MAX = 3
PAGE_SIZE_BIG = 10000
SYNC_STAGE_SECONDS = 25 * 60
SYNC_HEARTBEAT_SECONDS = 30
READ_METHODS = {'GET', 'HEAD', 'OPTIONS'}
_SYNC_TASK_LOCK = Lock()
_last_sync_task_ms = 0


def sync_failure_reason(error: Exception) -> str:
    if isinstance(error, AuthenticationError):
        return 'BCS 会话无效或缺少权限'
    if isinstance(error, RateLimitError):
        return 'BCS 请求受到限流'
    if isinstance(error, SyncInterrupted):
        return str(error)
    if isinstance(error, NetworkError):
        return 'BCS 网络请求失败'
    if isinstance(error, PlatformApiError):
        code = error.status_code
        return f'BCS 请求失败（错误码 {code}）' if type(code) is int else 'BCS 响应不完整或请求被拒绝'
    return 'BCS 请求异常'


class _SessionExpired(AuthenticationError):
    """The platform explicitly rejected authentication for this session."""


def _check_business_auth(data: Any) -> None:
    if isinstance(data, dict) and data.get('code') in (401, '401', 403, '403'):
        raise AuthenticationError('BCS 会话无效或缺少权限')


def _pause(seconds: float, budget: RequestBudget | None) -> None:
    if budget is None:
        time.sleep(seconds)
    else:
        budget.pause(seconds)


def _log_sync_warning(warning: dict) -> None:
    stage = {'submit': '提交同步', 'poll': '轮询进度', 'not_started': '尚未启动'}[warning['stage']]
    print(f"[同步警告] 店铺 {warning['id']} | {stage} | {warning['reason']}", flush=True)


@dataclass(frozen=True)
class OpenAPIShop:
    """BCS 店铺的私有 Open API 访问资料；密钥不参与 repr。"""

    id: int
    name: str
    api_key: str = field(repr=False)


class BCSClient:
    """BCS ERP 客户端"""

    def __init__(self, session: BCSSession | None = None, *, auto_refresh: bool = True) -> None:
        self._session = session
        self._auto_refresh = auto_refresh

    @property
    def base_url(self) -> str:
        return BCS_BASE_URL if self._session is not None else credentials.get().base_url

    def _headers(self) -> Dict[str, str]:
        return self._session.headers() if self._session is not None else credentials.get().bcs_headers()

    def request_json(self, method: str, url: str, retry: int = RETRY_MAX,
                     idempotent: bool = False, *, mutating: bool = False,
                     budget: RequestBudget | None = None, **kwargs) -> Dict[str, Any]:
        """Renew a configured session; only retry a rejected business read."""
        method = method.upper()
        read_only = method in READ_METHODS and not mutating
        attempts = max(1, retry) if read_only or (idempotent and not mutating) else 1
        current = None
        renewal = {}
        if budget is not None:
            budget.remaining()
            renewal['budget'] = budget
            kwargs.update(budget=budget, allow_redirects=False)
        if not self._auto_refresh:
            kwargs['allow_redirects'] = False
            if self._session is None:
                saved = credentials.get()
                if not saved.token or not saved.limit_key:
                    raise AuthenticationError('BCS 暂无已保存会话')
                return self._send_json(method, url, saved.bcs_headers(), attempts, **kwargs)
        elif self._session is None and url.startswith(BCS_BASE_URL + '/'):
            candidate = credentials.get()
            if auto_refresh_enabled(candidate):
                current = candidate
                kwargs['allow_redirects'] = False
        if current is None:
            return self._send_json(method, url, self._headers(), attempts, **kwargs)
        if not current.token or not current.limit_key:
            updated = refresh_login(expected=current, **renewal)
            return self._send_json(method, url, updated.bcs_headers(), attempts, **kwargs)
        try:
            return self._send_json(method, url, current.bcs_headers(), attempts, **kwargs)
        except _SessionExpired:
            try:
                updated = refresh_login(expected=current, **renewal)
            except SyncInterrupted as error:
                if error.kind == 'authentication_aborted':
                    raise
                raise AuthenticationError('BCS 会话已失效，时间预算内未能恢复会话') from None
            except (DataConsistencyError, OSError, StorageLockError):
                if budget is None:
                    raise
                raise AuthenticationError('BCS 会话已失效，本机无法恢复会话') from None
            except AuthenticationError as error:
                if read_only:
                    raise
                raise AuthenticationError(f'BCS 业务请求鉴权失败，未重新提交。{error}') from None
            if not read_only:
                raise AuthenticationError('BCS 业务请求鉴权失败。会话已更新，本次请求未重新提交。') from None
        return self._send_json(method, url, updated.bcs_headers(), 1, **kwargs)

    def _send_json(self, method: str, url: str, headers: Dict[str, str], attempts: int,
                   *, timeout=60, budget: RequestBudget | None = None, **kwargs) -> Dict[str, Any]:
        if method == 'POST':
            headers = {**headers, 'Content-Type': 'application/json;charset=UTF-8'}
        for attempt in range(attempts):
            try:
                bounded_timeout = budget.timeout(timeout) if budget else timeout
                response = requests.request(method, url, headers=headers, timeout=bounded_timeout, **kwargs)
                if response.status_code == 401:
                    raise _SessionExpired('BCS 凭证无效或已过期')
                if response.status_code == 403:
                    raise AuthenticationError('BCS 请求被拒绝，请检查账号权限或网关状态')
                if kwargs.get('allow_redirects') is False and 300 <= response.status_code < 400:
                    raise PlatformApiError('BCS 返回意外重定向', status_code=response.status_code)
                if response.status_code == 429:
                    if attempt + 1 < attempts:
                        _pause(3 * (attempt + 1), budget)
                        continue
                    raise RateLimitError('BCS 请求受到限流')
                if response.status_code >= 500 and attempt + 1 < attempts:
                    _pause(2 * (attempt + 1), budget)
                    continue
                response.raise_for_status()
                data = response.json()
                if self._session is None and isinstance(data, dict) and data.get('code') in (401, '401'):
                    raise _SessionExpired('BCS 凭证无效或已过期')
                if budget:
                    _check_business_auth(data)
                return data
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if budget:
                    budget.remaining()
                if attempt + 1 == attempts:
                    raise NetworkError('BCS 网络请求失败') from exc
                _pause(2 * (attempt + 1), budget)
            except requests.exceptions.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else None
                raise PlatformApiError('BCS HTTP 请求失败', status_code=code) from exc
            except requests.exceptions.JSONDecodeError as exc:
                raise PlatformApiError('BCS 返回非 JSON 数据') from exc
        raise NetworkError('BCS 请求重试耗尽')

    def get(self, url: str, retry: int = RETRY_MAX, *, budget: RequestBudget | None = None) -> Dict[str, Any]:
        return self.request_json("GET", url, retry, **({'budget': budget} if budget else {}))

    def post(self, url: str, data: Any, retry: int = RETRY_MAX) -> Dict[str, Any]:
        return self.request_json("POST", url, retry, json=data)

    def fetch_shop_list(self, *, budget: RequestBudget | None = None) -> List[Dict[str, Any]]:
        """账号全部店铺 → [{'id', 'name'}]"""
        if budget:
            return self._fetch_shops(lambda sid, row: {'id': sid, 'name': str(row.get('shopName') or sid)},
                                     budget=budget)
        return [{'id': shop.id, 'name': shop.name} for shop in self.fetch_open_api_shops()]

    def get_main_shop(self) -> int:
        if config.MAIN_SHOP:
            return config.MAIN_SHOP
        shops = self.fetch_shop_list()
        if not shops:
            raise PlatformApiError("未获取到店铺列表")
        return shops[0]["id"]

    def fetch_open_api_shops(self) -> List[OpenAPIShop]:
        """完整分页读取店铺访问资料，供显式 Open API 集成内部使用，不打印原始行。"""
        return self._fetch_shops(lambda shop_id, row: OpenAPIShop(
            shop_id, str(row.get('shopName') or shop_id), str(row.get('apiKey') or '').strip()))

    def fetch_binding_shops(self) -> list[dict]:
        """Project BCS identities from an explicit saved session without renewing it."""
        if self._session is None:
            raise AuthenticationError('BCS 暂无已保存会话')
        def identity(shop_id: int, row: dict) -> dict:
            name, sid = row.get('shopName'), row.get('sid')
            return {'id': shop_id, 'name': name if isinstance(name, str) and name.strip() else str(shop_id),
                    'sid': sid if isinstance(sid, str) else None}

        return self._fetch_shops(identity, deadline=time.monotonic() + 15)

    def _fetch_shops(self, project, *, deadline: float | None = None,
                     budget: RequestBudget | None = None) -> list:
        shops = []
        seen = set()
        for page in range(1, 1001):
            url = f"{self.base_url}/system/wbShop/user/list?pageNum={page}&pageSize=100"
            if budget:
                d = self.get(url, budget=budget)
            elif deadline is None:
                d = self.get(url)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkError('读取 BCS 店铺列表超时')
                timeout = min(3, remaining / 2)
                d = self.request_json('GET', url, retry=1, timeout=(timeout, timeout), allow_redirects=False)
            _check_business_auth(d)
            if not isinstance(d, dict) or d.get('code') != 200 or not isinstance(d.get('rows'), list):
                raise PlatformApiError('BCS 店铺列表响应不完整')
            rows = d['rows']
            for row in rows:
                raw_id = row.get('id') if isinstance(row, dict) else None
                text_id = str(raw_id)
                if not text_id.isascii() or not text_id.isdecimal() or int(text_id) <= 0:
                    raise PlatformApiError('BCS 店铺列表含无效 ID')
                shop_id = int(text_id)
                if shop_id in seen:
                    raise PlatformApiError('BCS 店铺列表分页重复，无法确认完整性')
                seen.add(shop_id)
                shops.append(project(shop_id, row))
            total = d.get('total')
            if total is not None:
                text_total = str(total)
                if not text_total.isascii() or not text_total.isdecimal() or int(text_total) < len(shops):
                    raise PlatformApiError('BCS 店铺列表 total 无效或不一致')
                if len(shops) == int(total):
                    return shops
                if not rows:
                    raise PlatformApiError('BCS 店铺列表提前结束')
            elif not rows:
                return shops
        raise PlatformApiError('BCS 店铺列表超过分页上限')

    def fetch_shop_products(self, shop_id: int, filter_type: str = "BASE",
                            *, budget: RequestBudget | None = None) -> List[Dict[str, Any]]:
        url = (f"{self.base_url}/shopKeeper/productList/list?filter={filter_type}"
               f"&pageNum=1&pageSize={PAGE_SIZE_BIG}&shopId={shop_id}")
        d = self.get(url, **({'budget': budget} if budget else {}))
        _check_business_auth(d)
        if not isinstance(d, dict) or d.get('code') != 200:
            raise PlatformApiError(f'店铺{shop_id} BCS 商品查询失败')
        rows = d.get('rows')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PlatformApiError(f'店铺{shop_id} BCS 商品响应缺少有效 rows，不能视为空店')
        total = d.get('total')
        if total is not None:
            if (isinstance(total, bool) or not str(total).isdecimal()
                    or int(total) != len(rows)):
                raise PlatformApiError(f'店铺{shop_id} BCS 商品总数与返回行数不一致，快照不完整')
        elif len(rows) >= PAGE_SIZE_BIG:
            raise PlatformApiError(f'店铺{shop_id} BCS 商品达到单页上限，无法确认完整性')
        if any(row.get('shopId') is not None and str(row['shopId']) != str(shop_id) for row in rows):
            raise PlatformApiError(f'店铺{shop_id} BCS 商品响应包含其他店铺数据')
        try:
            unique = deduplicate_bcs_products(rows, shop_id)
        except ValueError as error:
            raise PlatformApiError(str(error)) from None
        if len(unique) != len(rows):
            print(f'[店铺{shop_id}] BCS商品去重：原始{len(rows)}行，'
                  f'保留{len(unique)}个商品，移除{len(rows) - len(unique)}条重复记录')
        return unique

    def batch_set_prices(self, shop_id: int, items: list[dict]) -> dict:
        rows = [{**item, "shopId": shop_id} for item in items]
        return self.post(f"{self.base_url}/shopKeeper/price/batch", {"dataList": rows})

    def batch_set_stock(self, shop_id: int, warehouse_id: int, items: list[dict]) -> dict:
        warehouse = {"warehouseId": warehouse_id, "stockItems": items}
        return self.post(f"{self.base_url}/shopKeeper/stock/batchSetByChrtIdsBatch",
                         {"shopId": shop_id, "warehouses": [warehouse]})

    def remove_to_trash(self, shop_id: int, nm_ids: List[int]) -> Dict[str, Any]:
        return self.post(f"{self.base_url}/shopKeeper/clean/removeToTrash",
                         [{"shopId": shop_id, "nmId": nm} for nm in nm_ids])

    def batch_push_products(self, shop_configs: List[Dict[str, Any]], sku_prices: List[Dict[str, Any]], mode: int = 1, carry_brand: int = 1) -> Dict[str, Any]:
        url = f"{self.base_url}/products/batch/push"
        body = {
            "shop": shop_configs,
            "mode": mode,
            "carryBrand": carry_brand,
            "customBrand": None,
            "vendorCodePrefix": None,
            "titleSuffix": None,
            "aiRewrite": False,
            "aiRetouch": False,
            "aiRetouchTemplateId": None,
            "imgUploadMode": 0,
            "skuPrices": sku_prices,
            "wbCollection": {},
        }
        return self.post(url, body)

    def sync_shop(self, shop_id: int, filter_type: str = "ALL", *, budget: RequestBudget | None = None) -> str:
        global _last_sync_task_ms
        with _SYNC_TASK_LOCK:
            _last_sync_task_ms = max(time.time_ns() // 1_000_000, _last_sync_task_ms + 1)
            task_id = f'TASK_{_last_sync_task_ms}'
        url = f"{self.base_url}/shopKeeper/product/sync?taskId={task_id}&shopId={shop_id}&filter={filter_type}"
        # This GET submits a synchronization task and must not be replayed as a read.
        d = self.request_json('GET', url, mutating=True, allow_redirects=False, budget=budget)
        _check_business_auth(d)
        code = d.get('code') if isinstance(d, dict) else None
        if code != 200:
            raise PlatformApiError('BCS 未确认同步已受理', status_code=code if type(code) is int else None)
        return task_id

    def wait_sync_done(self, task_id: str, shop_id: Optional[int] = None, timeout: int = 600,
                       interval: float = 2.5, quiet: bool = False, *, budget: RequestBudget | None = None) -> None:
        start = time.monotonic()
        shop_deadline = start + min(timeout, 600)
        if budget is None or shop_deadline < budget.deadline:
            budget = RequestBudget(shop_deadline, budget.stop if budget else None, 'shop_timeout')
        heartbeat = start
        while True:
            budget.remaining()
            d = self.get(f"{self.base_url}/shopKeeper/product/sync/progress/{task_id}", budget=budget)
            _check_business_auth(d)
            code = d.get('code') if isinstance(d, dict) else None
            data = d.get('data') if isinstance(d, dict) else None
            if code not in (None, 200) or not isinstance(data, dict):
                raise PlatformApiError('BCS 同步进度响应异常', status_code=code if type(code) is int else None)
            if data.get("status") == 1:
                return
            now = time.monotonic()
            if not quiet and now >= heartbeat:
                pct = data.get('completedCount')
                progress = f'{pct}%' if type(pct) in (int, float) else '等待平台确认'
                print(f'  [同步 店{shop_id}] {progress}', flush=True)
                heartbeat = now + SYNC_HEARTBEAT_SECONDS
            budget.pause(interval)

    def synchronize_shop(self, shop_id: int, *, quiet: bool = False,
                         budget: RequestBudget | None = None) -> dict | None:
        stage = 'not_started'
        try:
            if budget:
                budget.remaining()
            stage = 'submit'
            options = {'budget': budget} if budget else {}
            task_id = self.sync_shop(shop_id, **options)
            stage = 'poll'
            self.wait_sync_done(task_id, shop_id=shop_id, quiet=quiet, **options)
        except Exception as error:
            warning = {'id': shop_id, 'stage': stage, 'reason': sync_failure_reason(error)}
            if isinstance(error, AuthenticationError):
                if budget and budget.stop is not None:
                    budget.stop.set()
                warning['kind'] = 'authentication'
            elif isinstance(error, SyncInterrupted):
                warning['kind'] = error.kind
            if not quiet:
                _log_sync_warning(warning)
            return warning
        return None

    def sync_shops_parallel(self, shop_ids: list[int], workers: int = 2,
                            *, deadline: float | None = None) -> list[dict]:
        """Dispatch only available slots and return warnings in input order."""
        budget = RequestBudget(deadline if deadline is not None else time.monotonic() + SYNC_STAGE_SECONDS,
                               Event())
        failures = {}
        workers = max(1, min(workers, 4))
        next_shop = completed = 0
        heartbeat = time.monotonic() + SYNC_HEARTBEAT_SECONDS
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = set()
            while pending or next_shop < len(shop_ids):
                while len(pending) < workers and next_shop < len(shop_ids):
                    try:
                        budget.remaining()
                    except SyncInterrupted as error:
                        for sid in shop_ids[next_shop:]:
                            failures[sid] = {'id': sid, 'stage': 'not_started', 'kind': error.kind,
                                             'reason': str(error)}
                        next_shop = len(shop_ids)
                        break
                    pending.add(pool.submit(self.synchronize_shop, shop_ids[next_shop], quiet=True, budget=budget))
                    next_shop += 1
                if not pending:
                    break
                done, pending = wait(pending, timeout=max(0, heartbeat - time.monotonic()),
                                     return_when=FIRST_COMPLETED)
                completed += len(done)
                for future in done:
                    if warning := future.result():
                        failures[warning['id']] = warning
                if time.monotonic() >= heartbeat:
                    print(f'[同步进度] 已结束 {completed}/{len(shop_ids)} 家，{len(pending)} 家处理中', flush=True)
                    heartbeat = time.monotonic() + SYNC_HEARTBEAT_SECONDS
        warnings = [failures[sid] for sid in shop_ids if sid in failures]
        for warning in warnings:
            _log_sync_warning(warning)
        return warnings


client = BCSClient()
