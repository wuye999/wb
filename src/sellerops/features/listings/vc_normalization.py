"""历史 VC 前缀规范化；全部读取先于写入，跨店统一分配且不重放提交。"""
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sellerops.core.files import FileLock
from sellerops.domain.vc_normalization import (
    NormalizationCatalog, RenameAttempt, allocate_codes, replacement_base,
)
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.features.listings.open_api_common import (
    OperationLog, safe_reason, select_accesses, split_values,
)
from sellerops.features.listings.open_api_payloads import card_index, positive_id
from sellerops.features.listings.vc_normalization_payloads import batch_lengths, rename_payload
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.persistence.vc_normalization import COMMAND, load_attempts, load_catalog
from sellerops.settings import paths


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


def _candidate_rows(args: Any, catalog: NormalizationCatalog,
                    cards: dict[int, dict[str, dict[str, Any]]], requested: list[str],
                    log: OperationLog) -> tuple[dict[str, str], list[tuple[int, dict[str, Any], str]]]:
    bases: dict[str, str] = {}
    candidates = []
    for sid, index in sorted(cards.items()):
        selected = requested if requested else sorted(index)
        for vc in selected:
            card = index.get(vc)
            if card is None:
                log.add(sid, vc, 'skipped', reason='本店非回收站商品中没有此完整旧 VC')
                continue
            names = catalog.names(vc)
            if args.name and not any(args.name in cn for cn in names):
                continue
            detail = {'nmID': positive_id(card['nmID']), 'cn': ' / '.join(sorted(names))}
            if vc.upper() in catalog.excluded:
                log.add(sid, vc, 'skipped', reason='商品已人工排除', **detail)
                continue
            try:
                cn, prefix = catalog.resolve(vc)
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
        detail = dict(nmID=nm, cn=cn, newVC=assigned.get(old))
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
        row = log.add(sid, old, 'planned', card=payload, **detail)
        prepared.append(PreparedRename(sid, payload, row))
    return prepared


def _submit(clients: dict[int, WBContentAPI], prepared: list[PreparedRename], log: OperationLog) -> None:
    # 在首个写请求之前完成所有批次的大小校验。
    batches: dict[int, list[list[PreparedRename]]] = {}
    for sid in sorted(clients):
        items = [item for item in prepared if item.shop_id == sid]
        offset = 0
        batches[sid] = []
        for count in batch_lengths([item.payload for item in items]):
            batches[sid].append(items[offset:offset + count])
            offset += count
    for sid, groups in batches.items():
        stopped = False
        for group in groups:
            if stopped:
                for item in group:
                    item.record.update(status='blocked', reason='本店此前提交失败，后续批次未发送')
                log.save()
                continue
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
                for item in group:
                    item.record.update(status='unknown', reason=safe_reason(exc))
            else:
                for item in group:
                    item.record.update(status='accepted', cardReceipt=receipt)
            log.save()


def _run(args: Any, requested: list[str], log: OperationLog) -> int:
    catalog, attempts = load_catalog(), load_attempts()
    _, shops = select_accesses(args.shops)
    log.document['shops'] = [{'id': shop.id, 'name': shop.name} for shop in shops]
    cards, occupied, clients = {}, {}, {}
    failed = False
    with ExitStack() as stack:
        for shop in shops:
            try:
                api = stack.enter_context(closing(WBContentAPI(shop.api_key)))
                clients[shop.id] = api
                active, all_ids = _read_cards(api)
                cards[shop.id] = active
                occupied.update({(shop.id, nm): vc for nm, vc in all_ids.items()})
                print(f'[读取] 店{shop.id}：非回收站 {len(active)}，含回收站编码 {len(all_ids)}')
            except Exception as exc:
                failed = True
                log.add(shop.id, '*', 'failed', reason=safe_reason(exc))
        if failed:
            log.document['aborted'] = '店铺读取不完整；未分配新码，整轮未提交'
            print('[停止] 店铺读取不完整，整轮不提交')
            return log.finish()
        prepared = _prepare(args, catalog, attempts, cards, occupied, requested, log)
        log.save()
        if args.apply:
            _submit(clients, prepared, log)
    return log.finish()


def run(args: Any) -> int:
    if sum((bool(args.vc), bool(args.name), bool(args.all))) != 1:
        raise ValueError('须且只能用 --vc、--name 或 --all 选择商品')
    if args.name and args.name != args.name.strip():
        raise ValueError('中文商品名不能为空或含首尾空白')
    requested = split_values(args.vc) if args.vc else []
    if args.shops:
        ids = [positive_id(value) for value in split_values(args.shops)]
        if len(ids) != len(set(ids)):
            raise ValueError('店铺 ID 重复')
    log = OperationLog(COMMAND, args.apply)
    log.document.update(version=1, selection=dict(vc=requested, name=args.name, all=args.all))
    try:
        # 同一 data 下串行覆盖历史读取、完整避重、提交与回执，避免两进程分配同码。
        with FileLock(Path(paths.LOG_DIR) / COMMAND):
            return _run(args, requested, log)
    except Exception as exc:
        log.add(0, '*', 'failed', reason=safe_reason(exc))
        try:
            return log.finish()
        except Exception:
            print('[错误] 改码日志无法保存；已停止，禁止据此重放提交')
            return 1
