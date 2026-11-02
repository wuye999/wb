"""publish inputs。"""

import csv
import json
import math
import os
import re


from typing import Dict, Any, List, Optional, Tuple


from sellerops.persistence.price_book import price_book_columns
from sellerops.persistence.price_book import floor_price
from sellerops.settings import paths as config


from sellerops.domain.vendor_codes import extract_vc_prefix


DEFAULT_STOCK = 999

def parse_dims_str(s: str) -> Optional[Tuple[int, int, int, float]]:
    """解析自定义尺寸字符串 '长*宽*高/毛重'（例: 8*14*26/0.3 或 8x14x26/0.3），返回 (L, W, H, weight)。"""
    if not s:
        return None
    s_clean = str(s).strip()
    m = re.match(r"^([\d.]+)\s*[*xX]\s*([\d.]+)\s*[*xX]\s*([\d.]+)(?:\s*/\s*([\d.]+))?$", s_clean)
    if not m:
        return None
    try:
        wt = float(m.group(4)) if m.group(4) else 0.0
        return (
            math.ceil(float(m.group(1))),
            math.ceil(float(m.group(2))),
            math.ceil(float(m.group(3))),
            round(wt, 3),
        )
    except (TypeError, ValueError):
        return None

def parse_cn_stock(s: str) -> Dict[str, int]:
    """解析 `--cn-stock` 参数："中文名:库存,中文名:库存" → {中文名: 库存}。"""
    out: Dict[str, int] = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cn, v = part.split(":", 1)
        cn = cn.strip()
        try:
            out[cn] = int(str(v).strip())
        except ValueError:
            continue
    return out

def stock_for(cn: str, overrides: Optional[Dict[str, int]] = None, default: int = DEFAULT_STOCK) -> int:
    """根据商品中文名决定上架库存：overrides 优先，未指定则返回 default"""
    overrides = overrides or {}
    if cn in overrides:
        return overrides[cn]
    return default

def load_publish_catalog() -> Dict[str, Dict[str, Any]]:
    """加载商品价格表，返回 {cn: {sku, cn, dp, dims, prefix}}"""
    import openpyxl
    if not os.path.exists(config.PRICE_BOOK_XLSX):
        return {}
    wb = openpyxl.load_workbook(config.PRICE_BOOK_XLSX, data_only=True)
    try:
        ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active
        cols = price_book_columns(ws, config.PRICE_BOOK_XLSX, required=("cn", "sku", "dp"))
        i_cn, i_sku, i_dp = cols["cn"], cols["sku"], cols["dp"]
        i_dim, i_prefix = cols["dim"], cols["prefix"]
        boss = {}
        for r in ws.iter_rows(min_row=2, values_only=True):
            def cell(i):
                return r[i] if i is not None and i < len(r) else None

            cn = cell(i_cn)
            if cn:
                cn = str(cn).strip()
                boss[cn] = {
                    "sku": cell(i_sku),
                    "cn": cn,
                    "dp": cell(i_dp),
                    "dims": str(cell(i_dim) or "").strip(),
                    "prefix": str(cell(i_prefix) or "").strip().upper(),
                }
        return boss
    finally:
        wb.close()

