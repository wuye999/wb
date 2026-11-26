"""
sellerops 店铺商品拉取与快照管理（原 fetch_products.py + sync_shops.fetch_all）

默认使用 BCS 快照接口：先同步 WB → BCS，再读取 BASE 缓存，兼容原本地快照契约。
显式 source=wb-openapi 时改为用 BCS 保存的 apiKey 直接读取 WB Open API。
"""

import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from sellerops.integrations.bcs.budget import RequestBudget, SyncInterrupted
from sellerops.integrations.bcs.client import OpenAPIShop, SYNC_STAGE_SECONDS, client as bcs, sync_failure_reason
from sellerops.persistence import db
from sellerops.persistence.mappings import (
    PRICE_BOOK_ERRORS, PriceBook, PriceBookChanged, read_current_price_book, save_price_book,
)
from sellerops.persistence.price_book import is_price_book_prefix
from sellerops.persistence.snapshots import (
    ProductSnapshotRepository, get_shop, record_sync_error, save_shop, save_snapshot, snapshot_metadata,
)
from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.integrations.wildberries.open_api import OpenAPIResponseError
from sellerops.features.catalog import snapshot_open_api
from sellerops.features.catalog.shops import shop_status, snapshot_freshness
from sellerops.domain.snapshot_values import first_price, stock_values, valid_sizes
from sellerops.domain.vendor_codes import extract_vc_prefix

_OPEN_API_METHOD = 'wb-openapi(content.cards, prices.nmList, moscow.chrtIds)'
_BCS_METHOD = 'bcs.fetch_shop_products(filter=BASE, pageSize=10000)'
BCS_REMOTE_SECONDS = 30 * 60
_COMPLETE_PREFIX_VC = re.compile(r'BCS-[A-Za-z]{4}[-/][^\s/-](?:\S*[^\s/-])?')


def _book_skip(error: Exception) -> tuple[str, str]:
    if isinstance(error, FileNotFoundError):
        return 'missing_price_book', '商品价格表缺失'
    if isinstance(error, OSError):
        return 'unreadable_price_book', '商品价格表被占用或不可读，请保存并关闭 Excel'
    if isinstance(error, PriceBookChanged):
        return 'price_book_changed', '商品价格表版本已变化'
    return 'invalid_price_book', str(error)


@dataclass(frozen=True)
class PrefixMapping:
    book: PriceBook | None
    unique: dict[str, dict]
    conflicts: set[str]
    skip_code: str | None = None
    skip_reason: str | None = None

    @classmethod
    def prepare(cls):
        try:
            book = read_current_price_book(strict_identity=True)
        except PRICE_BOOK_ERRORS as error:
            return cls(None, {}, set(), *_book_skip(error))
        counts = Counter(row['prefix'] for row in book.rows if is_price_book_prefix(row['prefix']))
        return cls(book, {row['prefix']: row for row in book.rows if counts[row['prefix']] == 1},
                   {prefix for prefix, count in counts.items() if count > 1})

    def save(self, connection, shop_id: int) -> dict:
        result = {'prefix_mapped': 0}
        if self.book is None:
            return {**result, 'prefix_skip_code': self.skip_code, 'prefix_skip_reason': self.skip_reason}
        connection.execute('SAVEPOINT prefix_mapping')
        try:
            save_price_book(connection, self.book)
            updates, skipped = [], Counter()
            stamp = db.now()
            for row in connection.execute("""
                SELECT i.vc, m.block_listing FROM shop_items i JOIN vc_map m ON m.vc = i.vc
                 WHERE i.shop_id = ? AND i.is_trashed = 0 AND m.status = 'pending'
            """, (str(shop_id),)):
                vc = row['vc']
                prefix = extract_vc_prefix(vc) if _COMPLETE_PREFIX_VC.fullmatch(vc) else None
                if row['block_listing']:
                    skipped['blocked'] += 1
                elif not prefix:
                    skipped['invalid_vc_prefix'] += 1
                elif prefix in self.conflicts:
                    skipped['ambiguous_prefix'] += 1
                elif product := self.unique.get(prefix):
                    updates.append((product['sku'], product['cn'], stamp, vc))
                else:
                    skipped['unmatched_prefix'] += 1
            changed = connection.executemany("""
                UPDATE vc_map SET sku = ?, last_cn = ?, status = 'mapped', source = 'prefix', updated_at = ?
                 WHERE vc = ? AND status = 'pending' AND coalesce(block_listing, '') = ''
            """, updates).rowcount
            self.book.verify()
            result = {'prefix_mapped': changed, 'prefix_skipped': dict(skipped)}
            if not self.unique:
                result.update(prefix_skip_code='no_usable_prefix', prefix_skip_reason='价格表没有可用的唯一四位前缀')
        except (OSError, PriceBookChanged) as error:
            connection.execute('ROLLBACK TO prefix_mapping')
            result['prefix_skip_code'], result['prefix_skip_reason'] = _book_skip(error)
        connection.execute('RELEASE prefix_mapping')
        return result


