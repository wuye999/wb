# -*- coding: utf-8 -*-
"""
wb_ops 统一凭证管理

单一凭证文件：data/credentials.json，结构：
{
  "bcs": { "base_url": "...", "token": "...", "limit_key": "...", "cookie_extra": "..." },
  "wb":  { "root_version": "v1.108.1", "shops": [ {shop_name, shop_id, authorizev3, wb_seller_lk, cookie}, ... ] }
}

BCS 三件套（token/limit_key/cookie_extra）+ WB 5 店 cookie 全在这里，换凭证只改一个文件。
"""
import json

from . import config
DEFAULT_ROOT_VERSION = "v1.108.1"


class Credentials:
    """凭证访问器：读 credentials.json，提供 BCS header 与 WB 店铺会话数据。"""

    def __init__(self, path=None):
        self.path = path or config.CREDENTIALS_JSON
        self.reload()

    def reload(self):
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)
        self.bcs = self.data.get("bcs") or {}
        self.wb = self.data.get("wb") or {}
        # 只保留三件套齐全的店铺
        self.shops = [s for s in self.wb.get("shops", [])
                      if s.get("authorizev3") and s.get("wb_seller_lk") and s.get("cookie")]
        self.root_version = self.wb.get("root_version", DEFAULT_ROOT_VERSION)
        self.ai = self.data.get("ai") or {}

    # ---- BCS 云端 API 凭证 ----
    @property
    def base_url(self):
        return self.bcs.get("base_url", "https://wb.bcserp.com/prod-api")

    @property
    def token(self):
        return self.bcs.get("token") or ""

    @property
    def limit_key(self):
        return self.bcs.get("limit_key") or ""

    @property
    def cookie_extra(self):
        return self.bcs.get("cookie_extra") or ""

    def bcs_headers(self):
        """BCS 请求头（Bearer + X-Limit-Key + Cookie）"""
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-HK;q=0.8,zh-TW;q=0.7",
            "Authorization": "Bearer " + self.token,
            "X-Limit-Key": self.limit_key,
            "Referer": "https://wb.bcserp.com/productList",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"),
            "Cookie": ("Admin-Token=" + self.token + "; Limit-Key=" + self.limit_key +
                       ";" + self.cookie_extra),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    # ---- AI 回复（OpenAI 兼容 LLM：DeepSeek / 商汤日日新 SenseNova / 通义 等） ----
    @property
    def ai_key(self):
        return self.ai.get("api_key") or self.ai.get("deepseek_key") or ""

    @property
    def ai_base_url(self):
        return self.ai.get("base_url") or "https://api.deepseek.com/chat/completions"

    @property
    def ai_model(self):
        return self.ai.get("model") or "deepseek-chat"

    @property
    def ai_watch_interval(self):
        try:
            return int(self.ai.get("watch_interval") or 90)
        except (TypeError, ValueError):
            return 90

    # ---- WB 卖家后台 cookie 三件套 ----
    def wb_shops(self):
        return self.shops

    def wb_shop(self, shop_id):
        for s in self.shops:
            if s.get("shopId") == shop_id:
                return s
        return None

    def wb_shop_ids(self):
        return [s.get("shopId") for s in self.shops]

    def validate(self):
        """校验凭证完整性，返回 (ok, 问题列表)"""
        problems = []
        if not self.token:
            problems.append("bcs.token 为空（BCS 接口会 401）")
        if not self.limit_key:
            problems.append("bcs.limit_key 为空（网关会 405）")
        for s in self.wb.get("shops", []):
            if not (s.get("authorizev3") and s.get("wb_seller_lk") and s.get("cookie")):
                problems.append(f"WB 店铺 {s.get('shopName')}({s.get('shopId')}) 三件套不完整")
        return (len(problems) == 0), problems


# ---- 模块级单例（懒加载，cookies-update 后 reload 即可） ----
_CRED = None


def get():
    global _CRED
    if _CRED is None:
        _CRED = Credentials()
    return _CRED


def reload():
    global _CRED
    _CRED = Credentials()
    return _CRED