def load_publish_file(file_path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    统一解析批量上架输入文件（支持 .json, .csv, .txt）。
    自动兼容：
      1. .json: List[dict] 结构或 List[nm] 数组
      2. .csv:
         - 订单登记/筛选非我店订单 CSV（含 '商品中文名' 与 '现在商品的nmId' / 'VC' 列），
           自动关联商品价格表推导新 VC (BCS-{前缀}-{原尾段})、实际售价与包装尺寸；
         - 普通上架 CSV（含 nmId, price, vendorCode, cn 列）；
      3. .txt: 每行一个商品码
    返回: (file_items, raw_nms)
    """
    if not file_path or not os.path.exists(file_path):
        return [], []

    file_items: List[Dict[str, Any]] = []
    raw_nms: List[str] = []

    if file_path.endswith(".json"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                j_data = json.load(f)
            if isinstance(j_data, list):
                for elem in j_data:
                    if isinstance(elem, dict) and (elem.get("nmId") or elem.get("sku") or elem.get("nm")):
                        file_items.append(elem)
                    elif str(elem).isdigit():
                        raw_nms.append(str(elem))
        except Exception as e:
            print(f"[警告] 解析 JSON 文件失败: {e}")
        return file_items, raw_nms

    if file_path.endswith(".csv"):
        rows = []
        for enc in ["utf-8-sig", "gb18030", "gbk"]:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        break
            except Exception:
                continue

        if not rows:
            return [], []

        first_row = rows[0]
        has_nm_col = any("nmId" in k or "nmid" in k.lower() or "商品码" in k for k in first_row.keys())
        has_cn_col = any("中文名" in k or "品名" in k for k in first_row.keys())
        has_vc_col = "VC" in first_row or "vendorCode" in first_row

        if has_nm_col and (has_cn_col or has_vc_col):
            # 自动关联商品价格表推导上架配置
            boss_data = load_publish_catalog()
            for idx, r in enumerate(rows, 1):
                cn_key = next((k for k in r.keys() if "中文名" in k or "品名" in k), None)
                nm_key = next((k for k in r.keys() if "nmId" in k or "nmid" in k.lower() or "商品码" in k), None)
                vc_key = next((k for k in r.keys() if k in ("VC", "vendorCode")), None)

                cn = str(r.get(cn_key, "")).strip() if cn_key else ""
                cur_nm_raw = str(r.get(nm_key, "")).strip() if nm_key else ""
                orig_vc = str(r.get(vc_key, "")).strip() if vc_key else ""

                # 取第一个有效 nmId
                cur_nm = cur_nm_raw.split("/")[0].strip() if cur_nm_raw else ""
                if not cur_nm or not cur_nm.isdigit():
                    continue

                b = boss_data.get(cn)
                my_prefix = b["prefix"] if b else ""
                if not my_prefix and orig_vc:
                    my_prefix = extract_vc_prefix(orig_vc) or ""

                # 提取原 VC 尾段并拼接新 VC: BCS-{前缀}-{原尾段}
                tail = ""
                if orig_vc:
                    m = re.match(r"^BCS-[A-Za-z0-9]+-(.+)$", orig_vc)
                    tail = m.group(1) if m else orig_vc.split("-", 2)[-1]
                if not tail:
                    tail = cur_nm

                new_vc = f"BCS-{my_prefix}-{tail}" if my_prefix else orig_vc

                dims_t = parse_dims_str(b["dims"]) if b else None
                dp = floor_price(b["dp"]) if (b and b["dp"] is not None) else None

                file_items.append({
                    "nmId": int(cur_nm),
                    "vendorCode": new_vc,
                    "price": dp,
                    "length": dims_t[0] if dims_t else None,
                    "width": dims_t[1] if dims_t else None,
                    "height": dims_t[2] if dims_t else None,
                    "weight": dims_t[3] if dims_t else None,
                    "stock": 999,
                    "cn": cn,
                    "orig_vc": orig_vc,
                    "orig_nm": tail,
                    "raw_cur_nm": cur_nm_raw,
                })
        else:
            # 普通 CSV 格式
            for r in rows:
                nm_cand = r.get("nmId") or r.get("sku") or r.get("nm") or r.get("WB商品码")
                if nm_cand and str(nm_cand).isdigit():
                    file_items.append({
                        "nmId": int(nm_cand),
                        "price": float(r["price"]) if r.get("price") else None,
                        "vendorCode": r.get("vendorCode") or r.get("vc"),
                        "cn": r.get("cn") or r.get("商品中文名", ""),
                    })
        return file_items, raw_nms

    # 普通文本文件
    for enc in ["utf-8-sig", "gb18030", "gbk"]:
        try:
            with open(file_path, "r", encoding=enc) as f:
                for line in f:
                    line = line.strip()
                    if line and line.isdigit():
                        raw_nms.append(line)
            if raw_nms:
                break
        except Exception:
            continue

    return file_items, raw_nms