def is_empty_product(r):
    """空商品三缺判定：价格+库存+名称全空（productType=ERROR 残留），不计入待审核。
    （原逻辑从「空商品处理/cleanup_empty_products.py」内联，去掉跨目录依赖）"""
    sizes = valid_sizes(r) or []
    has_price = any(first_price({'sizeList': [size]}) is not None for size in sizes)
    return not has_price and stock_values(r) is None and not (r.get('title') or '').strip()


def fetch_shop(shop_id: int, no_sync: bool = False,
               source: str = 'bcs', *, access: OpenAPIShop | None = None, name: str = '') -> tuple[list[dict], int]:
    """完整拉取并原子保存单店快照，保留 Python 调用方的返回值和异常契约。"""
    rows, status, error = _fetch_single(shop_id, no_sync, source, access=access, name=name)
    if error is not None:
        raise error
    return rows, status['fetched'][0]['total']


def fetch_single_status(shop_id: int, no_sync: bool = False, source: str = 'bcs') -> dict:
    _, status, _ = _fetch_single(shop_id, no_sync, source)
    return status


def _receipt(source: str, shops: list[dict], missing: list[dict] | None = None) -> dict:
    return {'source': source, 'fetchedAt': db.now(), 'shops': shops, 'fetched': [],
            'sync_warnings': [], 'errors': [], 'missing_shops': missing or [],
            'prefix_mapping': {'mapped': 0, 'skipped_shops': [], 'skipped_vcs': {}},
            'mapping_counts': {'mapped_vcs': 0, 'pending_vcs': 0}}


def _finish_receipt(receipt: dict) -> None:
    fetched = receipt['fetched']
    if not fetched:
        return
    counts = Counter()
    summary = receipt['prefix_mapping']
    for row in fetched:
        summary['mapped'] += row['prefix_mapped']
        if reason := row.get('prefix_skip_reason'):
            summary['skipped_shops'].append({'id': row['id'], 'reason': reason})
        counts.update(row.get('prefix_skipped', {}))
    summary['skipped_vcs'] = dict(counts)

    shop_ids = [str(row['id']) for row in fetched]
    slots = ','.join('?' for _ in shop_ids)
    try:
        with db.connect(read_only=True) as connection:
            totals = {
                shop_id: {'mapped_vcs': mapped, 'pending_vcs': pending}
                for shop_id, mapped, pending in connection.execute(f"""
                    WITH scoped AS (
                        SELECT i.shop_id, i.vc, coalesce(m.status, 'pending') AS mapping_status
                        FROM shop_items i JOIN shops s USING (shop_id)
                        LEFT JOIN vc_map m ON m.vc = i.vc
                        WHERE i.shop_id IN ({slots}) AND s.status = 'active' AND i.is_trashed = 0
                          AND i.vc <> '' AND m.status IS NOT 'excluded'
                    )
                    SELECT shop_id, count(*) FILTER (WHERE mapping_status = 'mapped'),
                           count(*) FILTER (WHERE mapping_status = 'pending')
                    FROM scoped GROUP BY shop_id
                    UNION ALL
                    SELECT NULL, count(DISTINCT vc) FILTER (WHERE mapping_status = 'mapped'),
                           count(DISTINCT vc) FILTER (WHERE mapping_status = 'pending')
                    FROM scoped
                """, shop_ids)
            }
    except (OSError, sqlite3.Error):
        unknown = dict.fromkeys(receipt['mapping_counts'])
        receipt['mapping_counts'] = unknown
        for row in fetched:
            row.update(unknown)
    else:
        for row, shop_id in zip(fetched, shop_ids):
            row.update(totals.get(shop_id, receipt['mapping_counts']))
        receipt['mapping_counts'] = totals[None]


