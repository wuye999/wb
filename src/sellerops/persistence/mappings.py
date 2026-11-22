"""商品价格表和归属查询；工作簿仅作为商品资料的只读输入。"""
from pathlib import Path
from typing import Callable

from sellerops.persistence import db
from sellerops.persistence.price_book import floor_price, read_price_book
from sellerops.settings import paths


def save_products(connection, rows: list[dict], stat) -> None:
    stamp = db.now()
    products = [row for row in rows if row['sku'] and row['cn']]
    if len({row['sku'] for row in products}) != len(products):
        raise ValueError('商品价格表存在重复卖家 SKU，请核对后再运行')
    connection.execute('DELETE FROM products')
    connection.executemany('INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
        (r['sku'], r['cn'], r['dp'], r['minimum_price'], r['img'],
         None if r['dimensions'] is None else str(r['dimensions']), r['prefix'], r['stock_sku'],
         stat.st_mtime_ns, stat.st_size, stamp) for r in products
    ])


def _cached_products(connection) -> list[dict]:
    return [{**dict(row), 'floor': floor_price(row['dp']), 'idx': index}
            for index, row in enumerate(connection.execute("""
                SELECT sku, cn, actual_price AS dp, minimum_price, image_url AS img,
                       dimensions, prefix, stock_sku FROM products ORDER BY rowid
            """), start=1)]


class MappingRepository:
    """复用现有按商品、前缀和中文名查询的入口。"""

    @staticmethod
    def load_price_book(*, require_price: bool = True, fresh: bool = False) -> list[dict]:
        book = Path(paths.current.price_book_xlsx)
        try:
            stat = book.stat()
            with db.connect() as connection:
                cached = connection.execute(
                    'SELECT book_mtime_ns, book_size FROM products LIMIT 1').fetchone()
                if cached is not None and tuple(cached) == (stat.st_mtime_ns, stat.st_size):
                    # 即使 mtime 未变，提交前也检查用户是否能读取当前工作簿。
                    if fresh:
                        with book.open('rb'):
                            pass
                    return _cached_products(connection)
            rows = read_price_book(str(book), require_price=require_price)
            with db.connect(write=True) as connection:
                current = book.stat()
                if (current.st_mtime_ns, current.st_size) != (stat.st_mtime_ns, stat.st_size):
                    raise ValueError('商品价格表在读取期间被修改，请保存后重试')
                save_products(connection, rows, stat)
                return _cached_products(connection)
        except (FileNotFoundError, PermissionError) as error:
            with db.connect() as connection:
                rows = _cached_products(connection)
                stamp = connection.execute('SELECT max(refreshed_at) FROM products').fetchone()[0]
            if fresh:
                raise ValueError('请保存并关闭《商品价格表.xlsx》，确认文件存在后重试') from error
            if rows:
                print(f'[提示] 当前无法读取商品价格表，使用 {stamp} 的缓存')
            return rows

    @classmethod
    def load_prefix_map(cls, *, strict: bool = False) -> dict[str, dict]:
        rows = cls.load_price_book(require_price=False)
        result = {}
        for row in rows:
            prefix = row['prefix']
            if len(prefix) == 4 and prefix.isascii() and prefix.isalpha():
                if strict and prefix in result:
                    raise ValueError(f'商品价格表前缀重复：{prefix}')
                result[prefix] = row
        return result

    @classmethod
    def build_vc_resolver(cls, *, label_unmapped: bool = False) -> tuple[Callable[[str, str], str], dict]:
        cls.load_price_book(require_price=False)
        entries = db.vc_entries()
        names = {vc: row['cn'] for vc, row in entries.items()
                 if row['status'] == 'mapped' and row['cn']}

        def resolve_cn(vc: str, default_title: str = '') -> str:
            if not vc:
                return default_title
            row = db.resolve_vc(vc, entries)
            if row['status'] == 'mapped':
                return row['cn']
            if label_unmapped:
                return '非货盘' if row['status'] == 'excluded' else '待认领'
            return default_title

        return resolve_cn, names

    @classmethod
    def load_mapping_state(cls) -> tuple[dict, dict]:
        cls.load_price_book(require_price=False)
        entries = db.vc_entries()
        state, excluded = {}, {}
        for vc in entries:
            row = db.resolve_vc(vc, entries)
            if row['status'] == 'mapped':
                state[vc] = row
            elif row['status'] == 'excluded':
                excluded[vc] = row['note'] or '非货盘'
        return state, excluded
