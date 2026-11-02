"""Offline business regression cases with synthetic fixtures."""
import os
import time
import unittest


class TestRegression(unittest.TestCase):
    @staticmethod
    def _fb(found=None, stats=None, missing=None, pages=1):
            """构造 find_batches 返回值（离线单测用）。"""
            found = found or {}
            return {"found": found, "missing": set(missing or []),
                    "all_found": not (missing or []), "pages_fetched": pages,
                    "stats": stats or {}}

    def test_discount_upload_two_phase(self):
        """WB 改折扣两阶段提交（离线 mock，不触网/不写平台）。

            依据抓包 `api/网络请求/wb批量修改折扣+降价提示.har`：`upload/task` 必须
            先 `checkChange=true` 预检（只回弹窗标记、无 id），再 `checkChange=false`
            真正提交（回 `data.id`）。旧实现 URL 写死 checkChange=true，导致只做了预检、
            taskId 恒为 None，平台侧根本没落库。
            """
        from unittest import mock
        from sellerops.integrations.wildberries import client as wc
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs.get('json')))
            if url.endswith('checkChange=true'):
                return {'data': {'priceModal': False, 'quarantineModal': True}, 'error': False, 'errorText': ''}
            return {'data': {'id': 164469388, 'alreadyExists': False}, 'error': False, 'errorText': ''}
        payload = [{'vendorCode': 'BCS-QQNN-579331069', 'nmID': 1579005260, 'discount': 49, 'currencyIsoCode': 'CNY'}]
        shop = {'shopId': 9352, 'shopName': '袁州1', 'cookie': '', 'authorizev3': 't', 'wb_seller_lk': 't'}
        client = wc.WBClient(shop, root_version='v1.113.3')
        with mock.patch.object(wc, 'request', side_effect=fake_request):
            res = client.upload_batch_discount(payload)
        self.assertEqual([c[1].split('?')[-1] for c in calls], ['checkChange=true', 'checkChange=false'])
        self.assertEqual(calls[0][2], {'data': payload})
        self.assertEqual(calls[1][2], {'data': payload})
        self.assertTrue(res.success)
        self.assertEqual(res.task_id, 164469388)
        self.assertTrue(res.quarantine_modal)
        self.assertFalse(res.price_modal)
        calls.clear()
        with mock.patch.object(wc, 'request', side_effect=fake_request):
            res2 = client.upload_batch_discount(payload, precheck=False)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith('checkChange=false'))
        self.assertEqual(res2.task_id, 164469388)
        self.assertFalse(res2.quarantine_modal)
