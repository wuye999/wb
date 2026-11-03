"""上架流程内的有限标识等待、图片/库存提交，以及持久化续办。"""
import math
import time
from contextlib import ExitStack, closing
from dataclasses import dataclass
from typing import Any

from sellerops.features.catalog.snapshot_open_api import resolve_moscow
from sellerops.features.listings.open_api_common import OperationLog, safe_reason, select_accesses, split_values
from sellerops.features.listings.open_api_payloads import card_index, positive_id, sizes, media_urls
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.persistence import open_api_publish as store


@dataclass
class PublishContinuation:
    entry: dict[str, Any]
    row: dict[str, Any]
    api: WBContentAPI
    warehouse: dict[str, Any]


@dataclass
class _ReadyAssets:
    continuation: PublishContinuation
    nm: int
    urls: list[str]
    stocks: list[dict[str, int]]


class _CardPending(Exception):
    """目标卡片或完整规格尚未生成，可以继续有限等待。"""


def validate_wait(timeout: float, interval: float) -> None:
    if not math.isfinite(timeout) or not 0 <= timeout <= 7200:
        raise ValueError('--wait-timeout 须在 0 到 7200 秒之间（0=只查询一次）')
    if not math.isfinite(interval) or not 1 <= interval <= 60:
        raise ValueError('--wait-interval 须在 1 到 60 秒之间')


def can_continue(entry: dict[str, Any]) -> bool:
    return entry.get('status') == 'accepted' and all(
        stage.get('status') == 'accepted' for stage in entry.get('stages', {}).values())


def _assets(continuation: PublishContinuation, card: dict[str, Any] | None) -> _ReadyAssets:
    entry = continuation.entry
    if entry['warehouse'] != continuation.warehouse:
        raise ValueError('莫斯科仓名称或 id 与建卡准备记录不一致')
    if card is None:
        raise _CardPending('尚未生成该完整 VC 的商品卡')
    if positive_id(card.get('subjectID')) != positive_id(entry['card']['subjectID']):
        raise ValueError('生成后商品的类目与建卡记录不一致')
    raw = card.get('sizes')
    if (not isinstance(raw, list) or not raw or any(
            isinstance(size, dict) and (not size.get('chrtID') or not size.get('skus')) for size in raw)):
        raise _CardPending('商品卡已出现，完整规格标识尚未生成')
    specs = sizes(raw, keep_ids=True)
    expected = entry['card']['variants'][0]['sizes']
    actual_codes = {s for size in specs for s in size['skus']}
    expected_codes = {s for size in expected for s in size['skus']}
    if actual_codes - expected_codes or len(specs) > len(expected):
        raise ValueError('生成后商品的规格条码与本次建卡记录不一致')
    if actual_codes != expected_codes or len(specs) != len(expected):
        raise _CardPending('商品卡已出现，规格尚未完整生成')
    nm = positive_id(card.get('nmID'))
    if entry.get('targetNm') not in (None, nm):
        raise ValueError('目标商品码与此前就绪记录不一致')
    if card.get('video') and entry.get('stages', {}).get('media', {}).get('status') != 'accepted':
        raise ValueError('目标已有视频，图片上传会覆盖媒体，请人工明确处理')
    urls = media_urls(entry['media'])
    stock = entry['stock']
    if type(stock) is not int or stock < 0:
        raise ValueError('上架记录库存无效')
    stocks = [{'chrtId': spec['chrtID'], 'amount': stock} for spec in specs]
    if len(stocks) > 1000:
        raise ValueError('单商品规格超过库存提交上限')
    return _ReadyAssets(continuation, nm, urls, stocks)


def _submit(ready: list[_ReadyAssets], state: dict[str, Any], log: OperationLog) -> None:
    stopped = set()
    for plan in ready:
        item = plan.continuation
        entry, row, api = item.entry, item.row, item.api
        sid = entry['shopId']
        if sid in stopped:
            row.update(status='blocked', reason='该店此前提交失败，剩余商品未发送')
            log.save()
            continue
        stages = entry.setdefault('stages', {})
        try:
            for name, submit in (
                ('media', lambda: api.save_media(plan.nm, plan.urls)),
                ('stock', lambda: api.set_stocks(item.warehouse['id'], plan.stocks)),
            ):
                if stages.get(name, {}).get('status') == 'accepted':
                    continue
                stages[name] = {'status': 'submitting'}
                store.save_state(state)
                receipt = submit()
                stages[name] = {'status': 'accepted', 'receipt': receipt}
                store.save_state(state)
            entry['status'] = 'finished'
            row.update(status='accepted', reason='建卡、图片与莫斯科仓库存均已受理')
        except Exception as exc:
            stopped.add(sid)
            for stage in stages.values():
                if stage.get('status') == 'submitting':
                    stage['status'] = 'unknown'
            row.update(status='partial', reason=safe_reason(exc))
        store.save_state(state)
        row['stages'] = stages
        log.save()


