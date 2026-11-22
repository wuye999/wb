
from catalog_fixture import map_vcs, snapshot, write_book
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
            html_path = Path(temp) / "mismatch.html"
            self.addCleanup(paths.configure, paths.current.data_dir)
            paths.configure(temp)
            write_book([('fixture', '\u793a\u4f8b\u624b\u673a', 10, '', '')])
            snapshot(1, [dict(vendorCode=vc, repImg=img, nmId=nm) for vc, img, nm in [
                ('VC-tm', f'{basket}/tm/1.webp', 300123),
                ('VC-nm', None, 100123), ('VC-empty', None, None), ('VC-external', external, 100124),
            ]])
            map_vcs({vc: 'fixture' for vc in ('VC-tm', 'VC-nm', 'VC-empty', 'VC-external')})
            with patch.object(paths.current, 'out_mismatch_html', str(html_path)), \
                    patch.object(mapping, 'main_shop', return_value=1):
                mismatch_check.run(cn='\u793a\u4f8b\u624b\u673a')
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
