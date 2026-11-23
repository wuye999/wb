"""映射报表共用的快照价格和主店查询。"""
from sellerops.core import utils as common
from sellerops.domain.snapshot_values import first_price
from sellerops.features.catalog import products
from sellerops.features.catalog.mapping_excel import stock_summary
from sellerops.settings import paths as config


def main_shop(data: config.DataPaths):
    shops = products.snapshot_shops()
    return config.MAIN_SHOP or (shops[0]['id'] if shops else None)


def priced_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("trashedAt"):
            continue
        price = first_price(r)
        if price is None:
            continue
        c = dict(r)
        c["price"] = price
        c["vc"] = r.get("vendorCode") or ""
        c["wbnm"] = str(r.get("nmId")) if r.get("nmId") else (common.extract_wb_nm(c["vc"]) or str(c["vc"]).rsplit("-", 1)[-1])
        c["img"] = r.get("repImg") or ""
        out.append(c)
    return out


def price_of(r):
    """商品代表价 = sizeList[0].price"""
    return first_price(r)
