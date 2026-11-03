"""官方商品写入的范围选择、预览与提交日志。"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sellerops.core.files import atomic_dump_json
from sellerops.features.listings.open_api_payloads import positive_id
from sellerops.integrations.bcs import client as bcs
from sellerops.settings import paths


def split_values(raw: str) -> list[str]:
    values = [v.strip() for v in raw.split(',')]
    if not values or any(not v for v in values) or len(set(values)) != len(values):
        raise ValueError('逗号分隔参数含空值或重复值')
    return values


def select_accesses(raw: str, source_id: int | None = None) -> tuple[Any, list[Any]]:
    shops = bcs.client.fetch_open_api_shops()
    by_id = {shop.id: shop for shop in shops}
    if source_id is not None and source_id not in by_id:
        raise ValueError('源店不在 BCS 店铺列表中')
    ids = [positive_id(v) for v in split_values(raw)] if raw else sorted(set(by_id) - {source_id})
    if not ids or len(set(ids)) != len(ids) or any(sid not in by_id for sid in ids):
        raise ValueError('目标店铺为空、重复或不在 BCS 店铺列表中')
    if source_id in ids:
        raise ValueError('源店不能同时作为目标店')
    return by_id.get(source_id), [by_id[sid] for sid in ids]


def select_cards(index: dict[str, dict[str, Any]], raw_vcs: str, raw_nms: str,
                 all_cards: bool, limit: int = 0) -> list[dict[str, Any]]:
    if limit < 0 or sum((bool(raw_vcs), bool(raw_nms), bool(all_cards))) != 1:
        raise ValueError('须且只能用 --vc、--nm 或 --all 选择源商品；limit 不能为负')
    if raw_vcs:
        vcs = split_values(raw_vcs)
        if any(vc not in index for vc in vcs):
            raise ValueError('指定完整 VC 不在源店非回收站商品中')
        cards = [index[vc] for vc in vcs]
    elif raw_nms:
        nms = [positive_id(v) for v in split_values(raw_nms)]
        by_nm = {positive_id(card['nmID']): card for card in index.values()}
        if any(nm not in by_nm for nm in nms):
            raise ValueError('指定商品码不在源店非回收站商品中')
        cards = [by_nm[nm] for nm in nms]
    else:
        cards = list(index.values())
    return cards[:limit] if limit else cards


def safe_reason(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    return getattr(exc, 'public_reason', None) or f'{type(exc).__name__}（不输出原始响应；提交异常不自动重放）'


@dataclass
class CardOperation:
    shop_id: int
    vc: str
    source_nm: int
    target_nm: int | None = None
    card: dict[str, Any] | None = None
    media: list[str] | None = None


class OperationLog:
    def __init__(self, command: str, apply: bool) -> None:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.path = Path(paths.LOG_DIR) / f'{command}_{stamp}_{uuid4().hex[:8]}.json'
        self.document: dict[str, Any] = {'command': command, 'apply': apply,
                                         'createdAt': datetime.now().isoformat(), 'items': []}

    def add(self, shop_id: int, vc: str, status: str, **detail: Any) -> dict[str, Any]:
        row = dict(shopId=shop_id, vc=vc, status=status, **detail)
        self.document['items'].append(row)
        return row

    def save(self) -> None:
        atomic_dump_json(self.path, self.document)

    def finish(self) -> int:
        self.save()
        rows = self.document['items']
        failed = sum(row['status'] in {'failed', 'unknown', 'partial', 'blocked', 'timeout'} for row in rows)
        accepted = sum(row['status'] == 'accepted' for row in rows)
        planned = sum(row['status'] == 'planned' for row in rows)
        skipped = sum(row['status'] == 'skipped' for row in rows)
        for row in rows:
            print(f"[店{row['shopId']}] {row['vc']} {row['status']} {row.get('reason', '')}")
        print(f'[汇总] 已受理 {accepted} | 待提交 {planned} | 跳过 {skipped} | 失败/未完成 {failed}')
        print(f'[结果] {self.path}')
        return 1 if failed else 0
