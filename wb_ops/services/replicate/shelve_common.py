# -*- coding: utf-8 -*-
"""
wb_ops 上架核心公共工具与解析器 (shelve_common)

为新上架接口 (shelve_new) 与旧上架接口 (shelve_old) 提供统一的参数解析、
包装数据提取、前缀推导、仓库路由、查重与审计日志记录。
"""
import copy
import csv
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import quote

from wb_ops.domain.models import ShelveItem
from wb_ops.adapters import bcs_client as bcs
from wb_ops.adapters import wb_client
from wb_ops.framework.safe_io import safe_load_json, atomic_dump_json
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops import config
from wb_ops import common
from .wb_card import (
    fetch_card_json,
    parse_package_info,
    boss_pkg_map,
    extract_vc_prefix,
    random_prefix,
    resolve_vendor_prefix,
    resolve_replicate_package,
    basket_base,
)

DETAIL_URL_TPL = (
    "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
    "?appType=1&curr=rub&dest=-1257786&spp=30&hide_vflags=4294967296"
    "&hide_dtype=15&mtype=257&lang=ru&ab_testing=false&nm={nm}"
)

DEFAULT_STOCK = 999
RE_REGION = re.compile(r"подольск|электросталь|хоругвино|колчанино|восток|север|юг", re.I)
RECORDS_JSON = os.path.join(config.STATE_DIR, "复制上架记录.json")
KNOWN_WAREHOUSES_JSON = os.path.join(config.STATE_DIR, "known_warehouses.json")

_warehouse_cache: Dict[int, int] = {}


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


def load_known_warehouses() -> Dict[int, int]:
    """读取账号级兜底仓库表（data/state/known_warehouses.json）"""
    if not os.path.exists(KNOWN_WAREHOUSES_JSON):
        return {}
    data = safe_load_json(KNOWN_WAREHOUSES_JSON, default={})
    if not isinstance(data, dict):
        return {}
    out: Dict[int, int] = {}
    for k, v in data.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def main_warehouse(sid: int, shops_data: Optional[Dict[int, Any]] = None) -> Optional[int]:
    """获取指定店铺的主发货仓库（莫斯科仓 > 俄罗斯地区仓 > 兜底配置表）"""
    if sid in _warehouse_cache:
        return _warehouse_cache[sid]
    wh_id = bcs.default_warehouse_id(sid)
    if wh_id is None:
        whs = bcs.fetch_warehouses(sid)
        wh_id = next(
            (w["id"] for w in whs if RE_REGION.search(w.get("name") or "") and "成都" not in (w.get("name") or "")),
            None,
        )
    if wh_id is None:
        wh_id = load_known_warehouses().get(sid)
    if wh_id is not None:
        _warehouse_cache[sid] = wh_id
    return wh_id


def load_records() -> Dict[str, Any]:
    """读取上架记录（键包含 vc 与原始 nmId）"""
    return safe_load_json(RECORDS_JSON, default={})


def save_records(records: Dict[str, Any]):
    """原子写入上架记录"""
    atomic_dump_json(RECORDS_JSON, records, indent=2, use_lock=True)


