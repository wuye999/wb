# -*- coding: utf-8 -*-
"""
wb_ops 订单查询（原「查询订单情况wb.bcserp.com」抓包）

BCS 订单管理：同步全部订单（异步任务）→ 轮询进度 → 查询订单列表 + 状态分类计数。
鉴权：BCS（bcs.py，Bearer + X-Limit-Key），非 WB 会话。
注意：createPullProductTask 响应里的 msg 字段实为 taskId（非提示文案）。
"""
import csv
import os
import time
from datetime import datetime, timedelta

from . import bcs
from . import config
from . import common

OZON = "/system/ozonOrder"


def create_task(shop_ids, begin, end):
    """POST 创建订单拉取任务。shop_ids=[] 表示全部店铺。返回 taskId（从 msg 提取）。"""
    d = bcs.http_post_json(f"{bcs.base_url()}{OZON}/createPullProductTask",
                           {"shopIds": shop_ids, "beginTime": begin, "endTime": end})
    if d.get("code") != 200:
        raise RuntimeError(f"创建订单同步任务失败 code={d.get('code')} msg={d.get('msg')}")
    return d.get("msg")  # ⚠ msg 实为 taskId


def wait_progress(task_id, interval=2, timeout=600):
    """轮询订单同步进度直到 COMPLETED。"""
    start = time.time()
    while time.time() - start < timeout:
        d = bcs.http_get_json(f"{bcs.base_url()}{OZON}/getPullProductTaskProgress")
        data = d.get("data") or []
        task = next((t for t in data if t.get("taskId") == task_id), None)
        if task is None:
            time.sleep(interval)
            continue
        status = task.get("status")
        total = task.get("total", 0)
        completed = task.get("completed", 0)
        print(f"  [订单同步] {completed}/{total} {status}",
              end="\r" if status == "RUNNING" else "\n", flush=True)
        if status == "COMPLETED":
            return task
        time.sleep(interval)
    raise TimeoutError(f"订单同步超时 task={task_id}")


def fetch_orders(page_num=1, page_size=50):
    """GET 订单列表（分页）。返回 (total, rows)。"""
    d = bcs.http_get_json(f"{bcs.base_url()}{OZON}/user/list"
                          f"?pageNum={page_num}&pageSize={page_size}&type=0")
    if d.get("code") != 200:
        raise RuntimeError(f"查询订单失败 code={d.get('code')} msg={d.get('msg')}")
    return d.get("total", 0), d.get("rows") or []


def fetch_all_orders(page_size=50):
    """分页拉全部订单。返回 (total, all_rows)。"""
    total, rows = fetch_orders(1, page_size)
    all_rows = list(rows)
    pages = (total + page_size - 1) // page_size
    for pn in range(2, pages + 1):
        _, rows = fetch_orders(pn, page_size)
        all_rows.extend(rows)
        time.sleep(0.3)
    return total, all_rows


def fetch_status_count():
    """GET 订单状态分类计数。"""
    d = bcs.http_get_json(f"{bcs.base_url()}{OZON}/getOrderStatusCategoryCount")
    return d.get("data") or []


def _parse_date(args):
    """解析日期区间。返回 (begin, end)。"""
    if args.begin and args.end:
        return args.begin, args.end
    end = datetime.now().strftime("%Y-%m-%d")
    days = args.days if args.days else 1
    begin = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return begin, end


def run(args):
    common.ensure_utf8_stdout()
    begin, end = _parse_date(args)
    print(f"日期区间: {begin} ~ {end}")

    shop_ids = []
    if args.shops:
        shop_ids = [int(x) for x in args.shops.split(",") if x.strip()]
        if not shop_ids:
            print("[错误] --shops 参数无效")
            return 1

    if not args.no_sync:
        print("\n[同步] 创建订单拉取任务...")
        task_id = create_task(shop_ids, begin, end)
        print(f"  taskId = {task_id}")
        wait_progress(task_id)
    else:
        print("\n[跳过同步] 直接查询已缓存的订单")

    print("\n[查询] 拉取订单列表...")
    total, rows = fetch_all_orders(args.page_size)
    status_count = fetch_status_count()
    print(f"  订单总数 {total}，本次拉取 {len(rows)} 条")

    sc_map = {str(c.get("orderStatusCategory")): c.get("orderCount") for c in status_count}
    if sc_map:
        print(f"  状态分类计数: {sc_map}")

    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"订单查询_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["订单号", "店铺", "规格ID", "nmID", "供应商编码",
                                              "供应商状态", "WB状态", "折算金额", "币种",
                                              "仓库", "创建时间", "状态分类"])
            w.writeheader()
            for r in rows:
                w.writerow({
                    "订单号": r.get("orderId"),
                    "店铺": r.get("shopName"),
                    "规格ID": r.get("chrtId"),
                    "nmID": r.get("nmId"),
                    "供应商编码": r.get("article"),
                    "供应商状态": r.get("supplierStatus"),
                    "WB状态": r.get("wbStatus"),
                    "折算金额": r.get("convertedPrice"),
                    "币种": r.get("convertedCurrencyCode"),
                    "仓库": r.get("warehouseName"),
                    "创建时间": r.get("createdAt"),
                    "状态分类": r.get("orderStatusCategory"),
                })
        print(f"\n[日志] {path}")
    else:
        print("\n[完成] 该日期区间无订单")

    print(f"\n[汇总] 订单 {total} 条")
    return 0
