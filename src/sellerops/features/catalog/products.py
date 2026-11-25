"""
sellerops 店铺商品拉取与快照管理（原 fetch_products.py + sync_shops.fetch_all）

默认使用 BCS 快照接口：先同步 WB → BCS，再读取 BASE 缓存，兼容原本地快照契约。
显式 source=wb-openapi 时改为用 BCS 保存的 apiKey 直接读取 WB Open API。
"""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sellerops.integrations.bcs.client import OpenAPIShop, client as bcs, sync_failure_reason
from sellerops.persistence import db
from sellerops.persistence.snapshots import ProductSnapshotRepository, save_shop, save_snapshot
from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.integrations.wildberries.open_api import OpenAPIResponseError
from sellerops.features.catalog import snapshot_open_api
from sellerops.features.catalog.shops import require_active, shop_status
from sellerops.domain.snapshot_values import first_price, stock_values, valid_sizes

_OPEN_API_METHOD = 'wb-openapi(content.cards, prices.nmList, moscow.chrtIds)'

def is_empty_product(r):
    """空商品三缺判定：价格+库存+名称全空（productType=ERROR 残留），不计入待审核。
    （原逻辑从「空商品处理/cleanup_empty_products.py」内联，去掉跨目录依赖）"""
    sizes = valid_sizes(r) or []
    has_price = any(first_price({'sizeList': [size]}) is not None for size in sizes)
    return not has_price and stock_values(r) is None and not (r.get('title') or '').strip()


def fetch_shop(shop_id: int, no_sync: bool = False,
               source: str = 'bcs', *, access: OpenAPIShop | None = None, name: str = '') -> tuple[list[dict], int]:
    """完整拉取并原子保存单店快照；默认走 BCS 快照，Open API 失败不回退 BCS。"""
    if type(shop_id) is not int or shop_id <= 0:
        raise ValueError('店铺 ID 必须为正整数')
    require_active(shop_id)
    if source not in {'wb-openapi', 'bcs'}:
        raise ValueError('未知快照数据来源')
    metadata = {}
    if source == 'wb-openapi':
        if no_sync:
            print('[提示] Open API 直接读取 WB，无需 BCS 同步；--no-sync 无需生效')
        if access is None:
            matches = [shop for shop in bcs.fetch_open_api_shops() if shop.id == shop_id]
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
    return _save_snapshot(shop_id, rows, source, method, metadata, access.name if access else name)


def _fetch_bcs(shop_id: int, no_sync: bool) -> list[dict]:
    if not no_sync:
        print(f"[0/3] 店 {shop_id}：同步 WB 数据 ...")
        t0 = time.monotonic()
        if bcs.synchronize_shop(shop_id):
            print(f'[降级] 店铺 {shop_id} 同步未确认，读取该店 BCS BASE 缓存。')
        else:
            print(f"      同步完成（{time.monotonic() - t0:.0f}s）")
    print("=" * 60)
    print(f"[1/2] 店铺 {shop_id}：拉取在架商品（BASE） ...")
    rows = bcs.fetch_shop_products(shop_id, filter_type="BASE")
    print(f"[2/2] 获取 {len(rows)} 条")
    return rows


def _save_snapshot(shop_id: int, rows: list[dict], source: str,
                   method: str, metadata: dict, name: str = '') -> tuple[list[dict], int]:
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
    with db.connect(write=True) as connection:
        row = connection.execute('SELECT status FROM shops WHERE shop_id=?', (str(shop_id),)).fetchone()
        if row and row['status'] == 'archived':
            raise ValueError(f'店铺 {shop_id} 已归档，已保留原快照')
        total = save_snapshot(connection, shop_id, result, name=name)
    print(f'[店铺{shop_id}] 已保存快照：{total} 条')
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


def _fetch_open_api_shops(accesses: list[OpenAPIShop], parallel: int) -> tuple[list[dict], list[dict]]:
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
                _, total = _save_snapshot(shop.id, rows, 'wb-openapi', _OPEN_API_METHOD, metadata, shop.name)
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