def record_shelve_success(records: Dict[str, Any], item: ShelveItem, target_sids: List[int]):
    """记录上架成功条目（同时记录 vc、nm_id 与原始 k 三条防线，确保跨账号/跨接口查重一致性）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nm_key = str(item.nm_id)
    k_key = str(item.extra.get("k")) if item.extra and item.extra.get("k") else None
    for sid in target_sids:
        s_sid = str(sid)
        records.setdefault(nm_key, {})[s_sid] = now_str
        if k_key:
            records.setdefault(k_key, {})[s_sid] = now_str
        if item.vendor_code:
            records.setdefault(item.vendor_code, {})[s_sid] = now_str


def vc_exists_in_shop(vc: str, sid: int, records: Optional[Dict[str, Any]] = None) -> bool:
    """查重：先查本地提交记录，再调 BCS 接口全状态查重 (filter=ALL)"""
    if records and str(sid) in (records.get(vc) or {}):
        return True
    url = (
        f"{bcs.client.base_url}/shopKeeper/productList/list?filter=ALL&pageNum=1&pageSize=10"
        f"&shopId={sid}&vendorCodeMulti={vc}"
    )
    try:
        d = bcs.client.get(url)
        return bool(d.get("code") == 200 and d.get("rows"))
    except Exception:
        return False


def generate_image_urls(nm_id: int, photo_count: Optional[int] = None) -> List[str]:
    """生成 WB 商品 CDN 高清大图链接列表"""
    count = photo_count or 1
    urls = [f"{basket_base(nm_id)}/images/big/{i}.webp" for i in range(1, count + 1)]
    if not urls:
        urls = [f"{basket_base(nm_id)}/images/big/1.webp"]
    return urls


def fetch_wb_detail_bcs(nm_id: int) -> Optional[Dict[str, Any]]:
    """通过 BCS 代理获取 WB 商品原生详情 (product 实体)，失败返回 None"""
    wb_url = DETAIL_URL_TPL.format(nm=nm_id)
    url = f"{bcs.client.base_url}/wb/api/proxy/common?url={quote(wb_url, safe='')}"
    try:
        d = bcs.client.get(url)
        if d.get("code") != 200:
            return None
        products = (d.get("data") or {}).get("products") or []
        return products[0] if products else None
    except Exception:
        return None


def build_synthetic_detail(
    card_info: Optional[Dict[str, Any]],
    nm_id: int,
    title: str = "",
    weight: float = 0.0,
    vendor_code: str = "",
    price_str: str = "0.00",
    subject_id: Optional[int] = None,
    parent_id: Optional[int] = None,
) -> Dict[str, Any]:
    """根据 card.json 拼装合成 detail product 实体（供旧接口 /system/wbCollection/wb/new 兜底）"""
    ci = card_info or {}
    data = ci.get("data") or {}
    subj_id = (
        subject_id
        or ci.get("subjectId")
        or ci.get("subject_id")
        or data.get("subject_id")
        or 0
    )
    p_id = (
        parent_id
        or ci.get("subjectParentId")
        or ci.get("subject_root_id")
        or data.get("subject_root_id")
        or 0
    )
    sizes = [{"origName": 0, "name": "", "price": price_str, "vendorCode": vendor_code}]
    return {
        "id": int(nm_id),
        "root": ci.get("imt_id") or 0,
        "brand": (ci.get("selling") or {}).get("brand_name") or "",
        "brandId": 0,
        "colors": ci.get("colors") or [],
        "subjectId": subj_id,
        "subjectParentId": p_id,
        "name": title or ci.get("imt_name") or "",
        "pics": (ci.get("media") or {}).get("photo_count") or 0,
        "weight": weight or 0,
        "sizes": sizes,
    }



def resolve_item_details(
    item: ShelveItem,
    mapping_state: Optional[Dict[str, Any]] = None,
    pmap: Optional[Dict[str, Any]] = None,
    boss_pkg: Optional[Dict[str, Any]] = None,
    card_cache: Optional[Dict[int, Any]] = None,
) -> Tuple[Optional[ShelveItem], Optional[str]]:
    """
    智能补全上架商品缺失字段：
    1. 校验 nm_id
    2. 中文名自动补全（若空则查本地映射表）
    3. 价格自动补全（指定价格 > 映射表在架价/双倍价 > 价格表售价）
    4. 前缀码与完整 VC 自动推导（指定完整 VC 优先；或指定前缀码；或中文名价格表前缀；或随机 4 位）
    5. 尺寸重量自动补全（显式传入 > 价格映射表 > 价格表 > card.json 包装组）
    6. 标题与图片提取（从 card.json 获取并组装）
    返回: (resolved_item, None) 或 (None, err_msg)
    """
    nm_id = item.nm_id
    if not nm_id or nm_id <= 0:
        return None, f"无效的 WB商品码: {nm_id}"

    card_cache = card_cache if card_cache is not None else {}
    mapping_state = mapping_state if mapping_state is not None else {}
    boss_pkg = boss_pkg if boss_pkg is not None else {}
    pmap = pmap if pmap is not None else {}

    # 构建快速 nmId -> 映射表行索引
    nm_str = str(nm_id)
    matched_map_row = None
    for vc_k, row in mapping_state.items():
        row_nm = str(row.get("nmId") or "").strip()
        if row_nm == nm_str or vc_k == nm_str or vc_k.endswith(f"-{nm_str}"):
            matched_map_row = row
            break

    # 1. 中文名补全
    cn = item.cn.strip()
    if not cn and matched_map_row:
        cn = matched_map_row.get("cn") or ""

    # 2. 完整 VC 与前缀码决策
    vc = (item.vendor_code or "").strip()
    prefix = (item.prefix or "").strip()

    if vc:
        # 指定了完整 VC
        if not prefix:
            extracted = extract_vc_prefix(vc)
            prefix = f"BCS-{extracted}" if extracted else "BCS-CUSTOM"
        elif not prefix.startswith("BCS-"):
            prefix = f"BCS-{prefix}"
    else:
        # 未指定完整 VC，按规则推导前缀并拼接 BCS-{prefix}-{nmId}
        if prefix:
            prefix_clean = prefix.upper().replace("BCS-", "").strip()
            prefix = f"BCS-{prefix_clean}"
        else:
            p_code, _ = resolve_vendor_prefix(cn, matched_map_row.get("goodsCode") if matched_map_row else None, pmap)
            prefix = p_code
        vc = f"{prefix}-{nm_id}"

    # 3. 价格决策
    price = item.price
    if price is None or price <= 0:
        if matched_map_row:
            sp = matched_map_row.get("shop_price")
            dp = matched_map_row.get("dp")
            if sp not in (None, "") and float(sp) > 0:
                price = float(sp)
            elif dp not in (None, "") and float(dp) > 0:
                price = math.floor(float(dp))
        if (price is None or price <= 0) and cn:
            try:
                boss_items = MappingRepository.load_boss()
                for b_it in boss_items:
                    if b_it.get("cn") == cn and b_it.get("dp") is not None:
                        price = math.floor(float(b_it["dp"]))
                        break
            except Exception:
                pass
    if price is None or price <= 0:
        return None, f"商品 {nm_id} 价格缺失且无法从映射表自动匹配，请通过 --price 指定"

    # 4. 包装尺寸与重量决策
    l, w, h, wt = item.length, item.width, item.height, item.weight
    valid_dims = (
        l is not None and l > 0 and
        w is not None and w > 0 and
        h is not None and h > 0 and
        wt is not None and wt > 0
    )

    card_info = None
    if nm_id in card_cache:
        card_info = card_cache[nm_id]

    if not valid_dims:
        # 尝试查映射表 / 价格表 / card.json
        dims, err = resolve_replicate_package(vc, cn, nm_id, mapping_state, card_cache)
        if err or not dims:
            # 尝试直接抓 card.json
            if card_info is None:
                card_info = fetch_card_json(nm_id)
                card_cache[nm_id] = card_info
            if card_info:
                pkg = parse_package_info(card_info)
                l = l or pkg.get("length")
                w = w or pkg.get("width")
                h = h or pkg.get("height")
                wt = wt or pkg.get("weight")
            if (not l or not w or not h or not wt) and cn:
                bp = boss_pkg.get(cn)
                if bp:
                    bl, bw, bh, bwt = bp
                    l = l or bl
                    w = w or bw
                    h = h or bh
                    wt = wt or bwt
            if l and w and h and wt:
                l, w, h, wt = math.ceil(float(l)), math.ceil(float(w)), math.ceil(float(h)), round(float(wt), 3)
            else:
                return None, f"商品 {nm_id} 包装尺寸重量缺失（L={l}, W={w}, H={h}, 重量={wt}），请通过 --dims 指定"
        else:
            l, w, h, wt = dims
    else:
        l, w, h, wt = math.ceil(float(l)), math.ceil(float(w)), math.ceil(float(h)), round(float(wt), 3)

    # 5. 标题与图片提取（针对旧接口或详情需要）
    if card_info is None:
        card_info = fetch_card_json(nm_id)
        card_cache[nm_id] = card_info

    title = item.title or (card_info.get("imt_name") if card_info else "") or ""
    photo_count = ((card_info.get("media") or {}).get("photo_count")) if card_info else 1
    gen_imgs = ";".join(generate_image_urls(nm_id, photo_count))
    images = item.images or gen_imgs
    main_image = item.main_image or images.split(";")[0]

    resolved = ShelveItem(
        nm_id=nm_id,
        price=round(float(price), 2),
        vendor_code=vc,
        prefix=prefix,
        length=l,
        width=w,
        height=h,
        weight=wt,
        stock=item.stock if item.stock > 0 else DEFAULT_STOCK,
        target_shops=copy.deepcopy(item.target_shops),
        cn=cn,
        title=title,
        main_image=main_image,
        images=images,
        extra={
            **item.extra,
            "card_info": card_info,
        }
    )
    return resolved, None


def create_shelve_csv(prefix_name: str = "上架") -> Tuple[Any, Any, str]:
    """初始化上架 CSV 执行记录文件"""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_path = os.path.join(config.LOG_DIR, f"{prefix_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_file)
    writer.writerow(["时间", "WB商品码", "vendorCode", "中文名", "价格", "包装长", "包装宽", "包装高", "毛重", "目标店", "库存", "结果", "原因"])
    return csv_file, writer, csv_path


def load_boss_shelve_dict() -> Dict[str, Dict[str, Any]]:
    """加载商品价格表，返回 {cn: {sku, cn, dp, dims, prefix}}"""
    import openpyxl
    if not os.path.exists(config.BOSS_XLSX):
        return {}
    wb = openpyxl.load_workbook(config.BOSS_XLSX, data_only=True)
    ws = wb.active
    boss = {}
    for r in range(2, ws.max_row + 1):
        cn = ws.cell(r, 3).value
        if cn:
            cn = str(cn).strip()
            boss[cn] = {
                "sku": ws.cell(r, 2).value,
                "cn": cn,
                "dp": ws.cell(r, 4).value,
                "dims": str(ws.cell(r, 5).value or "").strip(),
                "prefix": str(ws.cell(r, 7).value or "").strip().upper(),
            }
    wb.close()
    return boss


def load_shelve_file(file_path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    统一解析批量上架输入文件（支持 .json, .csv, .txt）。
    自动兼容：
      1. .json: List[dict] 结构或 List[nm] 数组
      2. .csv: 
         - 订单登记/筛选非我店订单 CSV（含 '商品中文名' 与 '现在商品的nmId' / 'VC' 列），
           自动关联商品价格表推导新 VC (BCS-{前缀}-{原尾段})、双倍售价与包装尺寸；
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
            boss_data = load_boss_shelve_dict()
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
                dp = float(b["dp"]) if (b and b["dp"] is not None) else None

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


def _display_width(s: Any) -> int:
    """计算字符串终端显示列宽（全角/CJK 占 2 列，半角占 1 列）"""
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in str(s))


def _pad_str(s: Any, width: int, align: str = "<") -> str:
    """对齐字符串以满足指定显示宽度"""
    s = str(s)
    dw = _display_width(s)
    pad = max(0, width - dw)
    return (" " * pad + s) if align == ">" else (s + " " * pad)


def print_shelve_preview(items: List[ShelveItem], target_shops_default: List[int]):
    """打印格式化预览表格（动态适配中英文字符宽度与原始VC溯源）"""
    if not items:
        print("\n[预览] 无待上架商品")
        return

    has_orig = any(it.extra and (it.extra.get("orig_vc") or it.extra.get("orig_nm")) for it in items)

    col_nm = 14
    col_vc = max(32, max((len(it.vendor_code or "") for it in items), default=30) + 2)
    col_orig = 32 if has_orig else 0
    col_cn = 16
    col_price = 10
    col_dims = 22
    col_stock = 6
    col_shops = 16

    header = (
        _pad_str("WB现商品码", col_nm) +
        _pad_str("上架新VC", col_vc) +
        (_pad_str("原始VC", col_orig) if has_orig else "") +
        _pad_str("中文名", col_cn) +
        _pad_str("售价(元)", col_price) +
        _pad_str("包装规格(长*宽*高/重)", col_dims) +
        _pad_str("库存", col_stock) +
        _pad_str("目标店铺", col_shops)
    )

    sep_len = _display_width(header)
    print("\n" + "=" * sep_len)
    print(header)
    print("-" * sep_len)

    for it in items:
        sids = it.target_shops or target_shops_default
        shops_str = ",".join(map(str, sids))
        dims_str = f"{it.length}*{it.width}*{it.height}/{it.weight}kg"
        p_val = float(it.price or 0.0)
        p_str = str(int(p_val)) if p_val.is_integer() else f"{p_val:.2f}"
        orig_vc = (it.extra.get("orig_vc") or it.extra.get("orig_nm")) if it.extra else "-"

        row = (
            _pad_str(it.nm_id, col_nm) +
            _pad_str(it.vendor_code or "-", col_vc) +
            (_pad_str(orig_vc or "-", col_orig) if has_orig else "") +
            _pad_str(it.cn or "-", col_cn) +
            _pad_str(p_str, col_price) +
            _pad_str(dims_str, col_dims) +
            _pad_str(it.stock, col_stock) +
            _pad_str(shops_str, col_shops)
        )
        print(row)

    print("=" * sep_len)

