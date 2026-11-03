"""按完整 VC 将源店实时商品内容同步到其他店；全部读取先于写入。"""
from contextlib import ExitStack, closing
from typing import Any

from sellerops.features.listings.open_api_common import (
    CardOperation, OperationLog, safe_reason, select_accesses, select_cards, split_values,
)
from sellerops.features.listings.open_api_payloads import (
    card_index, positive_id, update_payload, SYNC_FIELDS,
)
from sellerops.integrations.wildberries.content_api import WBContentAPI


def run(args: Any) -> int:
    source_id = positive_id(args.source_shop)
    fields = set(split_values(args.fields))
    if not fields <= SYNC_FIELDS:
        raise ValueError('不支持的同步字段：' + ','.join(sorted(fields - SYNC_FIELDS)))
    # 在获取凭证前验证选择器，避免无效参数引起远端读取。
    if sum((bool(args.vc), bool(args.nm), bool(args.all))) != 1 or args.limit < 0:
        raise ValueError('须且只能用 --vc、--nm 或 --all 选择源商品；limit 不能为负')
    source, targets = select_accesses(args.shops, source_id)
    log = OperationLog('sync-cards-openapi', args.apply)
    prepared: list[CardOperation] = []
    with ExitStack() as stack:
        source_api = stack.enter_context(closing(WBContentAPI(source.api_key)))
        cards = select_cards(card_index(source_api.cards()), args.vc, args.nm, args.all, args.limit)
        clients = {}
        for shop in targets:
            try:
                api = stack.enter_context(closing(WBContentAPI(shop.api_key)))
                clients[shop.id] = api
                target_index = card_index(api.cards())
            except Exception as exc:
                log.add(shop.id, '*', 'failed', reason=safe_reason(exc))
                continue
            for card in cards:
                vc = card['vendorCode']
                target = target_index.get(vc)
                if target is None:
                    log.add(shop.id, vc, 'skipped', reason='目标非回收站中没有相同完整 VC；不自动建卡或恢复')
                    continue
                try:
                    payload, media = update_payload(card, target, fields)
                    if payload is None and media is None:
                        log.add(shop.id, vc, 'skipped', reason='所选字段相同')
                        continue
                    op = CardOperation(shop.id, vc, positive_id(card['nmID']),
                                       positive_id(target['nmID']), payload, media)
                    prepared.append(op)
                    log.add(shop.id, vc, 'planned', sourceNm=op.source_nm, targetNm=op.target_nm,
                            fields=sorted(fields), card=payload, media=media)
                except Exception as exc:
                    log.add(shop.id, vc, 'failed', reason=safe_reason(exc))
        log.save()
        if args.apply:
            stopped = set()
            for op in prepared:
                row = next(r for r in log.document['items'] if r['shopId'] == op.shop_id and r['vc'] == op.vc)
                if op.shop_id in stopped:
                    row.update(status='blocked', reason='该店此前提交失败，剩余商品未发送')
                    log.save()
                    continue
                api = clients[op.shop_id]
                row['status'] = 'submitting'
                log.save()
                try:
                    if op.card is not None:
                        row['cardReceipt'] = api.update_cards([op.card])
                        log.save()
                    if op.media is not None:
                        row['mediaReceipt'] = api.save_media(op.target_nm, op.media)
                        log.save()
                    row['status'] = 'accepted'
                except Exception as exc:
                    stopped.add(op.shop_id)
                    row['status'] = 'partial' if row.get('cardReceipt') else 'unknown'
                    row['reason'] = safe_reason(exc)
                log.save()
    return log.finish()
