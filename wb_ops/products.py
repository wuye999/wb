# -*- coding: utf-8 -*-
"""
wb_ops 店铺商品拉取与快照管理（原 fetch_products.py + sync_shops.fetch_all）

拉取 BCS 店铺在架商品（filter=BASE）→ 保存为 data/products/shop{id}_products_all.json。
默认先触发 BCS 同步（WB→BCS，~40-50s/店）再拉列表，避免缓存滞后漏数据。
"""
import json
import os
import time
from datetime import datetime

from . import bcs
from . import config
def is_empty_product(r):
    """空商品三缺判定：价格+库存+名称全空（productType=ERROR 残留），不计入待审核。
    （原逻辑从「空商品处理/cleanup_empty_products.py」内联，去掉跨目录依赖）"""
    sl = r.get("sizeList") or []
    miss = 0
    if not any(s.get("price") is not None for s in sl):
        miss += 1
    if not any(s.get("stockList") for s in sl):
        miss += 1
    if not (r.get("title") or "").strip():
        miss += 1
    return miss == 3


def shop_ids_from_disk():
    """扫描 data/products/ 下 shop*_products_all.json → [店id]（离线，与 fetch 状态一致）"""
    import glob
    ids = []
    for p in sorted(glob.glob(config.shop_json_path("*"))):
        try:
            sid = int(os.path.basename(p).replace("shop", "").replace("_products_all.json", ""))
            ids.append(sid)
        except ValueError:
            continue
    return ids


def fetch_shop(shop_id, out_file, no_sync=False):
    """拉取单个店铺在架商品，保存到 out_file；返回 (rows, total)。
    默认先触发 BCS 同步（每店约 40s）再拉列表；--no-sync 跳过同步。
    同步失败自动降级（警告后直接拉上次同步数据）。"""
    if not no_sync:
        try:
            print(f"[0/3] 店 {shop_id}：同步 WB 数据 ...")
            t0 = time.time()
            task_id = bcs.sync_shop(shop_id)
            bcs.wait_sync_done(task_id, shop_id=shop_id)
            print(f"      同步完成（{time.time() - t0:.0f}s）")
        except Exception as e:
            print(f"  [警告] 同步失败：{e}（降级：直接拉取上次同步数据）")
    print("=" * 60)
    print(f"[1/2] 店铺 {shop_id}：拉取在架商品（BASE） ...")
    rows = bcs.fetch_shop_products(shop_id, filter_type="BASE")
    total = len(rows)
    print(f"[2/2] 获取 {len(rows)} 条")

    result = {
        "shopId": shop_id,
        "total": total,
        "fetchedCount": len(rows),
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fetchMethod": "bcs.fetch_shop_products(filter=BASE, pageSize=10000)",
        "code": 200,
        "msg": "查询成功",
        "rows": rows,
    }
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存：{out_file}（{os.path.getsize(out_file) / 1024 / 1024:.2f} MB）")
    vcs = len({r.get("vendorCode") for r in rows})
    n_alive = sum(1 for r in rows if not r.get("trashedAt"))
    print(f"统计：商品条数={len(rows)}，唯一 vendorCode={vcs}，在架(trashedAt空)={n_alive}")
    return rows, total


def fetch_all(no_sync=False):
    """拉取全部店铺在架商品：先并发同步全部店铺（总耗时≈单店~50s），再逐店拉取。"""
    shops = bcs.fetch_shop_list()
    print(f"共 {len(shops)} 个店铺: {[(s['id'], s['name']) for s in shops]}")
    status = {"fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "shops": shops, "errors": []}

    if not no_sync:
        t0 = time.time()
        print("\n>>> 并发同步全部店铺（WB → BCS）...")
        bcs.sync_shops_parallel([s["id"] for s in shops])
        print(f"    全部同步完成（{time.time() - t0:.0f}s）")

    for i, s in enumerate(shops):
        out = config.shop_json_path(s["id"])
        print(f"\n>>> 拉取店铺 {s['id']} ({s['name']})")
        try:
            fetch_shop(s["id"], out, no_sync=True)  # 同步已在上面完成
        except Exception as e:
            print(f"  [店铺{s['id']} 失败] {e}")
            status["errors"].append({"id": s["id"], "name": s["name"], "error": str(e)})
        if i < len(shops) - 1:
            time.sleep(1)
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(config.STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(f"\n状态已保存：{config.STATUS_JSON}")


def load_all_shops():
    """读全部店铺 JSON（存在即加载），返回 {shop_id: rows} 与店铺元信息。
    无任何店铺数据时抛 RuntimeError 提示先 fetch。"""
    shops_data = {}
    shops_meta = []
    try:
        meta = json.load(open(config.STATUS_JSON, encoding="utf-8"))
        shops_meta = meta.get("shops", []) or []
    except Exception:
        pass
    if not shops_meta:  # 状态文件缺失 → 扫描磁盘推断
        for sid in shop_ids_from_disk():
            shops_meta.append({"id": sid, "name": f"shop{sid}"})
    for s in shops_meta:
        p = config.shop_json_path(s["id"])
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            shops_data[s["id"]] = [r for r in d.get("rows", []) if not r.get("trashedAt")]
    if not shops_data:
        raise RuntimeError("未找到店铺 JSON，请先运行 fetch（wb.py fetch）")
    return shops_data, shops_meta
