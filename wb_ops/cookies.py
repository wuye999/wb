# -*- coding: utf-8 -*-
"""
wb_ops cookie 刷新（原 update_cookies.py）

从含 fetch 请求块的 .md 抓包文件提取会话（authorizev3 / wb-seller-lk / cookie），
解码 wb-seller-lk JWT 取店铺 Z-Sid，按 sid 匹配并更新 data/credentials.json 的 wb.shops。
"""
import json
import os
import re

from . import config
from . import credentials
from . import common


def extract_sessions(md_text):
    """按 fetch( 分割，每个块提取 authorizev3 / wb-seller-lk / cookie，返回列表"""
    sessions = []
    for block in re.split(r"\bfetch\(", md_text)[1:]:
        a3 = re.search(r'"authorizev3":\s*"([^"]+)"', block)
        lk = re.search(r'"wb-seller-lk":\s*"([^"]+)"', block)
        ck = re.search(r'"cookie":\s*"([^"]+)"', block)
        if not (a3 and lk and ck):
            continue
        try:
            sid = (common.jwt_payload(lk.group(1)).get("data") or {}).get("Z-Sid", "")
        except Exception:
            sid = ""
        sessions.append({"authorizev3": a3.group(1), "wb_seller_lk": lk.group(1),
                         "cookie": ck.group(1), "sid": sid})
    return sessions


def build_sid_map(cfg):
    """从 credentials.json 的 wb.shops 动态推导 {sid前缀8位: shopName}。

    店铺的稳定标识（Z-Sid 前 8 位）已内嵌在每家店的 wb_seller_lk JWT 里，
    从这里解码即可得到 sid→店铺名 映射，无需在代码里硬编码任何店铺。
    """
    sid_map = {}
    for shop in (cfg.get("wb") or {}).get("shops") or []:
        lk = shop.get("wb_seller_lk") or ""
        try:
            sid = (common.jwt_payload(lk).get("data") or {}).get("Z-Sid", "")
        except Exception:
            sid = ""
        if sid:
            sid_map[sid[:8]] = shop.get("shopName")
    return sid_map


def run(md_path):
    common.ensure_utf8_stdout()
    if not os.path.isabs(md_path):
        md_path = os.path.join(config.HAR_DIR, md_path)
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()

    sessions = extract_sessions(md_text)
    if not sessions:
        print("[错误] 未从 md 中提取到任何会话（检查 fetch 块格式）")
        return 1
    print(f"从 {os.path.basename(md_path)} 提取到 {len(sessions)} 组会话")

    cfg_path = config.CREDENTIALS_JSON
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    sid_map = build_sid_map(cfg)
    if not sid_map:
        print("[警告] 无法从凭证推导店铺 sid 映射（wb_seller_lk 缺失/无效），请先确保 credentials.json 各店铺凭证完整")

    updated = []
    for s in sessions:
        sid = s["sid"][:8]
        shop_name = sid_map.get(sid)
        if not shop_name:
            print(f"  [跳过] 未识别的 sid={sid}...（凭证中无匹配店铺）")
            continue
        shop = next((x for x in cfg.get("wb", {}).get("shops", []) if x["shopName"] == shop_name), None)
        if not shop:
            print(f"  [跳过] 凭证中无店铺 {shop_name}")
            continue
        shop["authorizev3"] = s["authorizev3"]
        shop["wb_seller_lk"] = s["wb_seller_lk"]
        shop["cookie"] = s["cookie"]
        shop["_comment"] = f"最近更新 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}"
        updated.append(shop_name)
        print(f"  ✓ {shop_name} (sid={sid}...) 已更新")

    if not updated:
        print("[错误] 没有更新任何店铺")
        return 1
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    credentials.reload()
    print(f"credentials.json 已更新: {updated}")
    print("提示：cookie 抓取时间若已超过 2 天，请尽快用 wb.py promo-apply 验证")
    return 0