def wait_and_finish(items: list[PublishContinuation], state: dict[str, Any], log: OperationLog,
                    *, timeout: float, interval: float, apply: bool) -> None:
    """仅查询待续办 VC；全部标识读取结束后才开始媒体/库存写入，之后不回读。"""
    validate_wait(timeout, interval)
    pending, ready, failed_shops = list(items), [], set()
    deadline = time.monotonic() + timeout
    max_rounds = max(1, math.ceil(timeout / interval) + 1) if apply else 1
    for attempt in range(max_rounds):
        remaining = []
        for item in pending:
            entry, row = item.entry, item.row
            sid, vc = entry['shopId'], entry['vc']
            if sid in failed_shops:
                row.update(status='blocked', reason='该店标识读取失败，已停止该店续办')
                continue
            if apply and timeout > 0 and time.monotonic() >= deadline:
                remaining.append(item)
                continue
            try:
                # textSearch 可模糊匹配；索引后只取相同完整 VC，并核对本次生成的条码。
                index = card_index(item.api.cards(vendor_code=vc, max_pages=10))
            except Exception as exc:
                failed_shops.add(sid)
                row.update(status='failed', reason=safe_reason(exc))
                continue
            try:
                plan = _assets(item, index.get(vc))
                ready.append(plan)
                row.update(targetNm=plan.nm, media=plan.urls, stocks=plan.stocks,
                           status='planned', reason='商品及完整规格标识已就绪')
                if apply:
                    entry['targetNm'] = plan.nm
                    entry.pop('wait', None)
                    store.save_state(state)
            except _CardPending as exc:
                row.update(status='planned', reason=str(exc), waitAttempts=attempt + 1)
                if apply:
                    entry['wait'] = {'attempts': attempt + 1, 'reason': str(exc)}
                    store.save_state(state)
                remaining.append(item)
            except Exception as exc:
                row.update(status='failed', reason=safe_reason(exc))
        pending = []
        for item in remaining:
            if item.entry['shopId'] in failed_shops:
                item.row.update(status='blocked', reason='该店标识读取失败，已停止该店续办')
            else:
                pending.append(item)
        log.save()
        if not pending or not apply or timeout == 0 or time.monotonic() >= deadline:
            break
        print(f'[等待建卡] 仍有 {len(pending)} 项未生成完整标识，第 {attempt + 1} 轮；等待后继续查询。', flush=True)
        pause = min(interval, max(0, deadline - time.monotonic()))
        while pause > 0:
            step = min(pause, 30)
            time.sleep(step)
            pause -= step
    for item in pending:
        if apply:
            item.row.update(status='timeout', reason='等待完整商品/规格标识超时；建卡受理记录已保留，可用 --resume 续办')
    usable = []
    for plan in ready:
        if plan.continuation.entry['shopId'] in failed_shops:
            plan.continuation.row.update(status='blocked', reason='该店标识读取失败，已停止该店续办')
        else:
            usable.append(plan)
    if apply:
        _submit(usable, state, log)
    log.save()


def resume(args: Any) -> int:
    """从既有阶段记录续办，不需要源详情、价格或重新生成条码。"""
    if (args.source_shop is not None or args.nm or args.all or args.file or args.price is not None
            or args.dims or args.limit or args.stock != 999):
        raise ValueError('--resume 仅使用 --shops/--vc 选择已有记录，不接受新的源商品、价格或库存')
    vcs = split_values(args.vc)
    _, targets = select_accesses(args.shops)
    log = OperationLog('publish-openapi', args.apply)
    with store.operation_lock(), ExitStack() as stack:
        state = store.load_state()
        continuations = []
        for shop in targets:
            active = []
            for vc in vcs:
                entry = state['items'].get(store.item_key(shop.id, vc))
                if not entry:
                    log.add(shop.id, vc, 'blocked', reason='没有本通道的已受理建卡记录')
                elif entry.get('status') == 'finished':
                    log.add(shop.id, vc, 'skipped', reason='图片与库存均已有受理记录')
                elif not can_continue(entry):
                    log.add(shop.id, vc, 'blocked', reason='建卡或后续写入状态未确认，禁止自动重放')
                else:
                    active.append(entry)
            if not active:
                continue
            try:
                api = stack.enter_context(closing(WBContentAPI(shop.api_key)))
                warehouse = resolve_moscow(api.warehouses())
            except Exception as exc:
                log.add(shop.id, '*', 'failed', reason=safe_reason(exc))
                continue
            for entry in active:
                row = log.add(shop.id, entry['vc'], 'planned', warehouse=warehouse,
                              sourceNm=entry['sourceNm'], cardReceipt=entry.get('receipt'))
                continuations.append(PublishContinuation(entry, row, api, warehouse))
        wait_and_finish(continuations, state, log, timeout=args.wait_timeout,
                        interval=args.wait_interval, apply=args.apply)
    return log.finish()