def _fetch_single(shop_id: int, no_sync: bool, source: str, *,
                  access: OpenAPIShop | None = None, name: str = '') -> tuple[list[dict], dict, Exception | None]:
    if type(shop_id) is not int or shop_id <= 0:
        raise ValueError('店铺 ID 必须为正整数')
    if source not in {'wb-openapi', 'bcs'}:
        raise ValueError('未知快照数据来源')
    with db.connect(read_only=True) as connection:
        before = get_shop(connection, shop_id)
    if before and before['status'] == 'archived':
        raise ValueError(f'店铺 {shop_id} 已归档，请先恢复')
    shop = {'id': shop_id, 'name': name or (before['shop_name'] if before else f'shop{shop_id}')}
    status = _receipt(source, [shop])
    prefixes = PrefixMapping.prepare()
    try:
        if source == 'wb-openapi':
            if no_sync:
                print('[提示] Open API 直接读取 WB，无需 BCS 同步；--no-sync 无需生效')
            if access is None:
                matches = [item for item in bcs.fetch_open_api_shops() if item.id == shop_id]
                if len(matches) != 1:
                    raise RuntimeError('BCS 未返回唯一目标店铺，无法读取该店 apiKey')
                access = matches[0]
            if access.id != shop_id:
                raise ValueError('店铺密钥归属与目标快照不一致')
            shop['name'] = access.name
            print(f'[拉取] 店铺 {shop_id}：WB Open API 商品、价格与莫斯科仓库存 ...')
            try:
                rows, metadata = snapshot_open_api.collect_shop(access)
            except Exception as error:
                raise OpenAPIResponseError(_safe_error(error)) from None
            fetched = _save_snapshot(shop, rows, source, 'wb_openapi', _OPEN_API_METHOD, metadata, prefixes)
        else:
            start = time.monotonic()
            budget = RequestBudget(start + BCS_REMOTE_SECONDS)
            warning = None
            if not no_sync:
                print(f'[同步] 店铺 {shop_id}：同步 WB 数据到 BCS')
                warning = bcs.synchronize_shop(shop_id, budget=RequestBudget(start + SYNC_STAGE_SECONDS))
                if warning:
                    status['sync_warnings'].append(warning)
                else:
                    print(f'[同步] 店铺 {shop_id}：BCS 已确认同步完成')
            rows, fetched = _collect_bcs(shop, no_sync, warning, budget, prefixes)
    except Exception as error:
        status['errors'].append(_record_failure(shop, before, source, error))
        return [], status, error
    status['fetched'].append(fetched)
    _finish_receipt(status)
    return rows, status, None


def _collect_bcs(shop: dict, no_sync: bool, warning: dict | None,
                 budget: RequestBudget, prefixes: PrefixMapping) -> tuple[list[dict], dict]:
    shop_id = shop['id']
    if warning:
        if warning.get('kind') == 'authentication':
            raise AuthenticationError()
        if warning.get('kind') == 'authentication_aborted':
            raise SyncInterrupted('authentication_aborted', 'BCS 鉴权熔断，已停止后续请求')
    budget.remaining()
    if warning:
        status = 'bcs_cache_fallback'
        print(f'[降级] 店铺 {shop_id} 同步未确认，读取该店 BCS BASE 缓存。')
    elif no_sync:
        status = 'bcs_cache_only'
        print(f'[缓存] 店铺 {shop_id} 已跳过同步，读取该店 BCS BASE 缓存。')
    else:
        status = 'bcs_synced'
    print(f'[拉取] 店铺 {shop_id}：在架商品（BASE）')
    rows = bcs.fetch_shop_products(shop_id, filter_type='BASE', budget=budget)
    metadata = {'warnings': {
        'missingSpecProducts': sum(valid_sizes(row) is None for row in rows),
        'missingStockProducts': sum(stock_values(row) is None for row in rows),
        'missingPriceProducts': sum(first_price(row) is None for row in rows),
    }}
    return rows, _save_snapshot(shop, rows, 'bcs', status, _BCS_METHOD, metadata, prefixes)


