"""按配置的关键词选择本店唯一可用的 WB 仓库。"""
from sellerops.domain.snapshot_values import positive_id


def select_warehouse(rows: list[dict], keyword: str) -> dict:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("请在 data/credentials.json 中配置 wb.warehouse_keyword")
    available = [row for row in rows if not row.get("isDeleting")]
    candidates = [row for row in available if isinstance(row.get("name"), str) and keyword in row["name"]]
    if len(candidates) != 1:
        names = "、".join(str(row["name"]) for row in available if row.get("name")) or "无"
        raise ValueError(f"已配置的 WB 仓库关键词「{keyword}」命中 {len(candidates)} 个仓库。"
                         f"当前可用仓库：{names}。请在 data/credentials.json 中填写更具体的 wb.warehouse_keyword")
    row = candidates[0]
    wid = positive_id(row.get("id"))
    if row.get("isProcessing") or not wid or str(row["id"]).strip() != str(wid):
        raise ValueError(f"已配置的 WB 仓库「{row['name']}」正在处理中或 id 无效")
    return {"id": wid, "name": row["name"].strip()}
