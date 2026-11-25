"""WorkBuddy 的本地快速体检及显式只读鉴权。"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

from sellerops.features.catalog.shops import print_snapshot_reminder, shop_status, snapshot_freshness
from sellerops.features.system import repository_update
from sellerops.integrations.sessions import browser
from sellerops.persistence import db
from sellerops.persistence.price_book import inspect_price_book as check_book
from sellerops.settings import credentials, paths


_BOOK_ISSUES = {
    'missing_product_identity': '卖家 SKU 和中文名均为空',
    'duplicate_seller_sku': '卖家 SKU 重复',
    'ambiguous_product_name': '无 SKU 时中文名无法唯一识别',
    'missing_price': '最低售价为空', 'invalid_price': '最低售价不是有效正数',
    'price_formula_without_cache': '最低售价公式无缓存，待核实',
    'missing_shipping_mode': '发货方式为空', 'invalid_shipping_mode': '发货方式无效',
    'wb_shipping_with_mabang_sku': 'WB 发货填了马帮库存 SKU',
    'mabang_shipping_without_stock_sku': '马帮发货漏填库存 SKU',
}


def active_pending(connection) -> int:
    entries = db.vc_entries(connection)
    return sum(db.resolve_vc(row[0], entries)['status'] == 'pending' for row in connection.execute("""
        SELECT DISTINCT i.vc FROM shop_items i JOIN shops s USING (shop_id)
         WHERE s.status = 'active' AND i.is_trashed = 0 AND i.vc IS NOT NULL
    """))


def _print_summary(output: dict) -> None:
    def count(key: str):
        value = output['shop_counts'][key]
        return '未知' if value is None else value

    def configured(value: bool) -> str:
        return '已配' if value else '未配'

    cred = output['credentials']
    settings = {'ready': 'JSON 和基础结构正常', 'missing': '文件缺失', 'unreadable': '文件不可读',
                'invalid_json': 'JSON 无效', 'invalid_structure': '结构有误'}
    print(f"[配置] credentials.json {settings[cred['status']]}")
    for issue in cred['issues'][:10]:
        reason = {'expected_object': '须为对象', 'expected_string': '须为字符串', 'expected_array': '须为列表',
                  'invalid_shop_id': '须为正整数', 'duplicate_shop_id': '店铺 ID 重复'}[issue['code']]
        print(f"  {issue['path']} {reason}")
    if len(cred['issues']) > 10:
        print(f"  另有 {len(cred['issues']) - 10} 项，见 JSON 结果。")
    print(f"[店铺] 凭证已登记 {count('credential_configured')} 家，三件套齐全 {count('credential_session_complete')} 家"
          f" | 本地已同步 {count('local_synced')} 家（在营 {count('local_synced_active')}，"
          f"归档 {count('local_synced_archived')}）")
    db_label = {'ready': '就绪', 'legacy': '有待迁移的旧数据', 'empty': '尚无库', 'unreadable': '不可读'}
    print(f"[本地库] {db_label[output['database']]}")

    platforms = output['platforms']
    bcs, wb, mabang, feishu = (platforms[name] for name in ('bcs', 'wb', 'mabang', 'feishu'))
    endpoint = '默认' if bcs['endpoint'] == 'default' else '自定义'
    print(f"[平台] BCS 登录账号{configured(bcs['login_configured'])}、会话{configured(bcs['session_configured'])}、{endpoint}端点"
          f" | WB 仓库关键词{configured(wb['warehouse_keyword_configured'])}"
          f" | 马帮账号{configured(mabang['login_configured'])}、会话{configured(mabang['session_configured'])}、"
          f"API {configured(mabang['api_configured'])}、店铺映射 {mabang['mapped_shops']} 项"
          f" | 飞书表入口{configured(feishu['configured'])}")

    runtime = output['browser']
    chromium = runtime['chromium']
    version = runtime['playwright_version'] or '无可用版本'
    build = chromium['installed_build'] or '未找到'
    browser_label = {'ready': '安装文件和校验回执齐全', 'package_missing': '缺少依赖包',
                     'package_mismatch': '依赖包版本不符', 'manifest_unreadable': '依赖包清单不可读',
                     'build_mismatch': '依赖包的 Chromium 构建或版本不符',
                     'installed_build_mismatch': '本机安装的 Chromium 构建不符',
                     'chromium_missing': '安装文件不完整', 'installed_unverified': '安装待核验'}
    print(f"[浏览器] {browser_label[runtime['status']]} | Playwright {version}（需 {runtime['expected_playwright_version']}）"
          f" | Chromium 构建 {build}（需 {chromium['expected_build']}）")

    book = output['price_book']
    book_label = {'ready': '表头可用', 'invalid': '表头有误', 'missing': '文件缺失', 'missing_sheet': '缺少 Sheet1',
                  'unreadable': '文件不可读', 'not_checked': '缺少读取依赖'}
    row_count = book.get('data_rows')
    rows = '行数未知' if row_count is None else f'{row_count} 行数据'
    print(f"[商品价格表] {book_label[book['status']]} | {rows}")
    for key, label in (('missing_headers', '缺少表头'), ('duplicate_headers', '必需表头重复'),
                       ('duplicate_optional_headers', '可选表头重复')):
        if book.get(key):
            print(f"  {label} {'、'.join(book[key])}")
    for code, total in book.get('issue_counts', {}).items():
        sample = '、'.join(map(str, book['row_samples'][code]))
        print(f"  {_BOOK_ISSUES[code]} {total} 行（行号示例 {sample}）")


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
    settings = credentials.inspect_credentials(data.credentials_json)
    runtime = browser.installation_status()
    try:
        book = check_book(Path(data.price_book_xlsx)) if deps['openpyxl'] else {'status': 'not_checked'}
    except Exception:
        book = {'status': 'unreadable'}
    book.setdefault('data_rows', None)
    book.setdefault('issue_counts', {})
    book.setdefault('row_samples', {})
    book.setdefault('skipped_checks', [])
    sqlite_status = 'ready' if Path(data.database).is_file() else 'legacy' if db.legacy_exists() else 'empty'
    shops, pending = [], None
    if sqlite_status in {'ready', 'empty'}:
        try:
            with db.connect(read_only=True) as connection:
                shops = shop_status(connection, include_snapshot=True)
                pending = active_pending(connection)
        except (ValueError, sqlite3.Error):
            sqlite_status = 'unreadable'
    checked_db = sqlite_status in {'ready', 'empty'}
    synced = [shop for shop in shops if shop['last_synced_at']]
    settings['shop_counts'].update(local_synced=len(synced) if checked_db else None,
                                  local_synced_active=sum(s['status'] == 'active' for s in synced) if checked_db else None,
                                  local_synced_archived=sum(s['status'] == 'archived' for s in synced) if checked_db else None)
    online = None
    if args.check_online:
        try:
            online = online_checks() if present and deps['requests'] else {}
        except Exception:
            online = {'credentials': 'unreadable'}
    freshness = snapshot_freshness(shops if checked_db else None)
    for shop in shops:
        shop.pop('snapshot_present', None)
    update = repository_update.check(force=getattr(args, 'check_updates', False))
    args.output = {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'dependencies': deps,
                   'snapshot_freshness': freshness, 'repository_update': update,
                   'credentials_file_exists': present, 'price_book': book, 'database': sqlite_status,
                   'shops': shops, 'pending_vcs': pending, 'online_checked': args.check_online, 'online': online,
                   'browser': runtime, **settings}
    args.summary = '本机体检完成' if not args.check_online else '本机体检及只读鉴权完成'
    if not all(deps.values()):
        args.next_action = '安装项目依赖后再运行 doctor'
    elif not present:
        args.next_action = '按需运行 login 连接平台'
    elif settings['credentials']['status'] != 'ready':
        args.next_action = '核对 credentials.json 的结构和字段类型'
    elif book['status'] != 'ready':
        args.next_action = '核对 data/商品价格表.xlsx 的 Sheet1 和必填表头'
    elif sqlite_status == 'legacy':
        args.next_action = '备份后运行 migrate-legacy'
    elif sqlite_status == 'unreadable':
        args.next_action = '核对本地 sellerops.db'
    elif not shops or not any(shop['last_synced_at'] for shop in shops):
        args.next_action = '按需运行 sync 获取商品快照'
    elif pending:
        args.next_action = '用 map workbench 生成带图待认领工作台'
    print(args.summary)
    _print_summary(args.output)
    print_snapshot_reminder(freshness)
    repository_update.print_reminder(update)
    if args.next_action:
        print(args.next_action)
    return int(not all(deps.values()) or sqlite_status == 'unreadable')