def _save_snapshot(shop: dict, rows: list[dict], source: str, status: str, method: str, metadata: dict,
                   prefixes: PrefixMapping) -> dict:
    shop_id = shop['id']
    timestamp = datetime.now().isoformat(' ', timespec='microseconds')
    result = {
        'fetchedAt': timestamp, 'fetchMethod': method, 'source': source,
        'code': 200, 'msg': '查询成功', 'rows': rows, **metadata,
    }
    with db.connect(write=True) as connection:
        row = get_shop(connection, shop_id)
        if row and row['status'] == 'archived':
            raise ValueError(f'店铺 {shop_id} 已归档，已保留原快照')
        total = save_snapshot(connection, shop_id, result, name=shop['name'], snapshot_status=status)
        fields = snapshot_metadata(get_shop(connection, shop_id))
        prefix_result = prefixes.save(connection, shop_id)
    print(f'[店铺{shop_id}] 已保存快照：{total} 条')
    if reason := prefix_result.get('prefix_skip_reason'):
        print(f'[店铺{shop_id}] 跳过前缀归属：{reason}')
    else:
        print(f"[店铺{shop_id}] 前缀新增归属 {prefix_result['prefix_mapped']} 条")
    vcs = len({r.get('vendorCode') for r in rows})
    n_alive = sum(1 for r in rows if not r.get('trashedAt'))
    print(f'[店铺{shop_id}] 统计：商品条数={len(rows)}，唯一 vendorCode={vcs}，在架(trashedAt空)={n_alive}')
    labels = {'missingPriceSpecs': '缺少价格的规格', 'missingStockSpecs': '未返回库存记录的规格',
              'missingImages': '缺少图片的商品', 'missingSpecProducts': '规格不完整的商品',
              'missingStockProducts': '库存无法确认的商品', 'missingPriceProducts': '价格无法确认的商品'}
    for key, count in metadata.get('warnings', {}).items():
        if count:
            print(f'[店铺{shop_id} 告警] {labels.get(key, key)}={count}（缺失保留为空，不补零）')
    return {'id': shop_id, 'total': total, 'fetchedAt': timestamp, **fields, **prefix_result}


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


def _record_failure(shop: dict, before: sqlite3.Row | None, source: str, error: Exception) -> dict:
    if isinstance(error, (OSError, sqlite3.Error)):
        reason = '本地快照保存失败'
    elif source == 'wb-openapi':
        reason = _safe_error(error)
    elif isinstance(error, ValueError):
        reason = '快照数据或店铺状态无效'
    else:
        reason = sync_failure_reason(error)
    shop_id = shop['id']
    print(f'[店铺{shop_id} 失败] {reason}')
    if before is not None:
        try:
            with db.connect(write=True) as connection:
                record_sync_error(connection, shop_id, before, reason)
        except (OSError, sqlite3.Error, ValueError):
            print(f'[店铺{shop_id} 警告] 本地无法登记同步失败', file=sys.stderr)
    result = {**shop, 'error': reason}
    if isinstance(error, SyncInterrupted):
        result['kind'] = error.kind
    elif source == 'bcs' and isinstance(error, AuthenticationError):
        result['kind'] = 'authentication'
    return result


def _fetch_open_api_shops(accesses: list[OpenAPIShop], parallel: int, before: dict,
                         prefixes: PrefixMapping) -> tuple[list[dict], list[dict]]:
    """有界并发采集；主线程逐店保存快照并汇总，不共享会话或令牌。"""
    fetched, errors = [], []
    if not accesses:
        return fetched, errors
    workers = min(parallel, len(accesses))
    print(f'\n>>> WB Open API 并发拉取：同时最多 {workers} 个店铺（店内按原限流顺序读取）')
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='snapshot') as pool:
        pending = {pool.submit(snapshot_open_api.collect_shop, shop): shop for shop in accesses}
        for future in as_completed(pending):
            access = pending[future]
            shop = {'id': access.id, 'name': access.name}
            try:
                rows, metadata = future.result()
                fetched.append(_save_snapshot(shop, rows, 'wb-openapi', 'wb_openapi', _OPEN_API_METHOD, metadata, prefixes))
            except Exception as error:
                errors.append(_record_failure(shop, before[access.id], 'wb-openapi', error))
            print(f'[进度] 已完成 {len(fetched) + len(errors)}/{len(accesses)} 店，'
                  f'成功 {len(fetched)}，失败 {len(errors)}')
    order = {shop.id: i for i, shop in enumerate(accesses)}
    fetched.sort(key=lambda row: order[row['id']])
    errors.sort(key=lambda row: order[row['id']])
    return fetched, errors


def _fetch_bcs_shops(status: dict, before: dict, no_sync: bool, workers: int, start: float,
                     prefixes: PrefixMapping) -> None:
    shops = status['shops']
    if not no_sync and shops:
        print(f'>>> 同步 {len(shops)} 家店铺（WB → BCS），并发上限 {workers}')
        status['sync_warnings'] = bcs.sync_shops_parallel(
            [shop['id'] for shop in shops], workers=workers, deadline=start + SYNC_STAGE_SECONDS)
        count = len(status['sync_warnings'])
        print(f'[同步汇总] 已确认 {len(shops) - count} 家，未确认 {count} 家')
    warnings = {warning['id']: warning for warning in status['sync_warnings']}
    auth_failed = any(warning.get('kind') == 'authentication' for warning in warnings.values())
    budget = RequestBudget(start + BCS_REMOTE_SECONDS)
    for i, shop in enumerate(shops):
        shop_id = shop['id']
        warning = warnings.get(shop_id)
        try:
            if auth_failed and (not warning or warning.get('kind') != 'authentication'):
                raise SyncInterrupted('authentication_aborted', 'BCS 鉴权熔断，已停止后续请求')
            _, fetched = _collect_bcs(shop, no_sync, warning, budget, prefixes)
            status['fetched'].append(fetched)
        except Exception as error:
            if isinstance(error, AuthenticationError):
                auth_failed = True
            status['errors'].append(_record_failure(shop, before[shop_id], 'bcs', error))
        if not auth_failed and i < len(shops) - 1 and time.monotonic() < budget.deadline:
            try:
                budget.pause(1)
            except SyncInterrupted:
                # Subsequent shops record the exhausted budget in their own error entries.
                pass


