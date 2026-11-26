"""商品价格表和归属查询；工作簿仅作为商品资料的只读输入。"""
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Callable
from zipfile import BadZipFile

from sellerops.persistence import db
from sellerops.persistence.headers import HeaderError
from sellerops.persistence.price_book import read_price_book
from sellerops.settings import paths


class PriceBookChanged(ValueError):
    pass


PRICE_BOOK_ERRORS = (OSError, HeaderError, ValueError)


def _version(stat: stat_result) -> tuple[int, int]:
    return stat.st_mtime_ns, stat.st_size


@dataclass(frozen=True)
class PriceBook:
    path: Path
    stat: stat_result
    rows: list[dict]

    def verify(self) -> None:
        if _version(self.path.stat()) != _version(self.stat):
            raise PriceBookChanged('商品价格表在读取或保存期间被修改，请保存后重试')


def read_current_price_book(*, require_price: bool = False, require_prefix: bool = False,
                            strict_identity: bool = False) -> PriceBook:
    from openpyxl.utils.exceptions import InvalidFileException

    path = Path(paths.current.price_book_xlsx)
    stat = path.stat()
    try:
        rows = read_price_book(str(path), require_price=require_price, require_prefix=require_prefix,
                               strict_identity=strict_identity)
    except (KeyError, InvalidFileException, BadZipFile, SyntaxError) as error:
        raise HeaderError('商品价格表无法解析，请核对文件与 Sheet1') from error
    products = [row for row in rows if row['sku'] and row['cn']]
    if strict_identity and len(products) != len(rows):
        raise ValueError('商品价格表有无效商品键，请核对卖家 SKU 与产品中文名')
    if len({row['sku'] for row in products}) != len(products):
        raise ValueError('商品价格表存在重复卖家 SKU，请核对后再运行')
    book = PriceBook(path, stat, products)
    book.verify()
    return book


def save_price_book(connection, book: PriceBook) -> None:
    """写路径在现有事务内显式更新缓存。"""
    book.verify()
    if db.has_column(connection, 'products', 'name_unique'):
        cached = connection.execute('SELECT book_mtime_ns, book_size, shipping_mode FROM products LIMIT 1').fetchone()
        if cached and tuple(cached)[:2] == _version(book.stat) and cached['shipping_mode'] is not None:
            return
    save_products(connection, book.rows, book.stat)


def save_products(connection, rows: list[dict], stat) -> None:
    stamp = db.now()
    products = [row for row in rows if row['sku'] and row['cn']]
    if len({row['sku'] for row in products}) != len(products):
        raise ValueError('商品价格表存在重复卖家 SKU，请核对后再运行')
    if not db.has_column(connection, 'products', 'name_unique'):
        connection.execute('ALTER TABLE products ADD COLUMN name_unique INTEGER')
    connection.execute('DELETE FROM products')
    connection.executemany("""
        INSERT INTO products (sku, cn, minimum_price, image_url, dimensions, prefix,
                              stock_sku, shipping_mode, book_mtime_ns, book_size, refreshed_at, name_unique)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (r['sku'], r['cn'], r['minimum_price'], r['img'],
         None if r['dimensions'] is None else str(r['dimensions']), r['prefix'], r['stock_sku'], r['shipping_mode'],
         stat.st_mtime_ns, stat.st_size, stamp, r['name_unique']) for r in products
    ])
    connection.execute("""
        UPDATE vc_map SET last_cn = (SELECT cn FROM products WHERE sku = vc_map.sku)
         WHERE status = 'mapped' AND EXISTS (
            SELECT 1 FROM products WHERE sku = vc_map.sku AND cn IS NOT vc_map.last_cn)
    """)


def _cached_products(connection) -> list[dict]:
    uniqueness = ('name_unique' if db.has_column(connection, 'products', 'name_unique')
                  else 'COUNT(*) OVER (PARTITION BY cn) = 1')
    shipping = 'shipping_mode' if db.has_column(connection, 'products', 'shipping_mode') else 'NULL'
    return [{**dict(row), 'idx': index}
            for index, row in enumerate(connection.execute(f"""
                SELECT sku, cn, minimum_price, image_url AS img,
                       dimensions, prefix, stock_sku, {shipping} AS shipping_mode, {uniqueness} AS name_unique
                  FROM products ORDER BY rowid
            """), start=1)]


class MappingRepository:
    """复用现有按商品、前缀和中文名查询的入口。"""

    @staticmethod
    def load_price_book(*, require_price: bool = True, fresh: bool = False) -> list[dict]:
        try:
            return read_current_price_book(require_price=require_price).rows
        except OSError as error:
            if fresh:
                raise ValueError('请保存并关闭《商品价格表.xlsx》，确认文件存在后重试') from error
            with db.connect() as connection:
                rows = _cached_products(connection)
                stamp = connection.execute('SELECT max(refreshed_at) FROM products').fetchone()[0]
            if rows:
                print(f'[提示] 当前无法读取商品价格表，使用 {stamp} 的缓存')
            return rows

    @classmethod
    def current_products(cls) -> list[dict]:
        """可继续使用实测数据或历史名称的流程在工作簿不可用时使用空现行视图。"""
        try:
            return cls.load_price_book(require_price=False, fresh=True)
        except PRICE_BOOK_ERRORS as error:
            print(f'[提示] 跳过商品价格表资料：{error}')
            return []

    @classmethod
    def load_vc_entries(cls, products: list[dict] | None = None, *, require_price: bool = False) -> dict:
        if products is None:
            products = cls.load_price_book(require_price=require_price)
        by_sku = {row['sku']: row for row in products}
        entries = db.vc_entries()
        for row in entries.values():
            product = by_sku.get(row['sku'])
            row['last_cn'] = row['last_cn'] or row['cn']
            row['current_product'] = product is not None
            row.update({field: product.get(field) if product else None for field in db.PRODUCT_FIELDS})
        return entries

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
    def build_vc_resolver(cls, *, label_unmapped: bool = False,
                          entries: dict | None = None) -> tuple[Callable[[str, str], str], dict]:
        if entries is None:
            entries = cls.load_vc_entries()
        names = {vc: row['cn'] or row['last_cn'] for vc, row in entries.items()
                 if row['status'] == 'mapped' and (row['cn'] or row['last_cn'])}

        def resolve_cn(vc: str, default_title: str = '') -> str:
            if not vc:
                return default_title
            row = db.resolve_vc(vc, entries, include_history=True)
            if row['status'] == 'mapped':
                return row['cn'] or '历史归属（名称未知）'
            if label_unmapped:
                return '非货盘' if row['status'] == 'excluded' else '待认领'
            return default_title

        return resolve_cn, names

    @classmethod
    def load_mapping_state(cls, *, require_price: bool = False,
                           products: list[dict] | None = None) -> tuple[dict, dict]:
        if products is None:
            products = cls.load_price_book(require_price=True, fresh=True) if require_price else cls.current_products()
        entries = cls.load_vc_entries(products)
        state, excluded = {}, {}
        for vc in entries:
            row = db.resolve_vc(vc, entries)
            if row['current_product']:
                state[vc] = row
            elif row['status'] == 'excluded':
                excluded[vc] = row['note'] or '非货盘'
        return state, excluded
