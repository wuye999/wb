
from catalog_fixture import map_vcs, snapshot, write_book
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from io import StringIO
import json
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sellerops.cli.main import build_parser, leaf_commands, main
from sellerops.features.catalog import mismatch_check
from sellerops.persistence import db
from sellerops.persistence.snapshots import save_shop
from sellerops.settings import paths


class Gallery(HTMLParser):
    def __init__(self):
        super().__init__()
        self.groups = []
        self.cards = {}
        self.images = {}
        self.zooms = set()
        self.vc = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and attrs.get("class") == "group":
            self.groups.append(attrs["data-cn"])
        if tag == "article" and attrs.get("class") == "product-card":
            self.vc = attrs["data-vc"]
            self.cards[self.vc] = attrs
        if tag == "img" and attrs.get("class") == "thumb":
            self.images[self.vc] = attrs
        if tag == "button" and attrs.get("class") == "zoom":
            self.zooms.add(self.vc)


def card(vc, nm_id, price=None, **fields):
    return dict(vendorCode=vc, nmId=nm_id, sizeList=[{'chrtId': nm_id, 'price': price}], **fields)


class MismatchCheckTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        self.data = Path(temp.name) / 'data'
        paths.configure(self.data)

    def read_gallery(self, result):
        html_path = Path(result['artifacts']['html'])
        self.assertEqual(html_path, Path(paths.current.out_mismatch_html))
        self.assertEqual(html_path.parent, self.data.parent / 'exports' / 'workbench')
        page = html_path.read_text(encoding='utf-8')
        gallery = Gallery()
        gallery.feed(page)
        return page, gallery

    def test_workbench_loads_medium_image_with_same_card_fallbacks(self):
        basket = "https://basket-01.wbbasket.ru/vol1/part100/100123/images"
        external = "https://example.invalid/main.webp?a=1&b='two'"
        write_book([('fixture', '示例手机', 10)])
        snapshot(1, [
            card('VC-tm', 300123, repImg=f'{basket}/tm/1.webp'),
            card('VC-nm', 100123), card('VC-empty', None), card('VC-external', 100124, repImg=external),
        ])
        map_vcs({vc: 'fixture' for vc in ('VC-tm', 'VC-nm', 'VC-empty', 'VC-external')})
        page, gallery = self.read_gallery(mismatch_check.run(cn='示例手机'))

        self.assertEqual(gallery.zooms, {'VC-tm', 'VC-nm', 'VC-external'})
        for vc in ("VC-tm", "VC-nm"):
            image = gallery.images[vc]
            self.assertEqual(image["src"], f"{basket}/c246x328/1.webp")
            self.assertEqual(image["data-big"], f"{basket}/big/1.webp")
            self.assertEqual(image["loading"], "lazy")
        self.assertEqual(json.loads(gallery.images["VC-tm"]["data-fallback"]),
                         [f"{basket}/big/1.webp", f"{basket}/tm/1.webp"])
        self.assertEqual(json.loads(gallery.images["VC-nm"]["data-fallback"]),
                         [f"{basket}/big/1.webp"])
        self.assertEqual(gallery.images["VC-external"]["src"], external)
        self.assertEqual(json.loads(gallery.images["VC-external"]["data-fallback"]), [])
        self.assertNotIn("VC-empty", gallery.images)
        self.assertIn("暂无商品图", page)

    def test_gallery_groups_anomalies_first_and_marks_prefix_and_price(self):
        write_book([
            ('S1', 'A 正常组', 50, 'DUPL'),
            ('S2', 'Z 异常组', 60, 'GOOD'),
            ('S3', '登记商品', 45, 'ELSE'),
            ('S4', '另一商品', None, 'DUPL'),
            ('S5', '无最低售价', None),
        ])
        snapshot(1, [
            card('BCS-ELSE-100', 100, 134, title='Товар & "название" </script>'),
            card('BCS-GOOD-101', 101, 121),
            card('BCS-RAND-102', 102, 610),
            card('BCS-DUPL-103', 103, 150),
            card('BCS-DUPL-104', 104),
            card('BCS-RAND-105', 105, 85),
            card('BCS-RAND-106', 106, 85),
            card('BCS-ELSE-108', 108, 134, trashedAt=1),
        ])
        snapshot(2, [card('BCS-ELSE-100', 500, 120), card('BCS-GOOD-101', 501, 121)])
        with db.connect(write=True) as connection:
            save_shop(connection, 3, status='archived')
        snapshot(3, [card('BCS-GOOD-101', 601, 141), card('BCS-ELSE-107', 607, 140)])
        map_vcs({'BCS-ELSE-100': 'S2', 'BCS-GOOD-101': 'S2', 'BCS-RAND-102': 'S2',
                 'BCS-DUPL-103': 'S1', 'BCS-DUPL-104': 'S1', 'BCS-RAND-105': 'S5',
                 'BCS-ELSE-107': 'S2', 'BCS-ELSE-108': 'S2'})

        result = mismatch_check.run()
        page, gallery = self.read_gallery(result)
        self.assertEqual(result['counts'], {'total': 6, 'groups': 3, 'anomalies': 1})
        self.assertEqual(gallery.groups, ['Z 异常组', 'A 正常组', '无最低售价'])
        self.assertEqual([vc for vc, attrs in gallery.cards.items() if attrs['data-anomaly'] == '1'],
                         ['BCS-ELSE-100'])
        self.assertEqual(gallery.cards['BCS-ELSE-100']['data-shops'], '1, 2')
        self.assertNotIn('BCS-ELSE-107', gallery.cards)
        self.assertIn('display:grid;grid-template-columns:repeat(auto-fill', page)
        self.assertIn('object-fit:contain', page)
        self.assertIn('共 3 个 · 最低售价 ¥60', page)
        self.assertIn('⚠ 1 个异常', page)
        self.assertIn('共 6 条 · 3 个中文名 · 异常 1 条 · 已勾选', page)
        self.assertIn('badge prefix" title="ELSE 登记为 登记商品（S3）">前缀冲突', page)
        self.assertIn('badge price" title="最低售价 ¥60 · shop1（1）¥134">非最低价倍数', page)
        self.assertIn('¥121 · <span class="multiple">×2', page)
        self.assertIn('¥610 · <span class="multiple">×10', page)
        self.assertIn('¥150 · <span class="multiple">×3', page)
        self.assertIn('title="Товар &amp; &quot;название&quot; &lt;/script&gt;"', page)
        self.assertIn('id="cn-select" multiple', page)
        self.assertIn('<dialog id="lightbox"', page)
        self.assertNotIn('<table', page)

    def test_price_flags_only_stores_with_non_multiple_prices(self):
        normal, deviating = 'VC-normal', 'VC-deviating'
        write_book([('S1', '旅行杯', 60)])
        snapshot(1, [card(normal, 101, 120), card(deviating, 102, 120)])
        snapshot(2, [card(normal, 201, 600), card(deviating, 202, 600)])
        snapshot(3, [card(normal, 301), card(deviating, 302, 134)])
        map_vcs({normal: 'S1', deviating: 'S1'})

        result = mismatch_check.run()
        page, gallery = self.read_gallery(result)

        self.assertEqual(result['counts'], {'total': 2, 'groups': 1, 'anomalies': 1})
        self.assertEqual(gallery.cards[normal]['data-anomaly'], '0')
        self.assertEqual(gallery.cards[deviating]['data-anomaly'], '1')
        self.assertEqual(page.count('¥120 · <span class="multiple">×2/×10</span>'), 2)
        self.assertIn('badge price" title="最低售价 ¥60 · shop3（3）¥134">非最低价倍数', page)

    def test_time_and_exact_name_filters_and_map_json_receipt(self):
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        before = (today - timedelta(days=2)).isoformat()
        today = today.isoformat()
        write_book([('S1', '旅行杯', 50), ('S2', '旅行杯盖', 48)])
        snapshots = [
            card('VC-begin', 201, 64, createAt=f'{yesterday}T11:45:30'),
            card('VC-end', 202, 100, createAt=today),
            card('VC-before', 203, 100, createAt=before),
            card('VC-other', 204, 48, createAt=today),
            card('VC-undated', 205, 100, createAt='无'),
        ]
        snapshot(1, snapshots)
        map_vcs({item['vendorCode']: 'S2' if item['vendorCode'] == 'VC-other' else 'S1'
                 for item in snapshots})

        output, logs = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(logs):
            code = main(['map', 'check-mismatch', '--cn', '旅行杯', '--days', '1',
                         '--begin', yesterday, '--end', today, '--data-dir', str(self.data), '--json'])
        self.assertEqual(code, 0)
        receipt = json.loads(output.getvalue())
        self.assertTrue(receipt['ok'])
        self.assertEqual(receipt['data']['counts'], {'total': 2, 'groups': 1, 'anomalies': 1})
        _, gallery = self.read_gallery(receipt['data'])
        self.assertEqual(set(gallery.cards), {'VC-begin', 'VC-end'})
        self.assertEqual(gallery.groups, ['旅行杯'])
        _, gallery = self.read_gallery(mismatch_check.run(cn='旅行杯', days=1))
        self.assertEqual(set(gallery.cards), {'VC-end'})
        self.assertEqual(mismatch_check.run(cn='旅行')['counts'], {'total': 0, 'groups': 0, 'anomalies': 0})

    def test_cli_exposes_only_one_screening_workbench(self):
        parser = build_parser()
        commands = dict(leaf_commands(parser))
        self.assertNotIn('catalog check-mapping', commands)
        catalog = parser.parse_args(['catalog', 'check-mismatch', '--cn', '示例', '--days', '3',
                                     '--begin', '2026-11-15', '--end', '2026-11-17'])
        workbuddy = parser.parse_args(['map', 'check-mismatch', '--cn', '示例', '--days', '3',
                                       '--begin', '2026-11-15', '--end', '2026-11-17', '--json'])
        self.assertEqual(catalog.handler, workbuddy.handler)
        for field in ('cn', 'begin', 'end', 'days'):
            self.assertEqual(getattr(catalog, field), getattr(workbuddy, field))
