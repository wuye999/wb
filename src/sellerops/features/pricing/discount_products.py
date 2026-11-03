"""定向折扣操作的字段准备；折扣写入不依赖规格、库存或现价。"""
from typing import Callable

from sellerops.domain.models import DiscountPlan
from sellerops.domain.snapshot_values import positive_id
from sellerops.features.listings.product_data import ProductDataResolver, valid_discount
from sellerops.features.listings.skip_report import SkipTracker


def snapshot_discount_items(sid: int, rows: dict[str, dict], plan: DiscountPlan, resolve_cn: Callable,
                            matches_filters: Callable, disc_matched: Callable, tracker: SkipTracker
                            ) -> tuple[list[dict], list[dict]]:
    """缺少阈值判断所需折扣时定向补齐；不限折扣时未知旧值不妨碍提交。"""
    items, blocked = [], []
    pool = list(dict.fromkeys(plan.target_vcs)) if plan.target_vcs else list(rows)
    needs_discount = not plan.is_all and (plan.threshold >= 0 or plan.below >= 0)
    with ProductDataResolver() as resolver:
        for vc in pool:
            row = rows.get(vc)
            if not row:
                reason = '本店快照无此商品（未在架或未同步）'
                tracker.add(sid, vc, reason)
                blocked.append({'vc': vc, 'reason': reason})
                continue
            title = row.get('shopGoodsName') or row.get('title') or ''
            cn = resolve_cn(vc, title)
            if not matches_filters(vc, cn, title, plan):
                continue
            prepared = resolver.ensure(sid, vc, row, discount=needs_discount)
            if prepared.reason:
                tracker.add(sid, vc, prepared.reason)
                blocked.append({'vc': vc, 'cn': cn, 'nmID': row.get('nmId'), 'reason': prepared.reason})
                continue
            row = prepared.row
            disc = row.get('discount') if valid_discount(row.get('discount')) else None
            if needs_discount and not disc_matched(disc, plan):
                continue
            if disc is not None and disc == plan.target_discount:
                continue
            items.append({'nmID': positive_id(row['nmId']), 'vendorCode': vc, 'cn': cn,
                          'old_discount': disc, 'currencyIsoCode': row.get('currencyIsoCode') or 'CNY',
                          'price': None, 'title': title})
            if plan.limit and len(items) >= plan.limit:
                break
    return items, blocked
