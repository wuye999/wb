"""client。"""
import json
import re


import requests
from sellerops.core import utils as common
from sellerops.core.files import atomic_dump_json

WWW_BASE = "https://www.mabangerp.com/index.php"

AAMZ_BASE = "https://aamz.mabangerp.com/index.php"

API_BASE = "https://api.mabangerp.com/v2"

SSO_GET_TOKEN_URL = "https://api.mabangerp.com/sso/api/v1/getTokenByKey"

REQUEST_INTERVAL = 0.6

PLATFORM_ID_WB = ""

DEF_FORECAST_LOGISTICS = "2369||262534"

DEF_FORECAST_CHANNEL = "wb_box"

CHANNEL_OBJ_RE = re.compile(
    r'\{"id":"(\d+)","source":"\d+","logisticsId":"(\d+)","myLogisticsId":"(\d+)",'
    r'"logisticsChannelName":"((?:\\u[0-9a-fA-F]{4})+)"')

def get_mabang_cred():
    """读 credentials.json 的 mabang 段。"""
    from sellerops.settings import credentials as credentials
    data = credentials.get().data.get("mabang") or {}
    missing = [k for k in ("www_cookie", "shop_map") if not data.get(k)]
    if missing:
        raise RuntimeError(f"credentials.json 缺少 mabang.{missing[0]}（马帮凭证/店铺映射不完整）")
    return data

def cookie_value(cookie_str, name):
    """从 Cookie 字符串提取指定字段值"""
    m = re.search(re.escape(name) + r"=([^;]+)", cookie_str or "")
    return m.group(1) if m else ""

def api_key_from_www(cred):
    return cookie_value(cred.get("www_cookie", ""), "MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE")

def api_ready(cred):
    """api 域凭证是否可用：有 Bearer，或 www_cookie 里能提取 key（可自动续期）"""
    return bool(cred.get("api_bearer")) or bool(api_key_from_www(cred))

def refresh_api_token(cred):
    """用 key（www cookie 提取）调 sso getTokenByKey 换发新 Bearer 并写回 credentials.json；返回新 token。"""
    key = api_key_from_www(cred)
    if not key:
        raise RuntimeError("www_cookie 中未找到 MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE，无法自动续期 api token")
    r = requests.post(
        SSO_GET_TOKEN_URL,
        headers={"User-Agent": common.UA, "Content-Type": "application/json",
                 "ProjectId": "erp", "cluster-id": "1", "lang": "cn",
                 "TimeZone": "UTC+8"},
        data=json.dumps({"key": key, "lang": "zh"}),
        timeout=30,
    )
    d = parse_json(r)
    token = (d.get("data") or {}).get("token")
    if d.get("code") != 200 or not token:
        raise RuntimeError(f"getTokenByKey 续期失败: {str(d)[:150]}")
    from sellerops.settings import credentials as credentials
    c = credentials.get()
    mb = c.data.setdefault("mabang", {})
    mb["api_bearer"] = token
    mb["api_key"] = key
    atomic_dump_json(c.path, c.data, indent=2, use_lock=True)
    c.reload()
    return token

def www_headers(cred):
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.mabangerp.com",
        "Referer": "https://www.mabangerp.com/index.php?mod=order.list",
        "Cookie": cred["www_cookie"],
    }

def api_headers(cred):
    bearer = cred.get("api_bearer") or ""
    if not bearer:
        bearer = refresh_api_token(cred)
    key = cred.get("api_key") or api_key_from_www(cred)
    if not key:
        raise RuntimeError("无法确定 api 域 key 头（www_cookie 中无 MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE）")
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/json",
        "Authorization": "Bearer " + bearer,
        "key": key,
        "ProjectId": "erp",
        "TimeZone": "UTC+8",
        "cluster-id": "1",
        "lang": "cn",
    }

def aamz_headers(cred):
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://aamz.mabangerp.com",
        "Referer": "https://aamz.mabangerp.com/index.php?mod=uploadforecastorderv2",
        "Cookie": cred.get("aamz_cookie") or cred["www_cookie"],
    }

def parse_json(resp):
    return resp.json()

def api_post(cred, path, body):
    """api 域 POST：401 时自动续期 Bearer 后重试一次"""
    payload = json.dumps(body)
    for attempt in (1, 2):
        r = requests.post(API_BASE + path, headers=api_headers(cred), data=payload, timeout=60)
        if r.status_code != 401:
            return r
        if attempt == 2:
            break
        print("  [api] 401，自动续期 Bearer 后重试...")
        refresh_api_token(cred)
        cred = get_mabang_cred()
    return r
