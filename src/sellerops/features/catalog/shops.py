"""本地店铺状态。归档仅改变状态，快照和提交记录随店保留。"""
from sellerops.persistence import db
from sellerops.domain.snapshot_values import positive_id


def shop_status(connection=None) -> list[dict]:
    if connection is None:
        with db.connect() as local:
            return shop_status(local)
    return [dict(row) for row in connection.execute("""
        SELECT shop_id, shop_name, status, last_source, last_synced_at, last_sync_error
        FROM shops ORDER BY rowid
    """)]


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
    args.output = {'shops': rows}
    print(f'本地店铺 {len(rows)} 家')
    for row in rows:
        status = '在营' if row['status'] == 'active' else '已归档'
        active, trashed = (row['active_item_count'], row['trashed_item_count']) if row['snapshot_present'] else ('未知', '未知')
        print(f"{row['shop_id']} | {row['shop_name']} | {status} | 来源 {row['last_source'] or '未知'} | "
              f"同步 {row['last_synced_at'] or '未同步'} | 快照在架 {active} | 快照回收站 {trashed}")
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
