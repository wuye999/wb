"""BCS 重复商品行去重；只保留可确认的完整原行，不拼接业务字段。"""
import json
from typing import Any

from sellerops.domain.snapshot_values import positive_id


_RECORD_FIELDS = {'id', 'createTime', 'updateTime', 'createBy', 'updateBy'}


def _business_values(row: dict[str, Any]) -> dict[str, str]:
    return {key: json.dumps(value, sort_keys=True, ensure_ascii=False)
            for key, value in row.items()
            if key not in _RECORD_FIELDS and value is not None
            and not (isinstance(value, (str, list, dict)) and not value)}


def _complete_row(rows: list[dict[str, Any]], shop_id: int, vc: str) -> dict[str, Any]:
    if len(rows) == 1:
        return rows[0]
    if len({bool(row.get('trashedAt')) for row in rows}) != 1:
        raise ValueError(f'店铺{shop_id} 商品{vc}的重复记录在架/回收站状态冲突')
    expected: dict[str, str] = {}
    values = [_business_values(row) for row in rows]
    for fields in values:
        for key, value in fields.items():
            if key in expected and expected[key] != value:
                raise ValueError(f'店铺{shop_id} 商品{vc}的重复记录字段冲突：{key}')
            expected[key] = value
    candidates = [row for row, fields in zip(rows, values) if fields == expected]
    if not candidates:
        raise ValueError(f'店铺{shop_id} 商品{vc}的重复记录没有可保留的完整原行')
    # 只有业务字段一致的完整行才按 BCS 元信息决定代表行；不以时间覆盖冲突。
    return max(candidates, key=lambda row: (positive_id(row.get('updateTime')) or 0,
                                            positive_id(row.get('id')) or 0))


def deduplicate_bcs_products(rows: list[dict[str, Any]], shop_id: int) -> list[dict[str, Any]]:
    """同店相同 nmId 和完整 VC 合并；标识或非空业务值冲突时拒绝整批。"""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    vc_ids: dict[str, int | None] = {}
    nm_vcs: dict[int, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f'店铺{shop_id} 商品列表包含无效行')
        if row.get('shopId') is not None and str(row['shopId']) != str(shop_id):
            raise ValueError(f'店铺{shop_id} 商品列表包含其他店铺数据')
        raw_vc = row.get('vendorCode') or row.get('goodsCode') or ''
        vc = raw_vc.strip() if isinstance(raw_vc, str) else ''
        nm = positive_id(row.get('nmId') or row.get('nmID'))
        if vc in vc_ids and (nm is None or vc_ids[vc] != nm):
            raise ValueError(f'店铺{shop_id} 商品{vc}的重复记录 nmId 缺失或冲突')
        if nm in nm_vcs and (not vc or nm_vcs[nm] != vc):
            raise ValueError(f'店铺{shop_id} 商品{nm}的重复记录 VC 缺失或冲突')
        if vc:
            vc_ids[vc] = nm
        if nm is not None:
            nm_vcs[nm] = vc
        # 单条缺失标识的记录保留原状，仍由具体动作检查必要字段。
        key = (nm, vc) if nm is not None and vc else ('unidentified', index)
        groups.setdefault(key, []).append(row)
    return [_complete_row(group, shop_id, str(key[-1])) for key, group in groups.items()]
