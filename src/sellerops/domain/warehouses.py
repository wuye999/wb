"""按本店仓库列表选择唯一可用的莫斯科仓库。"""
from sellerops.domain.snapshot_values import positive_id


def moscow_warehouse(rows: list[dict]) -> dict:
    candidates = [row for row in rows if "莫斯科" in str(row.get("name") or "")
                  and "成都" not in str(row.get("name") or "") and not row.get("isDeleting")]
    if len(candidates) == 1 and not candidates[0].get("isProcessing"):
        row = candidates[0]
        wid = positive_id(row.get("id"))
        if wid and str(row["id"]).strip() == str(wid):
            return {"id": wid, "name": row["name"].strip()}
    raise ValueError("无法确认本店唯一可用的莫斯科仓库名称和 id")
