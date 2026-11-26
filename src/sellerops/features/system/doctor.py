"""WorkBuddy 的本地快速体检及显式只读鉴权。"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

from sellerops.features.catalog.shops import print_snapshot_reminder, shop_status, snapshot_freshness
from sellerops.features.system import repository_update
from sellerops.integrations.sessions import browser
from sellerops.persistence import db
from sellerops.persistence.price_book import PRICE_BOOK_EXAMPLE, inspect_price_book as check_book
from sellerops.settings import credentials, paths


_BOOK_ISSUES = {
    'missing_product_identity': '卖家 SKU 和中文名均为空',
    'duplicate_seller_sku': '卖家 SKU 重复',
    'ambiguous_product_name': '无 SKU 时中文名无法唯一识别',
    'missing_price': '最低售价为空', 'invalid_price': '最低售价不是有效正数',
    'price_formula_without_cache': '最低售价公式无缓存，待核实',
    'invalid_dimensions': '尺寸格式或数值无效',
    'dimensions_formula_without_cache': '尺寸公式无缓存，待核实',
    'invalid_prefix': 'vendorCode 前缀须为四位英文字母',
    'duplicate_prefix': 'vendorCode 前缀重复，无法自动归属',
    'missing_shipping_mode': '发货方式为空', 'invalid_shipping_mode': '发货方式无效',
    'wb_shipping_with_mabang_sku': 'WB 发货填了马帮库存 SKU',
    'skip_shipping_with_mabang_sku': '发货方式为「不处理」却填了马帮库存 SKU',
    'mabang_shipping_without_stock_sku': '马帮发货漏填库存 SKU',
}
_BOOK_ACTIONS = {
    'missing': (f'首次同步前，将 {PRICE_BOOK_EXAMPLE} 复制到本次数据目录并改名为 商品价格表.xlsx。'
                '填写唯一四位英文字母前缀可启用自动归属'),
    'missing_sheet': '核对本次数据目录中 商品价格表.xlsx 的工作表名是否为 Sheet1',
    'invalid': '按体检提示修正本次数据目录中 商品价格表.xlsx 的 Sheet1 表头',
    'unreadable': '核对 商品价格表.xlsx 的文件格式和读取权限。如被 Excel 占用，请保存并关闭后再检查',
}
_PLATFORM_NAMES = {'bcs': 'BCS', 'wb': 'WB', 'mabang': '马帮', 'feishu': '飞书'}
_LEGACY_TEMPLATE_WARNINGS = {'legacy_wb_template_shop', 'legacy_mabang_template_shop_map'}
_CREDENTIAL_WARNINGS = {
    'legacy_wb_template_shop': '旧模板的示例店铺，备份后核对并移除该条目',
    'legacy_mabang_template_shop_map': '旧模板的示例映射，备份后核对并移除该条目',
    'duplicate_wb_supplier_uuid': 'WB 店铺 UUID 跨店重复，请核对这些店铺的绑定',
    'duplicate_wb_seller_lk': 'WB seller-lk 跨店重复，请核对这些店铺的凭证',
}
_ONLINE_LABELS = {'verified': '通过', 'authentication_failed': '鉴权失败',
                  'missing': '缺少会话或配置', 'unavailable': '检查不可用'}


def active_pending(connection) -> int:
    return connection.execute("""
        SELECT count(DISTINCT i.vc) FROM shop_items i JOIN shops s USING (shop_id)
          LEFT JOIN vc_map m ON m.vc = i.vc
         WHERE s.status = 'active' AND i.is_trashed = 0 AND coalesce(m.status, 'pending') = 'pending'
    """).fetchone()[0]


def _print_platforms(platforms: dict) -> None:
    def configured(value: bool) -> str:
        return '已配' if value else '未配'

    def login_summary(login: dict, username: bool, *, browser_login: bool = True) -> str:
        password = '已填' if login['password_configured'] else '未填'
        refresh = '关闭'
        if login['auto_refresh']:
            refresh = '开启' if login['auto_refresh_enabled'] else '开启（缺少续期条件）'
        browser_hint = '，可使用浏览器登录' if browser_login and username and not (
            login['password_configured'] or login['session_configured']) else ''
        return (f"账号{configured(username)}、密码{password}、会话{configured(login['session_configured'])}、"
                f"自动续期{refresh}{browser_hint}")

    bcs, wb, mabang, feishu = (platforms[name] for name in _PLATFORM_NAMES)
    bcs_default = bcs['endpoint'] == 'default'
    endpoint = '默认' if bcs_default else '自定义'
    print(f"[BCS 登录] {login_summary(bcs, bcs['login_configured'], browser_login=bcs_default)} | {endpoint}端点")
    print(f"[WB] 默认仓库关键词{configured(wb['warehouse_keyword_configured'])}")
    login = mabang['login']
    print(f"[马帮登录] {login_summary(login, login['username_configured'])} | API {configured(mabang['api_configured'])}")
    warehouse_label = {'missing': '未配，请填关键词或已核实 ID', 'invalid': '配置有误',
                       'keyword': '按关键词解析', 'id': '使用已核实 ID',
                       'keyword_and_id': '关键词与 ID 交叉核对'}
    fulfillment = mabang['fulfillment']
    print(f"[马帮订单履约] 店铺映射 {fulfillment['mapped_shops']} 项、"
          f"库存仓{warehouse_label[fulfillment['warehouse_mode']]}、交运渠道{configured(fulfillment['handover_configured'])}")
    print(f"[飞书] 表入口{configured(feishu['configured'])}")


def _print_online(output: dict) -> None:
    online = output['online']
    if online is None:
        return
    if 'credentials' in online:
        print('[在线鉴权] 未运行，凭证文件格式无效或无法读取')
        return
    if not online:
        reason = '缺少 requests 依赖' if not output['dependencies']['requests'] else '缺少凭证文件'
        print(f'[在线鉴权] 未运行，{reason}')
        return
    for name, label in _PLATFORM_NAMES.items():
        result = online.get(name)
        if result is None:
            continue
        if isinstance(result, dict):
            summary = '、'.join(f'店铺 {shop_id} {_ONLINE_LABELS[status]}' for shop_id, status in result.items())
        else:
            summary = _ONLINE_LABELS[result]
        if name == 'bcs' and result == 'unavailable' and output['platforms']['bcs']['endpoint'] == 'custom':
            summary += '（自定义端点未执行探针）'
        print(f'[在线鉴权] {label} {summary}')


def _print_summary(output: dict) -> None:
    def count(key: str):
        value = output['shop_counts'][key]
        return '未知' if value is None else value

    cred = output['credentials']
    settings = {'ready': 'JSON 和基础结构正常', 'missing': '文件缺失', 'unreadable': '文件不可读',
                'invalid_json': 'JSON 无效', 'invalid_structure': '结构有误',
                'unconfigured': '结构正常，BCS、WB、马帮和飞书均未填写有效配置'}
    print(f"[配置] credentials.json {settings[cred['status']]}")
    for issue in cred['issues'][:10]:
        reason = {'expected_object': '须为对象', 'expected_string': '须为字符串', 'expected_array': '须为列表',
                  'invalid_shop_id': '须为正整数', 'duplicate_shop_id': '店铺 ID 重复',
                  'invalid_warehouse_id': '须为正整数、十进制数字字符串或 null'}[issue['code']]
        print(f"  {issue['path']} {reason}")
    if len(cred['issues']) > 10:
        print(f"  另有 {len(cred['issues']) - 10} 项，见 JSON 结果。")
    for warning in cred['warnings'][:10]:
        shop_ids = warning.get('shop_ids')
        scope = f"（店铺 ID {'、'.join(map(str, shop_ids))}）" if shop_ids else ''
        print(f"  {warning['path']} {_CREDENTIAL_WARNINGS[warning['code']]}{scope}")
    if len(cred['warnings']) > 10:
        print(f"  另有 {len(cred['warnings']) - 10} 条告警，见 JSON 结果。")
    print(f"[店铺] 凭证已登记 {count('credential_configured')} 家，三件套齐全 {count('credential_session_complete')} 家"
          f" | 本地有快照 {count('local_synced')} 家（在营 {count('local_synced_active')}，"
          f"归档 {count('local_synced_archived')}）")
    db_label = {'ready': '就绪', 'legacy': '有待迁移的旧数据', 'empty': '尚无库', 'unreadable': '不可读'}
    print(f"[本地库] {db_label[output['database']]}")

    _print_platforms(output['platforms'])
    _print_online(output)

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


def _connection_hint(name: str, output: dict) -> str:
    platform = output['platforms'][name]
    if name == 'feishu':
        return '核对本机 lark-cli 的登录和飞书表权限'
    if name == 'bcs' and platform['endpoint'] == 'custom':
        return '核对 BCS 自定义端点及本机保存的会话配置'
    browser_needed = name == 'wb'
    if name in ('bcs', 'mabang'):
        if not platform['login_configured']:
            return f"先在 credentials.json 填写 {_PLATFORM_NAMES[name]} 登录账号"
        login = platform if name == 'bcs' else platform['login']
        browser_needed = not login['password_configured']
    if browser_needed:
        if output['browser']['status'] != 'ready':
            return f'需要浏览器时，先运行 login setup，再运行 login {name}'
        return f'按需运行 login {name}，在浏览器完成登录'
    return f'按需运行 login {name} 连接平台'


def _credential_next_action(output: dict) -> str:
    cred = output['credentials']
    status = cred['status']
    if status == 'missing':
        return '将 examples/credentials.example.json 复制到本次数据目录并改名为 credentials.json，再按需填写平台配置'
    if status == 'unreadable':
        return '核对 credentials.json 的文件位置和读取权限'
    if status in ('invalid_json', 'invalid_structure'):
        return '按体检提示修正 credentials.json 的 JSON 格式、结构或字段类型'
    warning_codes = {warning['code'] for warning in cred['warnings']}
    old_template = '备份 credentials.json 后核对并移除旧模板的示例条目，再按需填写平台配置'
    counts = output['shop_counts']
    if warning_codes & _LEGACY_TEMPLATE_WARNINGS and (status == 'unconfigured' or counts['local_synced'] == 0):
        return old_template
    if status == 'unconfigured':
        return '在 credentials.json 中按需填写待连接平台的配置，再运行 doctor 核对'

    online = output['online'] or {}
    for name in _PLATFORM_NAMES:
        result = online.get(name)
        if result == 'authentication_failed' or (isinstance(result, dict) and 'authentication_failed' in result.values()):
            if name == 'wb' and 'legacy_wb_template_shop' in warning_codes:
                return old_template
            return _connection_hint(name, output)
    for name in ('bcs', 'mabang'):
        platform = output['platforms'][name]
        if platform['configured'] and not platform['session_configured']:
            return _connection_hint(name, output)
    if output['platforms']['wb']['configured'] and counts['credential_session_complete'] == 0:
        return old_template if 'legacy_wb_template_shop' in warning_codes else _connection_hint('wb', output)
    return ''


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
    synced = [shop for shop in shops if shop['snapshot_present']]
    settings['shop_counts'].update(local_synced=len(synced) if checked_db else None,
                                  local_synced_active=sum(s['status'] == 'active' for s in synced) if checked_db else None,
                                  local_synced_archived=sum(s['status'] == 'archived' for s in synced) if checked_db else None)
    online = None
    if args.check_online:
        try:
            status = settings['credentials']['status']
            if not deps['requests'] or status == 'missing':
                online = {}
            elif status in ('invalid_json', 'unreadable'):
                online = {'credentials': 'unreadable'}
            else:
                online = online_checks()
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
    else:
        args.next_action = _credential_next_action(args.output)
        if not args.next_action:
            if book['status'] in _BOOK_ACTIONS:
                args.next_action = _BOOK_ACTIONS[book['status']]
            elif sqlite_status == 'legacy':
                args.next_action = '备份后运行 migrate-legacy'
            elif sqlite_status == 'unreadable':
                args.next_action = '核对本地 sellerops.db'
            elif not synced and settings['platforms']['bcs']['session_configured']:
                args.next_action = '按需运行 sync 获取商品快照'
            elif pending:
                args.next_action = '用 map workbench 生成带图待认领工作台'
    print(args.summary)
    _print_summary(args.output)
    print_snapshot_reminder(freshness)
    repository_update.print_reminder(update)
    if book['status'] == 'missing' and args.next_action != _BOOK_ACTIONS['missing']:
        print(f"[商品价格表] {_BOOK_ACTIONS['missing']}")
    if args.next_action:
        print(args.next_action)
    return int(not all(deps.values()) or sqlite_status == 'unreadable')
