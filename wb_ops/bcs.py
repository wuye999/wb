# -*- coding: utf-8 -*-
"""
wb_ops BCS 云端 API 客户端（原「检查价格/api.py」）

封装 wb.bcserp.com 的全部 HTTP 请求：gzip 自动解压、429 指数退避、401 报错、
店铺列表 / 商品拉取 / 同步并发 / 仓库 / 下架。凭证统一读 credentials.py。
"""
import time

import requests

from . import config
from . import credentials
RETRY_MAX = 3
PAGE_SIZE_BIG = 10000


def _headers():
    return credentials.get().bcs_headers()


def _request_json(method, url, retry, **kwargs):
    """统一请求：429 指数退避重试；401 明确报错；60s 超时；返回 JSON dict。"""
    headers = _headers()
    if method == "POST":
        headers["Content-Type"] = "application/json;charset=UTF-8"
    last = None
    for attempt in range(retry):
        try:
            resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            if resp.status_code == 429 and attempt < retry - 1:
                wait = 3 * (attempt + 1)
                print(f"    [429 限流] 等待 {wait}s 重试...")
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise RuntimeError("401 认证失败：BCS token 可能已过期，请更新 credentials.json 的 bcs.token")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            last = e
            if attempt < retry - 1:
                time.sleep(2)
                continue
            code = e.response.status_code if e.response is not None else "?"
            raise RuntimeError(f"HTTP {code}: {e}")
        except Exception as e:
            last = e
            if attempt < retry - 1:
                time.sleep(2)
                continue
    raise last if last else RuntimeError("网络错误")


def http_get_json(url, retry=RETRY_MAX):
    """GET 并解析 JSON；429 指数退避重试；401 明确报错。"""
    return _request_json("GET", url, retry)


def http_post_json(url, data, retry=RETRY_MAX):
    """POST JSON body；同上重试策略。"""
    return _request_json("POST", url, retry, json=data)


def base_url():
    """BCS API 根地址（读统一凭证）"""
    return credentials.get().base_url


# ---------------- 店铺 ----------------
def fetch_shop_list():
    """账号全部店铺 → [{'id', 'name'}]"""
    url = f"{base_url()}/system/wbShop/user/list?pageNum=1&pageSize=100"
    d = http_get_json(url)
    if d.get("code") != 200:
        raise RuntimeError(f"获取店铺列表失败 code={d.get('code')} msg={d.get('msg')}")
    return [{"id": r.get("id"), "name": r.get("shopName")}
            for r in (d.get("rows") or []) if r.get("id")]


def get_main_shop():
    """主店 ID：config.MAIN_SHOP 显式指定则用它；否则取店铺列表第一个"""
    if config.MAIN_SHOP:
        return config.MAIN_SHOP
    shops = fetch_shop_list()
    if not shops:
        raise RuntimeError("未获取到店铺列表")
    return shops[0]["id"]


def fetch_shop_products(shop_id, filter_type="BASE"):
    """拉取店铺商品（大 pageSize 一次拉全）→ rows 列表
    filter_type: ALL=全部 / BASE=在架(上架) / ERROR=草稿箱 / TRASH=回收站（日常用 BASE=在架）"""
    url = (f"{base_url()}/shopKeeper/productList/list?filter={filter_type}"
           f"&pageNum=1&pageSize={PAGE_SIZE_BIG}&shopId={shop_id}")
    d = http_get_json(url)
    if d.get("code") != 200:
        raise RuntimeError(f"店铺{shop_id}拉取失败 code={d.get('code')} msg={d.get('msg')}")
    return d.get("rows") or []


def count_by_filter(shop_id):
    """各状态商品计数 → {'ALL':n,'BASE':n,'ERROR':n,'TRASH':n}"""
    d = http_get_json(f"{base_url()}/shopKeeper/productList/countByFilter?shopId={shop_id}")
    if d.get("code") != 200:
        raise RuntimeError(f"countByFilter 失败 code={d.get('code')}")
    return d.get("data") or {}


def fetch_warehouses(shop_id):
    """店铺仓库列表 → [{'id','name','officeId','cargoType','deliveryType'}]（直接返回数组，无 code 包装）"""
    d = http_get_json(f"{base_url()}/system/wbWarehouses/list?shopId={shop_id}")
    if isinstance(d, list):
        return d
    return d.get("data") or d.get("rows") or []


def remove_to_trash(shop_id, nm_ids):
    """移至回收站（不可逆）；nm_ids 为 BCS 内部 nmId 列表"""
    return http_post_json(f"{base_url()}/shopKeeper/clean/removeToTrash",
                          {"shopId": shop_id, "nmIds": nm_ids})


# ---------------- BCS 数据同步（拉取 WB 数据） ----------------
def sync_shop(shop_id, filter_type="ALL"):
    """触发 BCS「拉取店铺商品数据」同步（BCS 从 WB 官方拉取，异步任务）。
    taskId 格式 TASK_{毫秒时间戳}（与前端 Date.now() 一致）。返回 taskId。"""
    task_id = f"TASK_{int(time.time() * 1000)}"
    url = (f"{base_url()}/shopKeeper/product/sync"
           f"?taskId={task_id}&shopId={shop_id}&filter={filter_type}")
    d = http_get_json(url)
    if d.get("code") != 200:
        raise RuntimeError(f"触发同步失败 shop={shop_id} code={d.get('code')} msg={d.get('msg')}")
    return task_id


def wait_sync_done(task_id, shop_id=None, timeout=240, interval=2.5, quiet=False):
    """轮询同步进度直到完成（data.status==1）。超时抛错。"""
    start = time.time()
    while time.time() - start < timeout:
        d = http_get_json(f"{base_url()}/shopKeeper/product/sync/progress/{task_id}")
        data = d.get("data") or {}
        pct = data.get("completedCount")
        if not quiet and pct is not None:
            print(f"  [同步 店{shop_id}] {pct}%", end="\r" if pct < 100 else "\n", flush=True)
        if data.get("status") == 1:
            return
        time.sleep(interval)
    raise TimeoutError(f"同步超时 task={task_id}")


def sync_shops_parallel(shop_ids, timeout=240, interval=2.5, max_workers=5):
    """并发同步多个店铺（同时触发 + 并发轮询进度），全部完成后返回。
    返回 {shop_id: task_id}；单店同步失败打印警告降级（不中断其他店）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _sync_one(sid):
        t0 = time.time()
        task = sync_shop(sid)
        wait_sync_done(task, shop_id=sid, timeout=timeout, interval=interval, quiet=True)
        print(f"  ✓ 店{sid} 同步完成（{time.time() - t0:.0f}s）")
        return sid, task

    tasks = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_sync_one, sid): sid for sid in shop_ids}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                _, task = fut.result()
                tasks[sid] = task
            except Exception as e:
                print(f"  [警告] 店{sid} 同步失败：{e}（降级：直接拉取上次同步数据）")
    return tasks
