"""本地店铺状态。归档仅改变状态，快照和提交记录随店保留。"""
from datetime import datetime
import os
import sqlite3
from time import time

from sellerops.persistence import db
from sellerops.domain.snapshot_values import positive_id


def shop_status(connection=None, *, include_snapshot=False) -> list[dict]:
    if connection is None:
        with db.connect(read_only=include_snapshot) as local:
            return shop_status(local, include_snapshot=include_snapshot)
    snapshot_column = ', snapshot_json IS NOT NULL AS snapshot_present' if include_snapshot else ''
    return [dict(row) for row in connection.execute(f"""
        SELECT shop_id, shop_name, status, last_source, last_synced_at, last_sync_error{snapshot_column}
        FROM shops ORDER BY rowid
    """)]


def _snapshot_max_age_hours() -> int:
    configured = os.getenv('SELLEROPS_SNAPSHOT_MAX_AGE_HOURS', '').strip()
    if configured.isdecimal():
        try:
            if hours := int(configured):
                return hours
        except ValueError:
            pass
    return 24


def _snapshot_age_hours(value: str | None, now: float) -> int | None:
    if isinstance(value, str) and value:
        try:
            seconds = now - datetime.fromisoformat(value.strip()).timestamp()
            if seconds >= 0:
                return int(seconds // 3600)
        except (ValueError, OverflowError, OSError):
            pass
    return None


def snapshot_freshness(rows: list[dict] | None) -> dict:
    threshold = _snapshot_max_age_hours()
    active = [shop for shop in rows or () if shop['status'] == 'active']
    issues = []
    now = time()
    for shop in active:
        stamp = shop['last_synced_at']
        age = _snapshot_age_hours(stamp, now) if shop['snapshot_present'] else None
        if not shop['snapshot_present']:
            status = 'never_synced'
        elif age is None:
            status = 'unknown_age'
        elif age >= threshold:
            status = 'stale'
        else:
            continue
        issues.append({'shop_id': shop['shop_id'], 'shop_name': shop['shop_name'], 'status': status,
                       'last_synced_at': stamp if isinstance(stamp, str) else None, 'age_hours': age})
    return {'status': 'checked' if rows is not None else 'unavailable',
            'max_age_hours': threshold, 'active_shop_count': len(active) if rows is not None else None,
            'shops': issues}


def selected_snapshot_freshness(shop_ids: set[str]) -> dict:
    try:
        rows = shop_status(include_snapshot=True)
    except (ValueError, OSError, sqlite3.Error):
        return snapshot_freshness(None)
    return snapshot_freshness([row for row in rows if row['shop_id'] in shop_ids])


def print_snapshot_reminder(freshness: dict) -> None:
    labels = {'stale': f"的本地快照已满 {freshness['max_age_hours']} 小时",
              'never_synced': '尚无本地商品快照', 'unknown_age': '的本地快照时间无法确认'}
    affected = freshness['shops']
    if not affected:
        return
    counts = []
    for status, label in labels.items():
        stores = [shop for shop in affected if shop['status'] == status]
        if stores:
            samples = '、'.join(f"{shop['shop_name']}（{shop['shop_id']}）" for shop in stores[:3])
            counts.append(f"{len(stores)} 家店铺{label}（{samples}{'等' if len(stores) > 3 else ''}）")
    shop_id = affected[0]['shop_id'] if len(affected) == 1 else '<店铺ID>'
    print(f"[快照提醒] {'，'.join(counts)}。如需最新商品数据，可运行 sync --shop-id {shop_id}。")


def archive_set() -> set[int]:
    return {int(row['shop_id']) for row in shop_status() if row['status'] == 'archived'}


def require_active(shop_id: int) -> None:
    with db.connect() as connection:
        row = connection.execute("SELECT status FROM shops WHERE shop_id=?", (str(shop_id),)).fetchone()
        if row and row['status'] == 'archived':
            raise ValueError(f'店铺 {shop_id} 已归档，请先恢复')


def run_list(args) -> int:
    with db.connect(read_only=True) as connection:
        rows = [dict(row) for row in connection.execute('''
            SELECT s.shop_id, s.shop_name, s.status, s.last_source, s.last_synced_at,
                   s.last_sync_error, s.snapshot_json IS NOT NULL AS snapshot_present,
                   CASE WHEN s.snapshot_json IS NOT NULL THEN coalesce(c.active, 0) END AS active_item_count,
                   CASE WHEN s.snapshot_json IS NOT NULL THEN coalesce(c.trashed, 0) END AS trashed_item_count
              FROM shops AS s LEFT JOIN (
                SELECT shop_id, count(*) FILTER (WHERE is_trashed = 0) AS active,
                       count(*) FILTER (WHERE is_trashed = 1) AS trashed
                  FROM shop_items GROUP BY shop_id
              ) AS c USING (shop_id)
             WHERE ? IS NULL OR s.shop_name = ? ORDER BY s.rowid
        ''', (args.name or None, args.name))]
    for row in rows:
        row['snapshot_present'] = bool(row['snapshot_present'])
    args.output = {'shops': rows, 'snapshot_freshness': snapshot_freshness(rows)}
    print(f'本地店铺 {len(rows)} 家')
    for row in rows:
        status = '在营' if row['status'] == 'active' else '已归档'
        active, trashed = (row['active_item_count'], row['trashed_item_count']) if row['snapshot_present'] else ('未知', '未知')
        print(f"{row['shop_id']} | {row['shop_name']} | {status} | 来源 {row['last_source'] or '未知'} | "
              f"同步 {row['last_synced_at'] or '未同步'} | 快照在架 {active} | 快照回收站 {trashed}")
    print_snapshot_reminder(args.output['snapshot_freshness'])
    return 0


def run_status(args) -> int:
    sid = positive_id(args.shop_id)
    if sid is None:
        raise ValueError('店铺 ID 必须为正整数')
    target = 'archived' if args.action == 'archive' else 'active'
    with db.connect(write=True) as connection:
        row = connection.execute("SELECT shop_name FROM shops WHERE shop_id=?", (str(sid),)).fetchone()
        if row is None:
            raise ValueError(f'本地库没有店铺 {sid}')
        connection.execute("UPDATE shops SET status=? WHERE shop_id=?", (target, str(sid)))
    args.output = {'shop_id': sid, 'shop_name': row['shop_name'], 'status': target}
    print(f"店铺 {sid} {row['shop_name']} " + ('已归档' if target == 'archived' else '已恢复'))
    return 0
