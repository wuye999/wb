"""Project-local data locations, independent of the process working directory."""
from datetime import date, datetime
import os
import re
from pathlib import Path
from sellerops.domain.vendor_codes import VC_PREFIX_RE


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BROWSER_RUNTIME_DIR = PROJECT_ROOT / '.runtime' / 'browsers'
MAIN_SHOP = None
DISCOUNT_THRESHOLD_DEF = 50
DISCOUNT_TARGET_DEF = 50


class DataPaths:
    """本次运行的数据文件，全部从一个根目录派生。"""

    def __init__(self, root: Path):
        self.data_dir = str(root)
        self.database = str(root / 'sellerops.db')
        self.exports_dir = root.parent / 'exports'
        self.products_dir = str(root / 'products')
        self.state_dir = str(root / 'state')
        self.workbench_dir = str(self.exports_dir / 'workbench')
        self.log_dir = str(root / 'logs')
        self.shops_dir = str(root / 'shops')
        self.shops_archive_dir = str(root / 'shops/_archive')
        self.browser_profiles_dir = str(root / 'browser_profiles')
        self.credentials_json = str(root / 'credentials.json')
        self.ai_test_qa = str(root / 'ai_test_qa.json')
        self.price_book_xlsx = str(root / '商品价格表.xlsx')
        self.mapping_xlsx = str(root / '价格映射表.xlsx')
        self.status_json = str(root / 'state/多店铺同步状态.json')
        self.vc_known_json = str(root / 'state/vc_known.json')
        self.vc_override_json = str(root / 'state/vc_override.json')
        self.vc_excluded_json = str(root / 'state/vc_excluded.json')
        self.out_mapping_html = str(Path(self.workbench_dir) / '价格映射核对工作台.html')
        self.out_mismatch_html = str(Path(self.workbench_dir) / '货不对板筛查工作台.html')
        self.records_json = str(root / 'state/复制上架记录.json')
        self.questions_replied = str(root / 'state/questions_replied.json')
        self.questions_shown = str(root / 'state/questions_front_shown.json')
        self.chat_replied = str(root / 'state/chat_replied.json')
        self.chat_shown = str(root / 'state/chat_front_shown.json')


def configure(data_dir: str | Path | None = None) -> DataPaths:
    global current
    root = Path(data_dir or os.environ.get("SELLEROPS_DATA_DIR") or PROJECT_ROOT / "data")
    current = DataPaths(root.expanduser().resolve())
    return current


def daily_log_dir(day: date | datetime | None = None, data: DataPaths | None = None) -> Path:
    d = day or date.today()
    return Path((data or current).log_dir) / f'{d:%Y-%m-%d}'


def shop_json_path(shop_id: int | str) -> str:
    return str(Path(current.products_dir) / f'shop{int(shop_id)}_products_all.json')


def shop_mapping_xlsx(shop_id: int | str, shop_name: str | None = None) -> str:
    sid = int(shop_id)
    if shop_name:
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', shop_name).rstrip(' .')
        return str(Path(current.shops_dir) / f'shop_{sid}_{safe_name}.xlsx')
    matches = sorted(Path(current.shops_dir).glob(f'shop_{sid}_*.xlsx'))
    return str(matches[0] if matches else Path(current.shops_dir) / f'shop_{sid}.xlsx')


def is_shop_archived(shop_id: int | str) -> bool:
    sid = int(shop_id)
    archive = Path(current.shops_archive_dir)
    return (archive / f'shop_{sid}.xlsx').exists() or any(archive.glob(f'shop_{sid}_*.xlsx'))


current = configure()
