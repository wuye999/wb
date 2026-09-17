# -*- coding: utf-8 -*-
"""
wb_ops 马帮库存登记：拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」
数据源（2026-09-10 抓包实测：api/网络请求/马帮获取库存SKU的库存.har）：
- POST aamz.mabangerp.com/index.php?mod=stock.getStockList（aamz cookie 鉴权）
  body: searchKey=Stock_stockSku&operate=likeStart&status=3&stockOrderby=a.stockQuantity desc
  （page/rowsPerPage 留空 = 返回全量）→ stockData[]：stockSku/nameCN/stockQuantity/statusText/stockPicture
写入：全量重建（先清空表再写入），图片下载后经 +record-upload-attachment 传「图」附件列
约定：默认 dry-run；--apply 真正执行；库存总量可为负，原样写入
"""
import json
import os
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor


from wb_ops import common
from wb_ops import credentials
from wb_ops.adapters import mabang_client as mbc   # 马帮 HTTP 原语（唯一实现，勿经服务层转发模块调用）
from .feishu_register import _lark, resolve_base, resolve_table


DATE_FMT = "%Y-%m-%d"
# 图片附件列候选名（本表实际列名为「图」，其余为兼容）
PIC_FIELD_CANDIDATES = ("图", "图片", "图片附件")


def _sv(v):
    """单元格值 → 去空格字符串（兼容 list/dict 富文本）"""
    if v is None:
        return ""
    if isinstance(v, list):
        v = "".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
    return str(v).strip()