def fetch_all(no_sync: bool = False, source: str = 'bcs', *, parallel: int = 4, bcs_parallel: int = 2,
              shop_ids: list[int] | None = None, needs_refresh: bool = False) -> dict:
    """拉取所选店铺快照，逐店保存结果并在失败时保留旧快照。"""
    if source not in {'wb-openapi', 'bcs'}:
        raise ValueError('未知快照数据来源')
    if type(parallel) is not int or parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    if type(bcs_parallel) is not int or bcs_parallel not in range(1, 5):
        raise ValueError('BCS 并发店铺数必须为 1–4')
    if needs_refresh and no_sync:
        raise ValueError('--needs-refresh 不能与 --no-sync 同时使用')
    if shop_ids is not None and any(type(shop_id) is not int or shop_id <= 0 for shop_id in shop_ids):
        raise ValueError('店铺 ID 必须为正整数')

    scope = set(shop_ids) if shop_ids is not None else None
    previous = shop_status(include_snapshot=needs_refresh)
    local = {int(shop['shop_id']): shop for shop in previous}
    archived = {shop_id for shop_id, shop in local.items() if shop['status'] == 'archived'}
    if scope is not None:
        if blocked := scope & archived:
            raise ValueError(f"店铺 {','.join(map(str, sorted(blocked)))} 已归档，请先恢复")
        if needs_refresh and (untracked := scope - local.keys()):
            ids = ','.join(map(str, sorted(untracked)))
            raise ValueError(f'店铺 {ids} 未登记到本地，请去掉 --needs-refresh 执行首次同步')
    if needs_refresh:
        due = {int(shop['shop_id']) for shop in snapshot_freshness(previous)['shops']}
        scope = due if scope is None else scope & due
        for shop in previous:
            shop.pop('snapshot_present')
    if scope is not None and not scope:
        return _receipt(source, [])

    start = time.monotonic()
    accesses = bcs.fetch_open_api_shops() if source == 'wb-openapi' else []
    shops = ([{'id': shop.id, 'name': shop.name} for shop in accesses] if source == 'wb-openapi'
             else bcs.fetch_shop_list(budget=RequestBudget(start + BCS_REMOTE_SECONDS)))
    present = {shop['id'] for shop in shops}
    selected = present - archived
    if scope is not None:
        selected &= scope
    shops = [shop for shop in shops if shop['id'] in selected]
    accesses = [shop for shop in accesses if shop.id in selected]
    print(f"目标店铺 {len(shops)} 家：{[(s['id'], s['name']) for s in shops]}")
    missing = [shop for shop_id, shop in local.items() if shop['status'] == 'active' and shop_id not in present
               and (scope is None or shop_id in scope)]
    if missing:
        print('[缺失店铺] ' + '、'.join(f"{shop['shop_name']}({shop['shop_id']})" for shop in missing))
    status = _receipt(source, shops, missing)
    if scope is not None:
        for shop_id in sorted(scope - present - local.keys()):
            reason = '远端店铺清单未找到该 ID'
            status['errors'].append({'id': shop_id, 'error': reason, 'kind': 'shop_not_found'})
            print(f'[店铺{shop_id} 失败] {reason}')
    if not shops:
        return status
    before = {}
    with db.connect(write=True) as connection:
        for shop in shops:
            save_shop(connection, shop['id'], shop['name'])
            before[shop['id']] = get_shop(connection, shop['id'])
    prefixes = PrefixMapping.prepare()
    if source == 'wb-openapi':
        if no_sync:
            print('[提示] Open API 直接读取 WB，无需 BCS 同步；--no-sync 无需生效')
        status['fetched'], errors = _fetch_open_api_shops(accesses, parallel, before, prefixes)
        status['errors'].extend(errors)
    else:
        _fetch_bcs_shops(status, before, no_sync, bcs_parallel, start, prefixes)
    _finish_receipt(status)
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
