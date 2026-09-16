# -*- coding: utf-8 -*-
"""
wb_ops 映射与品名归属仓储 (MappingRepository)
统一管理全局 VC 归属池、纠偏改名、前缀映射及排除清单的安全持久化与内存缓存。
"""
import os
import re
from typing import Dict, Any, Tuple, Callable
from .. import config
from ..framework.safe_io import atomic_dump_json, safe_load_json


class MappingRepository:
    """映射数据仓储"""

    @staticmethod
    def load_vc_known() -> Dict[str, Any]:
        """加载全局已知 VC 归属池 (vc_known.json)"""
        return safe_load_json(config.VC_KNOWN_JSON, default={})

    @staticmethod
    def save_vc_known(data: Dict[str, Any]):
        """原子保存全局已知 VC 归属池"""
        atomic_dump_json(config.VC_KNOWN_JSON, data, indent=2, use_lock=True)

    @staticmethod
    def load_vc_override() -> Dict[str, Any]:
        """加载全局 VC 纠偏改名清单 (vc_override.json)"""
        return safe_load_json(config.VC_OVERRIDE_JSON, default={})

    @staticmethod
    def save_vc_override(data: Dict[str, Any]):
        """原子保存全局 VC 纠偏改名清单"""
        atomic_dump_json(config.VC_OVERRIDE_JSON, data, indent=2, use_lock=True)

    @staticmethod
    def load_prefix_map() -> Dict[str, Any]:
        """从商品价格表加载 4 位前缀码映射"""
        if not os.path.exists(config.BOSS_XLSX):
            return {}
        try:
            import openpyxl
            wb = openpyxl.load_workbook(config.BOSS_XLSX, data_only=True)
            ws = wb["Sheet1"]
            pmap = {}
            for row in ws.iter_rows(min_row=2):
                sku = str(row[1].value).strip() if row[1].value else ""
                cn = str(row[2].value).strip() if row[2].value else ""
                if not sku and not cn:
                    continue
                prefix = str(row[6].value).strip().upper() if len(row) > 6 and row[6].value else ""
                if not prefix:
                    continue
                dp = row[3].value
                if prefix not in pmap:
                    pmap[prefix] = {"sku": sku, "cn": cn, "dp": dp}
            wb.close()
            return pmap
        except Exception:
            return {}

    @classmethod
    def build_vc_resolver(cls) -> Tuple[Callable[[str, str], str], Dict[str, str]]:
        """构建毫秒级商品中文名反查解析器。
        
        解析优先级:
        1. vc_override.json (最高优先级纠偏)
        2. vc_known.json (历史已知映射)
        3. 4位前缀码映射 (prefix_map 兜底)
        
        返回: (resolve_cn 函数, 全局已加载的 vc_cn 字典)
        """
        vc_cn: Dict[str, str] = {}

        # 1. vc_known.json
        known = cls.load_vc_known()
        for vc, info in known.items():
            if isinstance(info, dict) and info.get("cn"):
                vc_cn[vc.upper()] = info["cn"]

        # 2. vc_override.json
        override = cls.load_vc_override()
        for vc, info in override.items():
            if isinstance(info, dict) and info.get("cn"):
                vc_cn[vc.upper()] = info["cn"]

        # 3. 前缀映射兜底
        prefix_map = cls.load_prefix_map()
        prefix_re = re.compile(config.VC_PREFIX_RE)

        def resolve_cn(vc: str, default_title: str = "") -> str:
            if not vc:
                return default_title
            v_upper = vc.strip().upper()
            if v_upper in vc_cn:
                return vc_cn[v_upper]
            m = prefix_re.match(v_upper)
            if m:
                pref = m.group(1).upper()
                if pref in prefix_map and prefix_map[pref].get("cn"):
                    return prefix_map[pref]["cn"]
            return default_title

        return resolve_cn, vc_cn

    @classmethod
    def load_mapping_state(cls) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """读现有映射表 xlsx → 增量状态（归属关系 + 已排除清单）。
        返回 (state, excluded)：state={vc:{cn,dp,shop_price,discount,nmId,...}}；excluded={vc:原因}。
        映射表不存在 → 空。自动应用 vc_override.json 纠偏记录（优先级最高）。"""
        state, excluded = {}, {}
        if not os.path.exists(config.MAPPING_XLSX):
            return state, excluded
        try:
            import openpyxl
            wb = openpyxl.load_workbook(config.MAPPING_XLSX, data_only=True)
            if "映射总表" in wb.sheetnames:
                ws = wb["映射总表"]
                headers = [str(c.value or "") for c in ws[1]]
                if "vendorCode" in headers:
                    i_cn, i_vc = headers.index("产品中文名"), headers.index("vendorCode")
                    i_dp = headers.index("双倍售价") if "双倍售价" in headers else None
                    i_sp = next((i for i, h in enumerate(headers)
                                 if re.match(r"^店铺\d+价格\(CNY\)$", h)), None)
                    i_disc = headers.index("折扣%") if "折扣%" in headers else None
                    i_nm = headers.index("WB商品码") if "WB商品码" in headers else None
                    i_l = headers.index("尺寸长(cm)") if "尺寸长(cm)" in headers else None
                    i_w = headers.index("尺寸宽(cm)") if "尺寸宽(cm)" in headers else None
                    i_h = headers.index("尺寸高(cm)") if "尺寸高(cm)" in headers else None
                    i_wt = headers.index("毛重(kg)") if "毛重(kg)" in headers else None
                    for r in ws.iter_rows(min_row=2, values_only=True):
                        vc = r[i_vc]
                        if vc:
                            state[vc] = {
                                "cn": r[i_cn] or "",
                                "dp": r[i_dp] if i_dp is not None else None,
                                "shop_price": r[i_sp] if i_sp is not None else None,
                                "discount": r[i_disc] if i_disc is not None else None,
                                "nmId": (r[i_nm] if i_nm is not None else None),
                                "L": (r[i_l] if i_l is not None else None),
                                "W": (r[i_w] if i_w is not None else None),
                                "H": (r[i_h] if i_h is not None else None),
                                "weight": (r[i_wt] if i_wt is not None else None),
                            }
            if "已排除清单" in wb.sheetnames:
                ws2 = wb["已排除清单"]
                for r in ws2.iter_rows(min_row=2, values_only=True):
                    if r[0]:
                        excluded[r[0]] = r[1] or "非货盘商品（人工排除）"
            wb.close()
        except Exception:
            pass

        # 自动应用全局纠偏更正（高优先级）
        overrides = cls.load_vc_override()
        for vc, ov in overrides.items():
            if vc in state and isinstance(ov, dict) and ov.get("cn"):
                state[vc]["cn"] = ov["cn"]

        return state, excluded