def fetch_all(no_sync: bool = False, source: str = 'bcs', *, parallel: int = 4) -> dict:
    """BCS 默认先同步再读 BASE 缓存；显式 wb-openapi 时最多4店并发；失败保留旧快照，汇总后报错。"""
    if source not in {'wb-openapi', 'bcs'}:
        raise ValueError('未知快照数据来源')
    if type(parallel) is not int or parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    accesses = bcs.fetch_open_api_shops() if source == 'wb-openapi' else []
    shops = ([{'id': shop.id, 'name': shop.name} for shop in accesses]
             if source == 'wb-openapi' else bcs.fetch_shop_list())
    print(f"共 {len(shops)} 个店铺: {[(s['id'], s['name']) for s in shops]}")
    present = {shop['id'] for shop in shops}
    previous = shop_status()
    archived = {int(shop['shop_id']) for shop in previous if shop['status'] == 'archived'}
    missing = [shop for shop in previous if shop['status'] == 'active' and int(shop['shop_id']) not in present]
    if missing:
        print('[缺失店铺] ' + '、'.join(f"{shop['shop_name']}({shop['shop_id']})" for shop in missing))
    shops = [shop for shop in shops if shop['id'] not in archived]
    accesses = [shop for shop in accesses if shop.id not in archived]
    with db.connect(write=True) as connection:
        for shop in shops:
            save_shop(connection, shop['id'], shop['name'])

    status = {"fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "shops": shops, "errors": [], "source": source, "fetched": [], "missing_shops": missing,
              "sync_warnings": []}

    if source == 'wb-openapi' and no_sync:
        print('[提示] Open API 直接读取 WB，无需 BCS 同步；--no-sync 无需生效')
    if source == 'bcs' and shops and not no_sync:
        t0 = time.time()
        print("\n>>> 并发同步全部店铺（WB → BCS）...")
        status['sync_warnings'] = bcs.sync_shops_parallel([s['id'] for s in shops])
        count = len(status['sync_warnings'])
        print(f'    同步完成 {len(shops) - count} 家，待降级 {count} 家（{time.time() - t0:.0f}s）')

    if source == 'wb-openapi':
        status['fetched'], status['errors'] = _fetch_open_api_shops(accesses, parallel)
    else:
        fallback = {warning['id'] for warning in status['sync_warnings']}
        for i, s in enumerate(shops):
            print(f"\n>>> 拉取店铺 {s['id']} ({s['name']})")
            if s['id'] in fallback:
                print(f"[降级] 店铺 {s['id']} 同步未确认，读取该店 BCS BASE 缓存。")
            try:
                _, total = fetch_shop(s['id'], no_sync=True, source=source, name=s['name'])
                status['fetched'].append({'id': s['id'], 'total': total})
            except Exception as error:
                if isinstance(error, (OSError, sqlite3.Error)):
                    reason = '本地快照保存失败'
                elif isinstance(error, ValueError):
                    reason = '快照数据或店铺状态无效'
                else:
                    reason = sync_failure_reason(error)
                print(f"  [店铺{s['id']} 失败] {reason}")
                status['errors'].append({'id': s['id'], 'name': s['name'], 'error': reason})
            if i < len(shops) - 1:
                time.sleep(1)
        cached = sum(shop['id'] in fallback for shop in status['fetched'])
        print(f"[快照汇总] 已保存 {len(status['fetched'])} 家（降级缓存 {cached} 家），"
              f"失败 {len(status['errors'])} 家")

    with db.connect(write=True) as connection:
        for error in status['errors']:
            connection.execute('UPDATE shops SET last_sync_error = ? WHERE shop_id = ?',
                               (error['error'], str(error['id'])))
    return status


def snapshot_shops() -> list[dict]:
    """列出本地在营店铺，包括尚未取得成功快照的店。"""
    with db.connect() as connection:
        return [{'id': int(row['shop_id']), 'name': row['shop_name']}
                for row in connection.execute("SELECT * FROM shops WHERE status = 'active' ORDER BY rowid")]


def load_all_shops(*, include_trash: bool = False):
    """读取本地商品快照和店铺清单，默认只返回在架商品。"""
    shops_meta = snapshot_shops()
    shops_data = {}
    for shop in shops_meta:
        document = ProductSnapshotRepository.load_snapshot_document(shop['id'])
        if document is not None:
            rows = document['rows']
            shops_data[shop['id']] = rows if include_trash else [r for r in rows if not r.get('trashedAt')]
    if not shops_data:
        raise RuntimeError('未找到店铺快照，请先迁移旧数据或运行 sellerops catalog fetch')
    return shops_data, shops_meta
