import json
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from sellerops.features.catalog import mapping, mismatch_check
from sellerops.persistence.headers import COL_ACTUAL_PRICE, COL_CN, COL_IMG, COL_NM, COL_VC
from sellerops.settings import paths


class ProductImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = {}
        self.vc = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and attrs.get("class") == "mrow":
            self.vc = attrs["data-vc"]
        if tag == "img" and attrs.get("class") == "thumb":
            self.images[self.vc] = attrs


class MismatchCheckImagesTest(unittest.TestCase):
    def test_workbench_loads_medium_image_with_same_card_fallbacks(self):
        basket = "https://basket-01.wbbasket.ru/vol1/part100/100123/images"
        external = "https://example.invalid/main.webp?a=1&b='two'"
        with TemporaryDirectory() as temp:
            workbook_path = Path(temp) / "mapping.xlsx"
            html_path = Path(temp) / "mismatch.html"
            book = Workbook()
            sheet = book.active
            sheet.title = "映射总表"
            headers = [COL_CN, COL_VC, COL_ACTUAL_PRICE, "店铺1价格(CNY)", COL_IMG, COL_NM]
            sheet.append(headers)
            sheet.append(["示例手机", "VC-tm", 10, 10, f"{basket}/tm/1.webp", 300123])
            sheet.append(["示例手机", "VC-nm", 10, 10, None, 100123])
            sheet.append(["示例手机", "VC-empty", 10, 10, None, None])
            sheet.append(["示例手机", "VC-external", 10, 10, external, 100123])
            book.save(workbook_path)
            book.close()
            with patch.object(paths, "MAPPING_XLSX", str(workbook_path)), \
                    patch.object(paths, "OUT_MISMATCH_HTML", str(html_path)), \
                    patch.object(mapping, "shop_id", return_value=1):
                mismatch_check.run(cn="示例手机")
            page = html_path.read_text(encoding="utf-8")

        parser = ProductImages()
        parser.feed(page)
        for vc in ("VC-tm", "VC-nm"):
            image = parser.images[vc]
            self.assertEqual(image["src"], f"{basket}/c246x328/1.webp")
            self.assertEqual(image["data-big"], f"{basket}/big/1.webp")
            self.assertEqual(image["loading"], "lazy")
        self.assertEqual(json.loads(parser.images["VC-tm"]["data-fallback"]),
                         [f"{basket}/big/1.webp", f"{basket}/tm/1.webp"])
        self.assertEqual(json.loads(parser.images["VC-nm"]["data-fallback"]),
                         [f"{basket}/big/1.webp"])
        self.assertEqual(parser.images["VC-external"]["src"], external)
        self.assertEqual(json.loads(parser.images["VC-external"]["data-fallback"]), [])
        self.assertNotIn("VC-empty", parser.images)
        self.assertIn("暂无商品图", page)
