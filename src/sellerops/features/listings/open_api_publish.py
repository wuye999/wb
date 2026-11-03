"""备用官方上架：建卡、有限等待标识、图片和莫斯科仓库存，保留阶段续办。"""
import copy
from contextlib import ExitStack, closing
from typing import Any

from sellerops.features.catalog.snapshot_open_api import resolve_moscow
from sellerops.features.listings.open_api_common import (
    OperationLog, safe_reason, select_accesses, select_cards,
)
from sellerops.features.listings.open_api_payloads import (
    card_index, create_payload, dimensions, media_urls, photo_urls, positive_id, validate_category,
)
from sellerops.features.listings.open_api_sources import (
    fill_public_characteristics, public_inputs, public_template,
)
from sellerops.features.listings.publish_inputs import parse_dims_str
from sellerops.features.listings.open_api_finish import (
    PublishContinuation, can_continue, resume, validate_wait, wait_and_finish,
)
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.persistence import open_api_publish as store


def run(args: Any) -> int:
    validate_wait(args.wait_timeout, args.wait_interval)
    if args.resume:
        return resume(args)
    if type(args.stock) is not int or args.stock < 0 or args.limit < 0:
        raise ValueError('库存须为非负整数，limit 不能为负')
    source_id = positive_id(args.source_shop) if args.source_shop is not None else None
    if source_id is not None:
        if args.file or sum((bool(args.vc), bool(args.nm), bool(args.all))) != 1:
            raise ValueError('自有源店须使用 --vc、--nm 或 --all，不能混入 --file')
        inputs = None
    else:
        if args.all:
            raise ValueError('--all 仅适用于自有源店')
        inputs = public_inputs(args)
    source, targets = select_accesses(args.shops, source_id)
    log = OperationLog('publish-openapi', args.apply)
    with store.operation_lock(), ExitStack() as stack:
        state = store.load_state()
        shared_records = store.load_shared_records()
        sources = []
        if source is not None:
            source_api = stack.enter_context(closing(WBContentAPI(source.api_key)))
            for card in select_cards(card_index(source_api.cards()), args.vc, args.nm, args.all, args.limit):
                sources.append((copy.deepcopy(card), args.price, None))
        else:
            for row in inputs:
                try:
                    nm = positive_id(row.get('nmId'))
                    vc = row.get('vendorCode')
                    if not isinstance(vc, str) or not vc.strip() or vc != vc.strip():
                        raise ValueError('公开商品必须指定完整 vendorCode')
                    card, options = public_template(nm, vc, dims=args.dims, overrides=row.get('card'))
                    sources.append((card, row.get('price', args.price), options))
                except Exception as exc:
                    log.add(0, str(row.get('vendorCode') or '?'), 'failed', reason=safe_reason(exc))
        if len({card['vendorCode'] for card, _, _ in sources}) != len(sources):
            raise ValueError('待建卡商品的完整 VC 重复')
        clients, prepared, warehouses = {}, {}, {}
        continuations = []
        # 业务资料先于建卡准备；后续仅允许为本次待办 VC 有限查询新标识。
        for shop in targets:
            try:
                api = stack.enter_context(closing(WBContentAPI(shop.api_key)))
                clients[shop.id] = api
                warehouse = resolve_moscow(api.warehouses())
                warehouses[shop.id] = warehouse
                existing = card_index(api.cards())
                trash = card_index(api.trash_cards())
                schemas = {}
                for card, _, _ in sources:
                    subject = positive_id(card.get('subjectID'))
                    if subject not in schemas:
                        schemas[subject] = api.characteristics(subject)
            except Exception as exc:
                log.add(shop.id, '*', 'failed', reason=safe_reason(exc))
                continue
            for original, price, options in sources:
                card = copy.deepcopy(original)
                vc = card['vendorCode']
                key = store.item_key(shop.id, vc)
                previous = state['items'].get(key)
                if previous and previous.get('status') not in {'preparing', 'prepare_failed'}:
                    if can_continue(previous):
                        row = log.add(shop.id, vc, 'planned', sourceNm=previous['sourceNm'],
                                      warehouse=warehouse, stock=previous['stock'],
                                      cardReceipt=previous.get('receipt'),
                                      reason='沿用已受理的上架计划，仅续办剩余阶段')
                        continuations.append(PublishContinuation(previous, row, api, warehouse))
                    else:
                        status = 'skipped' if previous.get('status') == 'finished' else 'blocked'
                        log.add(shop.id, vc, status, reason='已有官方上架阶段记录；未确认状态不重新提交')
                    continue
                if vc in existing or vc in trash or store.legacy_submitted(shop.id, vc, shared_records):
                    log.add(shop.id, vc, 'skipped', reason='已有同 VC 商品/回收站卡/BCS 提交记录')
                    continue
                try:
                    if args.dims:
                        parsed = parse_dims_str(args.dims)
                        if not parsed:
                            raise ValueError('--dims 格式无效')
                        card['dimensions'] = dimensions(dict(zip(('length', 'width', 'height', 'weightBrutto'), parsed)))
                    schema = schemas[positive_id(card['subjectID'])]
                    if options is not None:
                        fill_public_characteristics(card, options, schema)
                    validate_category(card, schema)
                    payload = create_payload(card, price)
                    # 建卡仅携带图片；源视频通常是 HLS，不擅自猜测原文件地址。
                    media = media_urls(photo_urls(card))
                    entry = {'shopId': shop.id, 'vc': vc, 'sourceNm': positive_id(card['nmID']),
                             'status': 'planned', 'warehouse': warehouse, 'card': payload,
                             'media': media, 'stock': args.stock, 'stages': {}}
                    prepared.setdefault(shop.id, []).append((key, entry))
                    log.add(shop.id, vc, 'planned', sourceNm=entry['sourceNm'], warehouse=warehouse,
                            card=payload, media=media, stock=args.stock)
                except Exception as exc:
                    log.add(shop.id, vc, 'failed', reason=safe_reason(exc))
        log.save()
        stopped_shops = set()
        if args.apply:
            for sid, operations in prepared.items():
                stopped = False
                for offset in range(0, len(operations), 100):
                    batch = operations[offset:offset + 100]
                    if stopped:
                        for _, entry in batch:
                            row = next(r for r in log.document['items'] if r['shopId'] == sid and r['vc'] == entry['vc'])
                            row.update(status='blocked', reason='该店此前提交失败，剩余商品未发送')
                        log.save()
                        continue
                    # 持久化意图后才发送第一个请求；进程退出也不会自动重复建卡。
                    for key, entry in batch:
                        entry['status'] = 'preparing'
                        state['items'][key] = entry
                    store.save_state(state)
                    try:
                        count = sum(len(entry['card']['variants'][0]['sizes']) for _, entry in batch)
                        barcodes = []
                        for start in range(0, count, 5000):
                            barcodes.extend(clients[sid].barcodes(min(5000, count - start)))
                        if len(set(barcodes)) != count:
                            raise ValueError('条码跨批次重复，已阻止建卡')
                        cursor = 0
                        for _, entry in batch:
                            for size in entry['card']['variants'][0]['sizes']:
                                size['skus'] = [barcodes[cursor]]
                                cursor += 1
                            entry['status'] = 'submitting'
                        store.save_state(state)
                        receipt = clients[sid].create_cards([entry['card'] for _, entry in batch])
                        for key, entry in batch:
                            entry.update(status='accepted', receipt=receipt)
                        store.save_state(state)
                        store.record_shared(sid, [entry['vc'] for _, entry in batch])
                    except Exception as exc:
                        stopped = True
                        stopped_shops.add(sid)
                        for _, entry in batch:
                            if entry['status'] != 'accepted':
                                entry['status'] = 'prepare_failed' if entry['status'] == 'preparing' else 'unknown'
                            entry['reason'] = safe_reason(exc)
                        store.save_state(state)
                    for _, entry in batch:
                        row = next(r for r in log.document['items'] if r['shopId'] == sid and r['vc'] == entry['vc'])
                        row.update(status=('partial' if entry.get('reason') and entry['status'] == 'accepted'
                                           else 'failed' if entry['status'] == 'prepare_failed' else entry['status']))
                        row['receipt'] = entry.get('receipt')
                        if entry.get('reason'):
                            row['reason'] = entry['reason']
                        elif entry['status'] == 'accepted':
                            row['cardReceipt'] = entry['receipt']
                            continuations.append(PublishContinuation(entry, row, clients[sid], warehouses[sid]))
                    log.save()
        active_continuations = []
        for item in continuations:
            if item.entry['shopId'] in stopped_shops:
                item.row.update(status='blocked', reason='该店建卡提交失败，已受理商品保留记录，本轮不继续后续阶段')
            else:
                active_continuations.append(item)
        wait_and_finish(active_continuations, state, log, timeout=args.wait_timeout,
                        interval=args.wait_interval, apply=args.apply)
    return log.finish()
