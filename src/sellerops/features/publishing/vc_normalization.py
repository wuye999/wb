"""历史 VC 前缀规范化；全部读取先于写入，跨店统一分配且不重放提交。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from sellerops.core.files import FileLock
from sellerops.domain.vc_normalization import (
    NormalizationCatalog, RenameAttempt, allocate_codes, replacement_base,
)
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.features.publishing.open_api_common import (
    OperationLog, safe_reason, select_accesses, split_values,
)
from sellerops.features.publishing.open_api_payloads import card_index, positive_id
from sellerops.features.publishing.vc_normalization_payloads import batch_lengths, rename_payload
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.persistence import db
from sellerops.persistence.mappings import PRICE_BOOK_ERRORS
from sellerops.persistence.vc_normalization import COMMAND, load_attempts, load_catalog, write_log
from sellerops.settings import paths


READ_WORKERS = 4


@dataclass
class PreparedRename:
    shop_id: int
    payload: dict[str, Any]
    record: dict[str, Any]


def _read_cards(api: WBContentAPI) -> tuple[dict[str, dict[str, Any]], dict[int, str]]:
    cards, trash = api.cards(), api.trash_cards()
    if not isinstance(cards, list) or not isinstance(trash, list):
        raise ValueError('官方商品/回收站列表不完整')
    active, deleted = card_index(cards), card_index(trash)
    ids: dict[int, str] = {}
    codes = set()
    for row in (*active.values(), *deleted.values()):
        nm, vc = positive_id(row['nmID']), row['vendorCode']
        if nm in ids or vc.upper() in codes:
            raise ValueError('本店商品/回收站标识或 VC 重复，无法完整避重')
        ids[nm] = vc
        codes.add(vc.upper())
    if any(row.get('trashedAt') for row in active.values()):
        raise ValueError('非回收站列表混入回收站商品')
    return active, ids


def _read_shop(api_key: str) -> tuple[WBContentAPI, dict[str, dict[str, Any]], dict[int, str], float]:
    started = monotonic()
    api = WBContentAPI(api_key)
    try:
        active, all_ids = _read_cards(api)
    except BaseException:
        api.close()
        raise
    return api, active, all_ids, monotonic() - started


def _candidate_rows(args: Any, catalog: NormalizationCatalog,
                    cards: dict[int, dict[str, dict[str, Any]]], requested: list[str],
                    log: OperationLog) -> tuple[dict[str, str], list[tuple[int, dict[str, Any], str]]]:
    bases: dict[str, str] = {}
    candidates = []
    prefix_owners = catalog.prefix_owners()
    for sid, index in sorted(cards.items()):
        selected = requested if requested else sorted(index)
        for vc in selected:
            card = index.get(vc)
            if card is None:
                log.add(sid, vc, 'skipped', reason='本店非回收站商品中没有此完整旧 VC')
                continue
            names = catalog.names(vc)
            if args.name and args.name not in names:
                continue
            contains = getattr(args, "name_contains", None)
            if contains and not any(contains in cn for cn in names):
                continue
            detail = {'nmID': positive_id(card['nmID']), 'cn': ' / '.join(sorted(names))}
            if vc.upper() in catalog.excluded:
                log.add(sid, vc, 'skipped', reason='商品已人工排除', **detail)
                continue
            try:
                if vc not in catalog.skus:
                    raise ValueError('缺少明确的完整 VC 现行商品归属')
                cn, prefix = catalog.resolve(vc, _prefix_owners=prefix_owners)
                base = replacement_base(vc, prefix)
                if extract_vc_prefix(vc) == prefix:
                    log.add(sid, vc, 'skipped', reason='已有正确前缀', **detail)
                    continue
                bases[vc] = base
                candidates.append((sid, card, cn))
            except ValueError as exc:
                log.add(sid, vc, 'blocked', reason=str(exc), **detail)
    return bases, candidates


def _prepare(args: Any, catalog: NormalizationCatalog, attempts: list[RenameAttempt],
             cards: dict[int, dict[str, dict[str, Any]]], occupied: dict[tuple[int, int], str],
             requested: list[str], log: OperationLog) -> list[PreparedRename]:
    bases, candidates = _candidate_rows(args, catalog, cards, requested, log)
    assigned, errors = allocate_codes(bases, set(occupied.values()), attempts)
    attempted_cards = {(a.shop_id, a.nm_id) for a in attempts}
    occupied_by_code: dict[str, set[tuple[int, int]]] = {}
    historical_owners: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for attempt in attempts:
        historical_owners.setdefault((attempt.old_vc, attempt.new_vc), set()).add((attempt.shop_id, attempt.nm_id))
    for key, vc in occupied.items():
        occupied_by_code.setdefault(vc.upper(), set()).add(key)
    # 历史固定分配可能已经落在某店。只允许其原来那张卡占用新码。
    for old, new in assigned.items():
        allowed = historical_owners.get((old, new), set())
        if occupied_by_code.get(new.upper(), set()) - allowed:
            errors[old] = '历史分配的新码已被其他商品/回收站卡占用，需人工处理'
    prepared = []
    for sid, card, cn in candidates:
        old, nm = card['vendorCode'], positive_id(card['nmID'])
        detail = dict(nmID=nm, cn=cn, newVC=assigned.get(old), sku=catalog.skus[old])
        reason = errors.get(old)
        if (sid, nm) in attempted_cards:
            reason = '该商品已有已受理/未知/提交中的改码记录，禁止自动重放，请人工处理'
        if reason:
            log.add(sid, old, 'blocked', reason=reason, **detail)
            continue
        try:
            payload = rename_payload(card, assigned[old])
        except Exception as exc:
            log.add(sid, old, 'blocked', reason=safe_reason(exc), **detail)
            continue
        row = log.add(sid, old, 'planned', **detail)
        prepared.append(PreparedRename(sid, payload, row))
    return prepared


def _submit(clients: dict[int, WBContentAPI], prepared: list[PreparedRename], log: OperationLog) -> None:
    # 在首个写请求之前完成所有批次的大小校验。
    items_by_shop: dict[int, list[PreparedRename]] = {}
    if not prepared:
        return
    try:
        catalog = load_catalog()
    except PRICE_BOOK_ERRORS:
        for item in prepared:
            item.record.update(status='blocked', reason='现行商品价格表不可用，请重新预览')
        log.save()
        return
    prefix_owners = catalog.prefix_owners()
    for item in prepared:
        row = item.record
        try:
            current_sku = catalog.skus.get(row['vc'])
            cn, prefix = catalog.resolve(row['vc'], _prefix_owners=prefix_owners)
            if (not current_sku or row.get('sku', current_sku) != current_sku or cn != row['cn']
                    or prefix != extract_vc_prefix(row['newVC'])):
                raise ValueError('改码商品归属或登记前缀已变化')
        except ValueError as error:
            row.update(status='blocked', reason=f'{error}，请重新预览')
            continue
        items_by_shop.setdefault(item.shop_id, []).append(item)
    if len(prepared) != sum(map(len, items_by_shop.values())):
        log.save()
    batches: dict[int, list[list[PreparedRename]]] = {}
    for sid in sorted(clients):
        items = items_by_shop.get(sid, [])
        offset = 0
        batches[sid] = []
        for count in batch_lengths([item.payload for item in items]):
            batches[sid].append(items[offset:offset + count])
            offset += count
    for sid, groups in batches.items():
        stopped = False
        for index, group in enumerate(groups):
            for item in group:
                item.record['status'] = 'submitting'
            # 必须先持久化 submitting，进程中断或保存回执失败均不能重放。
            log.save()
            try:
                receipt = clients[sid].update_cards([item.payload for item in group])
                if not isinstance(receipt, dict) or receipt.get('accepted') is not True:
                    raise ValueError('更新接口没有完整受理回执，禁止自动重放')
            except Exception as exc:
                stopped = True
                reason = safe_reason(exc)
                for item in group:
                    item.record.update(status='unknown', reason=reason)
            else:
                for item in group:
                    item.record.update(status='accepted', cardReceipt=receipt)
            log.save()
            if stopped:
                remaining = groups[index + 1:]
                if remaining:
                    for pending in remaining:
                        for item in pending:
                            item.record.update(status='blocked', reason='本店此前提交失败，后续批次未发送')
                    log.save()
                break


def _run(args: Any, requested: list[str], log: OperationLog) -> int:
    catalog, attempts = load_catalog(), load_attempts()
    _, shops = select_accesses(args.shops)
    if getattr(args, 'capture_plan', False):
        from sellerops.features.catalog.shops import archive_set
        archived = archive_set()
        shops = [shop for shop in shops if shop.id not in archived]
    log.document['shops'] = [{'id': shop.id, 'name': shop.name} for shop in shops]
    cards, occupied, clients = {}, {}, {}
    failures = {}
    with ExitStack() as stack:
        if shops:
            workers = min(READ_WORKERS, len(shops))
            print(f'[读取] 开始拉取 {len(shops)} 个店铺（最多 {workers} 店并发）', flush=True)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                reads = {pool.submit(_read_shop, shop.api_key): shop.id for shop in shops}
                for done, future in enumerate(as_completed(reads), 1):
                    sid = reads[future]
                    try:
                        api, active, all_ids, elapsed = future.result()
                    except Exception as exc:
                        reason = failures[sid] = safe_reason(exc)
                        print(f'[读取] ({done}/{len(shops)}) 店{sid}：失败（{reason}）', flush=True)
                        continue
                    stack.callback(api.close)
                    clients[sid] = api
                    cards[sid] = active
                    occupied.update({(sid, nm): vc for nm, vc in all_ids.items()})
                    print(f'[读取] ({done}/{len(shops)}) 店{sid}：非回收站 {len(active)}，'
                          f'含回收站编码 {len(all_ids)}（{elapsed:.1f}s）', flush=True)
        if failures:
            for shop in shops:
                if shop.id in failures:
                    log.add(shop.id, '*', 'failed', reason=failures[shop.id])
            log.document['aborted'] = '店铺读取不完整；未分配新码，整轮未提交'
            print('[停止] 店铺读取不完整，整轮不提交')
            return log.finish()
        prepared = _prepare(args, catalog, attempts, cards, occupied, requested, log)
        log.save()
        if getattr(args, 'capture_plan', False):
            args.prepared = {'items': [{**item.record, 'card': item.payload} for item in prepared],
                             'blocked': [row for row in log.document['items'] if row['status'] != 'planned']}
        if args.apply:
            _submit(clients, prepared, log)
    return log.finish()


class NormalizationLog(OperationLog):
    def save(self) -> None:
        with db.connect(write=True) as connection:
            write_log(connection, self.path.stem, self.document)

    def result_reference(self) -> str:
        return f'{paths.current.database} ({self.path.stem})'


def run(args: Any) -> int:
    if sum((bool(args.vc), bool(args.name), bool(getattr(args, "name_contains", None)), bool(args.all))) != 1:
        raise ValueError('须且只能用 --vc、--name 或 --all 选择商品')
    if args.name and args.name != args.name.strip():
        raise ValueError('中文商品名不能为空或含首尾空白')
    requested = split_values(args.vc) if args.vc else []
    if args.shops:
        ids = [positive_id(value) for value in split_values(args.shops)]
        if len(ids) != len(set(ids)):
            raise ValueError('店铺 ID 重复')
    log = NormalizationLog(COMMAND, args.apply)
    log.document.update(version=1, selection=dict(vc=requested, name=args.name, name_contains=getattr(args, "name_contains", None), all=args.all))
    try:
        # 同一 data 下串行覆盖历史读取、完整避重、提交与回执，避免两进程分配同码。
        with FileLock(Path(paths.current.state_dir) / COMMAND):
            return _run(args, requested, log)
    except Exception as exc:
        log.add(0, '*', 'failed', reason=safe_reason(exc))
        try:
            return log.finish()
        except Exception:
            print('[错误] 改码日志无法保存；已停止，禁止据此重放提交')
            return 1


def apply_frozen(args: Any) -> int:
    log = NormalizationLog(COMMAND, True)
    log.document['version'] = 1
    planned = args.frozen['items']
    ids = sorted({row['shopId'] for row in planned})
    with FileLock(Path(paths.current.state_dir) / COMMAND), ExitStack() as stack:
        _, shops = select_accesses(','.join(map(str, ids)))
        history = load_attempts()
        completed = set()
        history_by_old: dict[str, set[str]] = {}
        history_by_new: dict[str, set[str]] = {}
        for attempt in history:
            completed.add((attempt.shop_id, attempt.nm_id))
            history_by_old.setdefault(attempt.old_vc, set()).add(attempt.new_vc)
            history_by_new.setdefault(attempt.new_vc.upper(), set()).add(attempt.old_vc)
        clients = {shop.id: stack.enter_context(closing(WBContentAPI(shop.api_key))) for shop in shops}
        prepared = []
        for record in planned:
            row = {k: v for k, v in record.items() if k != 'card'}
            log.document['items'].append(row)
            if (row['shopId'], row['nmID']) in completed:
                row.update(status='blocked', reason='\u672c\u5e97\u540c\u5361\u5df2\u6709\u63d0\u4ea4\u8bb0\u5f55')
                continue
            previous = history_by_old.get(row['vc'])
            target_owners = history_by_new.get(row['newVC'].upper())
            conflict = target_owners and (len(target_owners) > 1 or row['vc'] not in target_owners)
            if conflict or (previous and previous != {row['newVC']}):
                row.update(status='blocked', reason='\u8be5\u65e7 VC \u7684\u5386\u53f2\u65b0\u7801\u5df2\u53d8\u5316\uff0c\u8bf7\u91cd\u65b0\u9884\u89c8')
                continue
            prepared.append(PreparedRename(row['shopId'], record['card'], row))
        _submit(clients, prepared, log)
    args.outcome = {'items': log.document['items']}
    return log.finish()
