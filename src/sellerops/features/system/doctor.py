"""WorkBuddy 的本地快速体检及显式只读鉴权。"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

from sellerops.features.catalog.shops import shop_status
from sellerops.persistence import db
from sellerops.settings import paths


def active_pending(connection) -> int:
    entries = db.vc_entries(connection)
    return sum(db.resolve_vc(row[0], entries)['status'] == 'pending' for row in connection.execute("""
        SELECT DISTINCT i.vc FROM shop_items i JOIN shops s USING (shop_id)
         WHERE s.status = 'active' AND i.is_trashed = 0 AND i.vc IS NOT NULL
    """))


def check_book(path: Path) -> dict:
    from openpyxl import load_workbook
    from sellerops.persistence.price_book import BOSS_COLUMNS

    required = [BOSS_COLUMNS[k] for k in ('sku', 'cn', 'dp')]
    if not path.is_file():
        return {'status': 'missing', 'missing_headers': required}
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if 'Sheet1' not in workbook:
            return {'status': 'missing_sheet', 'missing_headers': required}
        header = next(workbook['Sheet1'].values, ())
        headers = [str(value or '').strip() for value in header]
        missing = [item for item in required if item not in headers]
        duplicate = [item for item in required if headers.count(item) > 1]
        return {'status': 'invalid' if missing or duplicate else 'ready',
                'missing_headers': missing, 'duplicate_headers': duplicate}
    finally:
        workbook.close()


def online_checks() -> dict:
    from sellerops.core.errors import AuthenticationError
    from sellerops.features.system.online_auth import check_bcs, check_feishu, check_mabang, check_wb
    from sellerops.settings.credentials import Credentials

    cred = Credentials(paths.current.credentials_json)
    checks = {}
    for name, probe in (('bcs', check_bcs), ('wb', check_wb), ('mabang', check_mabang), ('feishu', check_feishu)):
        try:
            checks[name] = probe(cred)
        except AuthenticationError:
            checks[name] = 'authentication_failed'
        except Exception:
            checks[name] = 'unavailable'
    return checks


def run(args) -> int:
    deps = {name: importlib.util.find_spec(name) is not None for name in ('requests', 'openpyxl')}
    data = paths.current
    present = Path(data.credentials_json).is_file()
    try:
        book = check_book(Path(data.price_book_xlsx)) if deps['openpyxl'] else {'status': 'not_checked'}
    except Exception:
        book = {'status': 'unreadable'}
    sqlite_status = 'ready' if Path(data.database).is_file() else 'legacy' if db.legacy_exists() else 'empty'
    shops, pending = [], None
    if sqlite_status in {'ready', 'empty'}:
        try:
            shops = shop_status()
            with db.connect() as connection:
                pending = active_pending(connection)
        except (ValueError, sqlite3.Error):
            sqlite_status = 'unreadable'
    online = None
    if args.check_online:
        try:
            online = online_checks() if present and deps['requests'] else {}
        except Exception:
            online = {'credentials': 'unreadable'}
    args.output = {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'dependencies': deps,
                   'credentials_file_exists': present, 'price_book': book, 'database': sqlite_status,
                   'shops': shops, 'pending_vcs': pending, 'online_checked': args.check_online, 'online': online}
    args.summary = '本机体检完成' if not args.check_online else '本机体检及只读鉴权完成'
    if not all(deps.values()):
        args.next_action = '安装项目依赖后再运行 doctor'
    elif not present:
        args.next_action = '按需运行 login 连接平台'
    elif book['status'] != 'ready':
        args.next_action = '核对 data/商品价格表.xlsx 的 Sheet1 和必填表头'
    elif sqlite_status == 'legacy':
        args.next_action = '备份后运行 migrate-legacy'
    elif sqlite_status == 'unreadable':
        args.next_action = '核对本地 sellerops.db'
    elif not shops or not any(shop['last_synced_at'] for shop in shops):
        args.next_action = '按需运行 sync 获取商品快照'
    elif pending:
        args.next_action = '用 map candidates 查看待认领商品'
    print(args.summary)
    if args.next_action:
        print(args.next_action)
    return int(not all(deps.values()) or sqlite_status == 'unreadable')