def _to_num(v):
    """尽力转数值；失败返回 None（兼容 "‑2" 这类字符串库存）"""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _parse_day(s, flag):
    """YYYY-M-D → date（strptime 自动补零规范化）；空串返回 None，非法抛 ValueError"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, DATE_FMT).date()
    except ValueError:
        raise ValueError(f"{flag} 日期格式应为 YYYY-MM-DD，实际收到: {s!r}")


def fetch_stock_list(cred):
    """[薄转发] 拉取马帮全部库存 SKU（HTTP 细节已下沉 adapters/mabang_client.fetch_stock_list）

    返回 list[dict{stockSku,nameCN,stockQuantity,statusText,stockPicture}]。
    """
    return mbc.fetch_stock_list(cred)


def _record_list_all(base_token, table_id):
    """分页读全表 → list[dict]（lark-cli ndjson 单次上限 2000，按 manifest 翻页）"""
    rows, offset, page = [], 0, 0
    while True:
        out = os.path.join(tempfile.gettempdir(), f"_mbstock_rl_{os.getpid()}_{page}.ndjson")
        man_path = out.replace(".ndjson", ".manifest.json")
        for p in (out, man_path):
            if os.path.exists(p):
                os.remove(p)
        man = _lark(["+record-list", "--base-token", base_token, "--table-id", table_id,
                     "--limit", "2000", "--offset", str(offset),
                     "--format", "ndjson", "--output", out, "--overwrite"])
        if os.path.exists(out):
            with open(out, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        for p in (out, man_path):
            try:
                os.remove(p)
            except OSError:
                pass
        if not man.get("has_more"):
            return rows
        nxt = man.get("next_offset")
        if nxt in (None, ""):
            raise RuntimeError(f"record-list 报告 has_more 但未给 next_offset"
                               f"（已读 {len(rows)} 条），为避免漏读已中止")
        offset, page = int(nxt), page + 1
        time.sleep(0.3)


def _read_all_record_ids(base_token, table_id):
    """读全表现有记录 ID 列表（清空前用；分页防截断）"""
    return [str(d["record_id"]) for d in _record_list_all(base_token, table_id)
            if d.get("record_id")]


def _create_records(base_token, table_id, items):
    """分批创建记录，返回 [(record_id, item)]（顺序对应）"""
    pairs = []
    payload_rows = [{"库存SKU": it["stockSku"] or None,
                     "商品中文名": it["nameCN"] or None,
                     "库存总量": str(it["stockQuantity"]) if it["stockQuantity"] is not None else None,
                     "状态": it["statusText"] or None} for it in items]
    for i in range(0, len(payload_rows), 200):
        chunk = payload_rows[i:i + 200]
        d = _lark(["+record-batch-create", "--base-token", base_token,
                   "--table-id", table_id],
                  payload={"create_records": chunk})
        rid_list = d.get("record_id_list") or []
        chunk_items = items[i:i + 200]
        if len(rid_list) != len(chunk_items):
            raise RuntimeError(
                f"record-batch-create 第 {i // 200 + 1} 批返回 {len(rid_list)}/{len(chunk_items)} 条，"
                f"数量不符已中止（请核对飞书表后重跑 mabang-stock-register --apply）")
        for rid, it in zip(rid_list, chunk_items):
            pairs.append((str(rid), it))
        time.sleep(0.5)
    return pairs


# ---------------- 每日新订单量列 ----------------

def _field_id_by_name(base_token, table_id, name):
    """field-list 查列 id（走 fr._lark，二进制解析已验证）。
    查询失败直接抛出 —— 不再把「查询失败」当「列不存在」，否则会误建同名列
    （飞书报 800010205 unique field name，且发生在数百条写入之后）。"""
    d = _lark(["+field-list", "--base-token", base_token,
               "--table-id", table_id])
    # fr._lark 已剥壳：字段列表直接在顶层 d["fields"]
    for f in d.get("fields") or []:
        if f.get("name") == name:
            return f.get("id")
    return None


def _resolve_attachment_field(base_token, table_id):
    """定位附件列的真实列名：候选名优先，其次退到任意 attachment 类型列；都没有返回 None"""
    d = _lark(["+field-list", "--base-token", base_token, "--table-id", table_id])
    fields = d.get("fields") or []
    by_name = {f.get("name"): f for f in fields}
    for nm in PIC_FIELD_CANDIDATES:
        if nm in by_name:
            return nm
    for f in fields:
        if f.get("type") == "attachment":
            return f.get("name")
    return None


from .mabang_stock_daily import (
    read_orders_daily,
    read_stock_records,
    run_daily,
)


def run(args):
    common.ensure_utf8_stdout()
    cred = mbc.get_mabang_cred()
    url = args.url or credentials.get().feishu_base_url()
    if not url:
        print("[错误] 未提供 --url 且配置 feishu.base_url 缺失")
        return 1
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, args.table)
    print(f"[表格] base_token={base_token} table_id={table_id}（{args.table}）")

    # 附件列预检（放在任何破坏性写入之前；字段查询失败不静默）
    try:
        pic_field = _resolve_attachment_field(base_token, table_id)
    except Exception as e:
        print(f"[错误] 读取「{args.table}」列信息失败（未改动任何记录）: {str(e)[:200]}")
        return 1
    if not pic_field:
        print(f"[错误]「{args.table}」未找到图片附件列（候选：{'/'.join(PIC_FIELD_CANDIDATES)}），"
              f"已中止，未改动任何记录。")
        return 1
    print(f"[图片列] {pic_field}")

    print("\n[拉取] 马帮库存 SKU 列表（stock.getStockList，aamz 域）...")
    stocks = fetch_stock_list(cred)
    neg = [s for s in stocks if (_to_num(s["stockQuantity"]) or 0) < 0]
    print(f"[数据] 共 {len(stocks)} 条库存 SKU；负库存 {len(neg)} 条")
    for s in stocks[:10]:
        print(f"  {s['stockSku']} | {s['nameCN']} | 库存={s['stockQuantity']} | {s['statusText']}")
    if len(stocks) > 10:
        print(f"  ... 其余 {len(stocks) - 10} 条")

    if not args.apply:
        print("\n（dry-run 未写入；确认无误后加 --apply 全量重建）")
        return 0

    # 1) 清空现有记录
    old_ids = _read_all_record_ids(base_token, table_id)
    print(f"\n[清空] 删除现有记录 {len(old_ids)} 条...")
    for i in range(0, len(old_ids), 100):
        cmd = ["+record-delete", "--base-token", base_token, "--table-id", table_id, "--yes"]
        for rid in old_ids[i:i + 100]:
            cmd += ["--record-id", rid]
        _lark(cmd)
        time.sleep(0.3)

    # 2) 写入新记录
    print(f"[写入] 创建 {len(stocks)} 条记录...")
    pairs = _create_records(base_token, table_id, stocks)
    if len(pairs) != len(stocks):
        print(f"[错误] 创建记录 {len(pairs)}/{len(stocks)} 条，数量不符已中止（请重跑本命令）")
        return 1
    print(f"  创建成功 {len(pairs)} 条")

    # 3) 图片附件（并行下载，串行上传）
    with_pic = [p for p in pairs if p[1]["stockPicture"]]
    print(f"[图片] 需上传 {len(with_pic)} 张（并行下载，串行上传）...")
    tmpdir = tempfile.mkdtemp(prefix="mbstock_")
    paths = {}

    def _dl(pair):
        rid, it = pair
        try:
            path = os.path.join(tmpdir, f"{rid}.jpg")
            if mbc.download_file(it["stockPicture"], path):
                return rid, path
            return rid, None
        except Exception:
            return rid, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for rid, path in ex.map(_dl, with_pic):
            if path:
                paths[rid] = path

    ok_img, fail_img = 0, []
    for rid, it in pairs:
        path = paths.get(rid)
        if not path:
            if it["stockPicture"]:
                fail_img.append(it["stockSku"])
            continue
        try:
            _lark(["+record-upload-attachment", "--base-token", base_token,
                   "--table-id", table_id, "--record-id", rid,
                   "--field-id", pic_field, "--file", path])
            ok_img += 1
        except Exception as e:
            fail_img.append(f"{it['stockSku']}({str(e)[:40]})")
        time.sleep(0.2)
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n[完成] 全量重建：{len(pairs)} 条记录 | 图片成功 {ok_img} / 失败 {len(fail_img)}")
    if fail_img:
        print("  图片失败:", "、".join(map(str, fail_img[:8])))
    print("[汇总] 负库存 " + str(len(neg)) + " 条: " +
          ("、".join(f"{s['stockSku']}({s['stockQuantity']})" for s in neg[:10]) if neg else "无"))
    return 0
