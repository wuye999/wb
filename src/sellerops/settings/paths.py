"""Project-local data locations, independent of the process working directory."""
import os
import re
from pathlib import Path
from sellerops.domain.vendor_codes import VC_PREFIX_RE


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAIN_SHOP = None
DEFAULT_SHOP_ID = MAIN_SHOP
DISCOUNT_THRESHOLD_DEF = 50
DISCOUNT_TARGET_DEF = 50
DEFAULT_WAREHOUSE_NAME = "莫斯科"
PRODUCTS_DIR: str
SHOPS_DIR: str
SHOPS_ARCHIVE_DIR: str


def configure(data_dir: str | Path | None = None) -> None:
    """Select the data root before loading command implementations; does not create it."""
    root = Path(data_dir or os.environ.get('SELLEROPS_DATA_DIR') or PROJECT_ROOT / 'data').expanduser().resolve()
    globals()['DATA_DIR'] = str(root)
    directories = {'PRODUCTS_DIR': 'products', 'STATE_DIR': 'state', 'HAR_DIR': 'har',
                   'WORKBENCH_DIR': 'workbench', 'LOG_DIR': 'logs', 'SHOPS_DIR': 'shops',
                   'SHOPS_ARCHIVE_DIR': 'shops/_archive'}
    files = {'CREDENTIALS_JSON': 'credentials.json', 'AI_TEST_QA': 'ai_test_qa.json',
             'PRICE_BOOK_XLSX': '商品价格表.xlsx', 'MAPPING_XLSX': '价格映射表.xlsx',
             'STATUS_JSON': 'state/多店铺同步状态.json', 'AUTO_ADD_JSON': 'state/多店铺自动新增清单.json',
             'VC_KNOWN_JSON': 'state/vc_known.json', 'VC_OVERRIDE_JSON': 'state/vc_override.json',
             'VC_EXCLUDED_JSON': 'state/vc_excluded.json', 'RESULT_CSV': 'logs/ops_result.csv',
             'OUT_MAPPING_HTML': 'workbench/价格映射核对工作台.html',
             'OUT_REVIEW_HTML': 'workbench/多店铺待审核.html',
             'OUT_MAPPING_CHECK_HTML': 'workbench/映射表核查工作台.html',
             'OUT_MISMATCH_HTML': 'workbench/货不对板筛查工作台.html'}
    for name, relative in {**directories, **files}.items():
        globals()[name] = str(root / relative)


def shop_json_path(shop_id: int | str) -> str:
    return str(Path(PRODUCTS_DIR) / f'shop{int(shop_id)}_products_all.json')


def shop_mapping_xlsx(shop_id: int | str, shop_name: str | None = None) -> str:
    sid = int(shop_id)
    if shop_name:
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', shop_name).rstrip(' .')
        return str(Path(SHOPS_DIR) / f'shop_{sid}_{safe_name}.xlsx')
    matches = sorted(Path(SHOPS_DIR).glob(f'shop_{sid}_*.xlsx'))
    return str(matches[0] if matches else Path(SHOPS_DIR) / f'shop_{sid}.xlsx')


def is_shop_archived(shop_id: int | str) -> bool:
    sid = int(shop_id)
    archive = Path(SHOPS_ARCHIVE_DIR)
    return (archive / f'shop_{sid}.xlsx').exists() or any(archive.glob(f'shop_{sid}_*.xlsx'))


configure()
