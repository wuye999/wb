"""
sellerops 店铺商品拉取与快照管理（原 fetch_products.py + sync_shops.fetch_all）

默认使用 BCS 快照接口：先同步 WB → BCS，再读取 BASE 缓存，兼容原本地快照契约。
显式 source=wb-openapi 时改为用 BCS 保存的 apiKey 直接读取 WB Open API。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sellerops.integrations.bcs import client as bcs
from sellerops.settings import paths as config
from sellerops.core.files import atomic_dump_json
from sellerops.core.files import safe_load_json
from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.integrations.wildberries.open_api import OpenAPIResponseError
from sellerops.features.catalog import snapshot_open_api
from sellerops.domain.snapshot_values import first_price, stock_values, valid_sizes

_OPEN_API_METHOD = 'wb-openapi(content.cards, prices.nmList, moscow.chrtIds)'

def is_empty_product(r):
    """空商品三缺判定：价格+库存+名称全空（productType=ERROR 残留），不计入待审核。
    （原逻辑从「空商品处理/cleanup_empty_products.py」内联，去掉跨目录依赖）"""
    sizes = valid_sizes(r) or []
    has_price = any(first_price({'sizeList': [size]}) is not None for size in sizes)
    return not has_price and stock_values(r) is None and not (r.get('title') or '').strip()


def shop_ids_from_disk():
    """扫描 data/products/ 下 shop*_products_all.json → [店id]（离线，与 fetch 状态一致）

    [薄转发] 唯一实现已下沉到 storage.product_repo.ProductSnapshotRepository。
    """
    from sellerops.persistence.snapshots import ProductSnapshotRepository
    return ProductSnapshotRepository.shop_ids_from_disk()


def fetch_shop(shop_id: int, out_file: str, no_sync: bool = False,
               source: str = 'bcs', *, access: bcs.OpenAPIShop | None = None) -> tuple[list[dict], int]:
    """完整拉取并原子保存单店快照；默认走 BCS 快照，Open API 失败不回退 BCS。"""
    if type(shop_id) is not int or shop_id <= 0:
        raise ValueError('店铺 ID 必须为正整数')
    if source not in {'wb-openapi', 'bcs'}:
        raise ValueError('未知快照数据来源')
    metadata = {}
    if source == 'wb-openapi':
        if no_sync:
            print('[提示] Open API 直接读取 WB，无需 BCS 同步；--no-sync 无需生效')
        if access is None:
            matches = [shop for shop in bcs.client.fetch_open_api_shops() if shop.id == shop_id]
            if len(matches) != 1:
                raise RuntimeError('BCS 未返回唯一目标店铺，无法读取该店 apiKey')
            access = matches[0]
        if access.id != shop_id:
            raise ValueError('店铺密钥归属与目标快照不一致')
        print(f'[拉取] 店铺 {shop_id}：WB Open API 商品、价格与莫斯科仓库存 ...')
        try:
            rows, metadata = snapshot_open_api.collect_shop(access)
        except Exception as error:
            raise OpenAPIResponseError(_safe_error(error)) from None
        method = _OPEN_API_METHOD
    else:
        rows = _fetch_bcs(shop_id, no_sync)
        method = 'bcs.fetch_shop_products(filter=BASE, pageSize=10000)'
        metadata['warnings'] = {
            'missingSpecProducts': sum(valid_sizes(row) is None for row in rows),
            'missingStockProducts': sum(stock_values(row) is None for row in rows),
            'missingPriceProducts': sum(first_price(row) is None for row in rows),
        }
    return _save_snapshot(shop_id, out_file, rows, source, method, metadata)


def _fetch_bcs(shop_id: int, no_sync: bool) -> list[dict]:
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
    print(f"[2/2] 获取 {len(rows)} 条")
    return rows


def _save_snapshot(shop_id: int, out_file: str, rows: list[dict], source: str,
                   method: str, metadata: dict) -> tuple[list[dict], int]:
    total = len(rows)
    result = {
        "shopId": shop_id,
        "total": total,
        "fetchedCount": len(rows),
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fetchMethod": method,
        "source": source,
        "code": 200,
        "msg": "查询成功",
        "rows": rows,
        **metadata,
    }
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    atomic_dump_json(out_file, result, indent=2, use_lock=True)
    print(f"[店铺{shop_id}] 已保存：{out_file}（{os.path.getsize(out_file) / 1024 / 1024:.2f} MB）")
    vcs = len({r.get("vendorCode") for r in rows})
    n_alive = sum(1 for r in rows if not r.get("trashedAt"))
    print(f"[店铺{shop_id}] 统计：商品条数={len(rows)}，唯一 vendorCode={vcs}，在架(trashedAt空)={n_alive}")
    labels = {'missingPriceSpecs': '缺少价格的规格', 'missingStockSpecs': '未返回库存记录的规格',
              'missingImages': '缺少图片的商品', 'missingSpecProducts': '规格不完整的商品',
              'missingStockProducts': '库存无法确认的商品', 'missingPriceProducts': '价格无法确认的商品'}
    for key, count in metadata.get('warnings', {}).items():
        if count:
            print(f'[店铺{shop_id} 告警] {labels.get(key, key)}={count}（缺失保留为空，不补零）')
    return rows, total


def _safe_error(error: Exception) -> str:
    if isinstance(error, OpenAPIResponseError):
        return error.public_reason
    if isinstance(error, AuthenticationError):
        return 'WB Open API 令牌缺失、失效或类别权限不足'
    if isinstance(error, RateLimitError):
        return 'WB Open API 读取受到限流，请稍后重试'
    if isinstance(error, NetworkError):
        return 'WB Open API 网络读取失败'
    return f'快照采集失败（{type(error).__name__}）'


def _fetch_open_api_shops(accesses: list[bcs.OpenAPIShop], parallel: int) -> tuple[list[dict], list[dict]]:
    """有界并发采集；主线程逐店保存快照并汇总，不共享会话或令牌。"""
    fetched, errors = [], []
    if not accesses:
        return fetched, errors
    workers = min(parallel, len(accesses))
    print(f'\n>>> WB Open API 并发拉取：同时最多 {workers} 个店铺（店内按原限流顺序读取）')
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='snapshot') as pool:
        pending = {pool.submit(snapshot_open_api.collect_shop, shop): shop for shop in accesses}
        for future in as_completed(pending):
            shop = pending.pop(future)
            try:
                rows, metadata = future.result()
                _, total = _save_snapshot(shop.id, config.shop_json_path(shop.id), rows,
                                          'wb-openapi', _OPEN_API_METHOD, metadata)
                fetched.append({'id': shop.id, 'total': total})
            except Exception as error:
                reason = _safe_error(error)
                print(f'  [店铺{shop.id} ({shop.name}) 失败] {reason}')
                errors.append({'id': shop.id, 'name': shop.name, 'error': reason})
            print(f'[进度] 已完成 {len(fetched) + len(errors)}/{len(accesses)} 店，'
                  f'成功 {len(fetched)}，失败 {len(errors)}')
    # 完成顺序由网络决定，持久化状态仍按 BCS 店铺顺序，便于比较和消费。
    order = {shop.id: i for i, shop in enumerate(accesses)}
    fetched.sort(key=lambda row: order[row['id']])
    errors.sort(key=lambda row: order[row['id']])
    return fetched, errors


def fetch_all(no_sync: bool = False, source: str = 'bcs', *, parallel: int = 4) -> None:
    """BCS 默认先同步再读 BASE 缓存；显式 wb-openapi 时最多4店并发；失败保留旧快照，汇总后报错。"""
    if source not in {'wb-openapi', 'bcs'}:
        raise ValueError('未知快照数据来源')
    if type(parallel) is not int or parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    accesses = bcs.client.fetch_open_api_shops() if source == 'wb-openapi' else []
    shops = ([{'id': shop.id, 'name': shop.name} for shop in accesses]
             if source == 'wb-openapi' else bcs.fetch_shop_list())
    print(f"共 {len(shops)} 个店铺: {[(s['id'], s['name']) for s in shops]}")
    status = {"fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "shops": shops, "errors": [], "source": source, "fetched": []}

    if source == 'wb-openapi' and no_sync:
        print('[提示] Open API 直接读取 WB，无需 BCS 同步；--no-sync 无需生效')
    if source == 'bcs' and not no_sync:
        t0 = time.time()
        print("\n>>> 并发同步全部店铺（WB → BCS）...")
        bcs.sync_shops_parallel([s["id"] for s in shops])
        print(f"    全部同步完成（{time.time() - t0:.0f}s）")

    if source == 'wb-openapi':
        status['fetched'], status['errors'] = _fetch_open_api_shops(accesses, parallel)
    else:
        for i, s in enumerate(shops):
            out = config.shop_json_path(s["id"])
            print(f"\n>>> 拉取店铺 {s['id']} ({s['name']})")
            try:
                _, total = fetch_shop(s['id'], out, no_sync=True, source=source)
                status['fetched'].append({'id': s['id'], 'total': total})
            except Exception as error:
                reason = str(error)
                print(f"  [店铺{s['id']} 失败] {reason}")
                status['errors'].append({'id': s['id'], 'name': s['name'], 'error': reason})
            if i < len(shops) - 1:
                time.sleep(1)
    os.makedirs(config.STATE_DIR, exist_ok=True)
    atomic_dump_json(config.STATUS_JSON, status, indent=2, use_lock=True)
    print(f"\n状态已保存：{config.STATUS_JSON}")
    if status["errors"]:
        raise RuntimeError(f"{len(status['errors'])} 个店铺拉取失败，失败店铺保留原快照；"
                           f"请处理后重新拉取，再执行 merge。详情：{config.STATUS_JSON}")


def load_all_shops():
    """读全部店铺 JSON（存在即加载），返回 {shop_id: rows} 与店铺元信息。
    无任何店铺数据时抛 RuntimeError 提示先 fetch。"""
    shops_data = {}
    shops_meta = []
    meta = safe_load_json(config.STATUS_JSON, default={})
    shops_meta = meta.get("shops", []) or []

    if not shops_meta:  # 状态文件缺失 → 扫描磁盘推断
        for sid in shop_ids_from_disk():
            shops_meta.append({"id": sid, "name": f"shop{sid}"})
    for s in shops_meta:
        p = config.shop_json_path(s["id"])
        if os.path.exists(p):
            d = safe_load_json(p, default={})
            shops_data[s["id"]] = [r for r in d.get("rows", []) if not r.get("trashedAt")]
    if not shops_data:
        raise RuntimeError("未找到店铺 JSON，请先运行 fetch（sellerops fetch）")
    return shops_data, shops_meta
