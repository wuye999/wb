# -*- coding: utf-8 -*-
"""
wb_ops WB 卖家后台 API 客户端（cookie 会话三件套）

合并了原 wb_promo_apply.py / wb_clean_delete.py 中重复的会话构造与请求重试：
- make_session：按店铺构建 requests.Session（authorizev3 / wb-seller-lk / Cookie 三件套）
- request / request_post：403 抛 CookieExpiredError，其余指数退避重试
凭证统一读 credentials.py（WB 部分）。
"""
import time

import requests

from . import credentials
from . import common

RETRY_SLEEPS = [1, 3, 8]  # 非 403 错误重试间隔（指数退避）


def make_session(shop, root_version=None):
    """按店铺构建 requests.Session（cookie + 三个业务头）。
    shop: credentials.json 里 wb.shops[] 的一项。"""
    root_version = root_version or credentials.get().root_version
    s = requests.Session()
    for c in (shop.get("cookie") or "").split(";"):
        c = c.strip()
        if "=" in c:
            k, v = c.split("=", 1)
            s.cookies.set(k.strip(), v.strip())
    s.headers.update({
        "Accept": "*/*",
        "authorizev3": shop["authorizev3"],
        "wb-seller-lk": shop["wb_seller_lk"],
        "Content-Type": "application/json",
        "Origin": "https://seller.wildberries.ru",
        "Referer": "https://seller.wildberries.ru/",
        "root-version": root_version,
        "User-Agent": common.UA,
    })
    return s


def request(session, method, url, **kwargs):
    """WB 接口统一请求：403 抛 CookieExpiredError；其他 4xx/5xx 指数退避重试。"""
    last = None
    for i, wait in enumerate([0] + RETRY_SLEEPS):
        try:
            r = session.request(method, url, timeout=30, **kwargs)
            if r.status_code == 403:
                raise common.CookieExpiredError("403：cookie 已失效（cfidsw-wb 过期），请刷新 credentials.json 该店 cookie")
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        except common.CookieExpiredError:
            raise
        except Exception as e:
            last = e
            if wait:
                print(f"    [重试] 等待 {wait}s ...")
                time.sleep(wait)
    raise last


def request_post(session, url, payload, allow_400_json=False):
    """POST 专用：allow_400_json=True 时，HTTP 400 且 body 是业务 JSON（删除部分失败）
    按正常响应返回（additionalErrors 可解析），不抛错。"""
    last = None
    for i, wait in enumerate([0] + RETRY_SLEEPS):
        try:
            r = session.post(url, json=payload, timeout=30)
            if r.status_code == 403:
                raise common.CookieExpiredError("403：cookie 已失效（cfidsw-wb 过期），请刷新 credentials.json 该店 cookie")
            if r.status_code == 400 and allow_400_json:
                try:
                    return r.json()
                except Exception:
                    raise RuntimeError(f"HTTP 400: {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        except common.CookieExpiredError:
            raise
        except Exception as e:
            last = e
            if wait:
                print(f"    [重试] 等待 {wait}s ...")
                time.sleep(wait)
    raise last
