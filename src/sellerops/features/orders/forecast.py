"""forecast。"""


import time


from sellerops.core import utils as common
from sellerops.integrations.mabang.client import get_mabang_cred as mb_client_get_mabang_cred
from sellerops.integrations.mabang.forecast import get_forecast_logistics as mb_client_get_forecast_logistics
from sellerops.integrations.mabang.forecast import batch_create_forecast as mb_client_batch_create_forecast
from sellerops.integrations.mabang.forecast import upload_forecast_batch as mb_client_upload_forecast_batch
from sellerops.integrations.mabang.forecast import get_forecast_list as mb_client_get_forecast_list
from sellerops.integrations.mabang.forecast import get_handover_channel_value as mb_client_get_handover_channel_value
from sellerops.integrations.mabang.forecast import set_handover as mb_client_set_handover
from sellerops.integrations.mabang.forecast import MabangUploadInterrupted
from sellerops.integrations.mabang.session import MabangAuthError
from sellerops.features.orders.forecast_polling import _resolve_poll_interval
from sellerops.features.orders.forecast_polling import _resolve_upload_timeout
from sellerops.features.orders.forecast_selection import collect_forecast_orders
from sellerops.features.orders.forecast_polling import find_batches
from sellerops.features.orders.forecast_polling import wait_upload_done
from sellerops.features.orders.forecast_selection import write_forecast_csv

FORECAST_STATUS_TAGS = {1: "待上传", 2: "上传中", 3: "预报成功", 99: "预报失败"}


def _record_auth_stop(target: list[dict], batches: list[str], submitted: tuple[str, ...],
                      reason: MabangAuthError) -> None:
    for row in target:
        row["_step_handover"] = "跳过-认证中止"
    accepted = set(submitted)
    report = write_forecast_csv(target, [
        {"batchNo": bn, "状态": "上传已受理，未确认完成" if bn in accepted else "上传未确认",
         "note": str(reason)} for bn in batches])
    if report:
        print(f"[报告] 认证中止前的批次明细: {report}")


def _upload(cred: dict, target: list[dict], batches: list[str]) -> str:
    try:
        return mb_client_upload_forecast_batch(cred, batches)
    except MabangAuthError as exc:
        _record_auth_stop(target, batches, exc.submitted if isinstance(exc, MabangUploadInterrupted) else (), exc)
        raise


def _print_forecast_rows(rows):
    """打印预报批次行（揽货批次号/批次状态/订单总数/批次重量/上传货代/创建人/回传号）。"""
    for b in rows:
        st = int(b.get("status") or 0)
        tag = FORECAST_STATUS_TAGS.get(st, f"状态{st}")
        fl = int(b.get("failNum") or 0)
        print(f"  {b.get('batchNo')} | {b.get('shopId')} | {tag}(status={st}) | "
              f"订单数={b.get('total')} 成功={b.get('successNum')} 失败={fl} | "
              f"货代={b.get('forecastLogisticsName') or '-'} | "
              f"创建人={b.get('employeeName') or '-'} | "
              f"回传号={b.get('handoverNumber') or '-'} | "
              f"批次重量={b.get('orderWeight') or '-'} | 创建={b.get('createTime')}")
        if fl:
            print(f"     [⚠ 告警] 该批次失败 {fl} 单，请核查")

def run_forecast(args):
    common.ensure_utf8_stdout()
    cred_check = mb_client_get_mabang_cred()

    if args.check:
        tabs = [(1, "待预报"), (3, "预报成功"), (99, "预报失败"), (5, "历史预报")]
        labels = dict(tabs)
        status = getattr(args, "status", None)
        pages = int(getattr(args, "pages", 1) or 0)

        if status:
            status = int(status)
            label = labels.get(status, f"status={status}")
            print(f"[查询] {label}(status={status}) 批次列表"
                  f"（{'翻到底' if pages == 0 else f'最多 {pages} 页'}，每页 200）...")
            rows_all, last = [], {}
            page, max_pages = 1, (99 if pages == 0 else pages)
            while page <= max_pages:
                try:
                    rows, st = mb_client_get_forecast_list(cred_check, status=status,
                                                 rows_per_page=200, page=page)
                except Exception as e:
                    print(f"  [错误] 查询失败: {str(e)[:80]}")
                    return 1
                last = st or {}
                rows_all.extend(rows)
                if len(rows) < 200 or (last.get("pageCount") and page >= last["pageCount"]):
                    break
                page += 1
            print(f"  共 {last.get('pageTotal')} 条 第{last.get('pageFrom')}-{last.get('pageTo')}条 "
                  f"{last.get('pageCur')}/{last.get('pageCount')}页")
            if not rows_all:
                print("  （无）")
            else:
                _print_forecast_rows(rows_all)
            return 0

        print("[查询] 预报批次概览（待预报=1 / 预报成功=3 / 预报失败=99 / 历史预报=5）...")
        first, stats0 = {}, {}
        for st_code, _label in tabs:
            try:
                rows, st = mb_client_get_forecast_list(cred_check, status=st_code,
                                             rows_per_page=200, page=1)
                first[st_code] = (rows, st or {})
                if not stats0:
                    stats0 = st or {}
            except MabangAuthError:
                raise
            except Exception as e:
                first[st_code] = ([], {"_err": str(e)[:80]})
        print(f"统计: 待上传 {stats0.get('waitTotal')} / 成功 {stats0.get('succesTotal')} / "
              f"失败 {stats0.get('failTotal')} / 历史 {stats0.get('historyTotal')}")
        for st_code, label in tabs:
            rows, st = first.get(st_code, ([], {}))
            if st.get("_err"):
                print(f"\n▸ status={st_code} {label}   查询失败: {st['_err']}")
                continue
            print(f"\n▸ status={st_code} {label}   共 {st.get('pageTotal')} 条 "
                  f"第{st.get('pageFrom')}-{st.get('pageTo')}条 "
                  f"{st.get('pageCur')}/{st.get('pageCount')}页")
            if not rows:
                print("  （无）")
            else:
                _print_forecast_rows(rows[:3])
                if len(rows) > 3:
                    print(f"  ...（仅列前 3 行；完整明细请用 --status {st_code}）")
        return 1 if any(st.get("_err") for _, st in first.values()) else 0

    target, cred = collect_forecast_orders(args)
    if not target:
        return 0

    n_f = sum(1 for r in target if r["has_forecast"])
    n_c = sum(1 for r in target if r["channel_selected"])
    print(f"[状态] 已预报(已生成批次) {n_f} 单 / 未预报 {len(target) - n_f} 单；"
          f"交运方式已选 {n_c} 单 / 未选择 {len(target) - n_c} 单")
    for r in target:
        r.setdefault("_step_create", ""), r.setdefault("_step_upload", "")
        r.setdefault("_step_handover", ""), r.setdefault("_batch", "")

    if not args.apply:
        todo = [r for r in target if not r["has_forecast"]]
        print(f"\n将生成预报批次的平台单号 {len(todo)} 个（前 10 个）: "
              f"{','.join(str(r['platform_order_id']) for r in todo[:10])}"
              f"{'...' if len(todo) > 10 else ''}")
        print("\n（dry-run 模式未生成/上传/交运；确认无误后加 --apply 执行全链路）")
        return 0

    to_create = [r for r in target if not r["has_forecast"]]
    batch_nos_new = []
    if to_create:
        logistics, channel = mb_client_get_forecast_logistics(cred)
        print(f"\n[① 生成批次] 物流={logistics} 渠道={channel}；"
              f"提交 {len(to_create)} 个平台单号...")
        pids = [str(r["platform_order_id"]) for r in to_create if r["platform_order_id"]]
        batch_nos_new, msg = mb_client_batch_create_forecast(cred, pids, logistics, channel)
        if not batch_nos_new:
            raise RuntimeError("预报生成未返回批次号，已中止上传与交运")
        print(f"  {msg}；新批次 {len(batch_nos_new)} 个: {','.join(batch_nos_new)}")
        for r in to_create:
            r["_step_create"] = "已生成"
    else:
        print("\n[① 生成批次] 全部订单已预报，跳过（不重复生成）")
        for r in target:
            r["_step_create"] = "跳过-已预报"

    up_timeout = _resolve_upload_timeout(args, cred)
    poll_iv = _resolve_poll_interval(args, cred)
    min_wait = int(getattr(args, "wait", 0) or 0)
    uploaded_nos = []          # 本轮流经上传的批次号（新批次或补传批次）

    if batch_nos_new:
        up_msg = _upload(cred, target, batch_nos_new)
        print(f"[② 上传批次] {len(batch_nos_new)} 个新批次: {up_msg}")
        uploaded_nos = [str(b) for b in batch_nos_new]
        for r in to_create:
            r["_step_upload"] = "已提交上传"
    elif getattr(args, "upload_waiting", False):
        waiting, _stats = mb_client_get_forecast_list(cred, status=1)
        wb_nos = [b["batchNo"] for b in waiting]
        if wb_nos:
            up_msg = _upload(cred, target, wb_nos)
            print(f"[② 上传批次] 补传待上传列表 {len(wb_nos)} 个批次: {up_msg}")
            for r in target:
                r["_step_upload"] = r["_step_upload"] or "补传历史批次"
            uploaded_nos = [str(b) for b in wb_nos]
        else:
            print("[② 上传批次] 待上传列表为空，无需补传")
    else:
        print("[② 上传批次] 无新生成批次（历史批次已上传或用 --upload-waiting 补传），跳过")
        for r in target:
            r["_step_upload"] = r["_step_upload"] or "跳过-无新批次"

    # ③ 轮询等待上传结束（正向判据：命中 status=3 预报成功；status=5 历史归档亦算成功）
    #    节奏：上传后 status=1 预扫描建店铺映射 → 每轮仅 status=3（1 请求/轮）
    #          → status=5 限频兜底 → 超时补映射 + status=99 定性
    to_handover = [r for r in target if not r["channel_selected"]]
    skipped_fail, skipped_upload, up_rows, store_of = [], [], {}, {}
    success_nos, failed_nos, still_nos = set(), set(), set()
    if uploaded_nos:
        try:
            res = wait_upload_done(cred, uploaded_nos, timeout=up_timeout,
                                   interval=poll_iv, min_wait=min_wait)
        except MabangAuthError as exc:
            _record_auth_stop(target, uploaded_nos, tuple(uploaded_nos), exc)
            raise
        up_rows, store_of = res["rows"], res["store_of"]
        success_nos, failed_nos, still_nos = res["success"], res["failed"], res["still"]

        fail_stores = {store_of.get(b) for b in failed_nos if store_of.get(b)}
        still_stores = {store_of.get(b) for b in still_nos if store_of.get(b)}
        fail_unknown = any(not store_of.get(b) for b in failed_nos)
        still_unknown = any(not store_of.get(b) for b in still_nos)

        if failed_nos:      # 预报失败优先：该批次所属店铺本轮不交运
            if fail_unknown:
                skipped_fail = list(to_handover)
                print("  [警告] 无法定位失败批次所属店铺，本轮交运整体跳过（保守策略）")
            else:
                skipped_fail = [r for r in to_handover if r["shop"] in fail_stores]
            for r in skipped_fail:
                r["_step_handover"] = "跳过-预报失败"
            to_handover = [r for r in to_handover if r not in skipped_fail]

        if still_nos:       # 上传未完成：同样不交运
            if still_unknown or fail_unknown:
                skipped_upload = list(to_handover)
                print("  [警告] 无法定位未完成批次所属店铺，本轮交运整体跳过（保守策略）")
            else:
                skipped_upload = [r for r in to_handover if r["shop"] in still_stores]
            for r in skipped_upload:
                r["_step_handover"] = "跳过-上传未完成"
            to_handover = [r for r in to_handover if r not in skipped_upload]
    elif min_wait > 0:
        print(f"\n[③ 等待] 本次未生成新批次，额外等待 {min_wait}s（--wait）...")
        for left in range(min_wait, 0, -30):
            print(f"  剩余 {left}s ...")
            time.sleep(min(left, 30))
    else:
        print("\n[③ 等待] 本次未生成新批次，直接尝试交运（不轮询）")

    if skipped_fail:
        print(f"[⚠ 告警] {len(skipped_fail)} 单因「预报失败」跳过交运（标记=跳过-预报失败）；"
              f"请到马帮后台核查失败批次后重跑本命令。")
    if skipped_upload:
        print(f"[⚠ 告警] {len(skipped_upload)} 单因「上传未完成」跳过交运"
              f"（标记=跳过-上传未完成）；稍后重跑 mabang-process 幂等补交运。")

    auth_interrupted = False
    if not to_handover:
        print("[④ 交运] 全部订单已选择交运方式，跳过")
        for r in target:
            r["_step_handover"] = r["_step_handover"] or "跳过-已选择"
    else:
        print(f"[④ 交运] 对 {len(to_handover)} 个未选择交运方式的订单设置 "
              f"「{cred.get('handover_keyword') or '七库海外仓'}」...")
        try:
            channel_value = mb_client_get_handover_channel_value(cred, to_handover[0]["order_id"])
            print(f"  交运渠道值: {channel_value}")
            ok_ids, not_found = mb_client_set_handover(
                cred, [str(r["order_id"]) for r in to_handover], channel_value)
            for r in to_handover:
                if str(r["order_id"]) in ok_ids:
                    r["_step_handover"] = "交运成功"
                else:
                    r["_step_handover"] = "交运失败-未在成功列表"
            if not_found:
                print(f"  [警告] 平台未找到订单: {not_found}")
            print(f"  交运成功 {len(ok_ids)} / 共提交 {len(to_handover)}")
        except Exception as e:
            auth_interrupted = isinstance(e, MabangAuthError)
            print(f"  [错误] 交运失败: {e}")
            for r in to_handover:
                r["_step_handover"] = f"错误:{str(e)[:40]}"

    batch_rows = []
    for bn in uploaded_nos:      # 优先用轮询快照（批次可在轮询后继续变化）
        b = (up_rows or {}).get(bn) or {}
        shop = b.get("shopId") or store_of.get(bn, "")
        state = ("预报成功" if bn in success_nos else
                 "预报失败" if bn in failed_nos else
                 "上传未完成" if bn in still_nos else "状态未知")
        status_txt = (f"{state}(status={b.get('status')} 成功={b.get('successNum')}/{b.get('total')} "
                      f"失败={b.get('failNum')})" if b else state)
        batch_rows.append({"batchNo": bn, "shop": shop, "total": b.get("total"),
                           "状态": status_txt, "note": "",
                           "handoverNumber": b.get("handoverNumber", ""),
                           "orderWeight": b.get("orderWeight", ""),
                           "forecastLogisticsName": b.get("forecastLogisticsName", "")})
    if batch_rows and not auth_interrupted and not any(r.get("状态", "").startswith("预报成功") for r in batch_rows):
        try:                     # 无成功快照时回读 status=3 兜底
            r3 = find_batches(cred, batch_nos_new or uploaded_nos, status=3)
            for bn, b in r3["found"].items():
                for row in batch_rows:
                    if row["batchNo"] == bn:
                        row["状态"] = (f"预报成功(status={b.get('status')} "
                                       f"成功={b.get('successNum')}/{b.get('total')})")
                        row["handoverNumber"] = b.get("handoverNumber", "")
                        row["orderWeight"] = b.get("orderWeight", "")
                        row["forecastLogisticsName"] = b.get("forecastLogisticsName", "")
        except Exception as e:
            auth_interrupted = isinstance(e, MabangAuthError)
            print(f"  [警告] 回读批次清单失败: {e}")

    csv_path = write_forecast_csv(target, batch_rows)
    n_ok = sum(1 for r in target if r["_step_handover"] == "交运成功")
    n_fail = sum(1 for r in target if r["_step_handover"] == "跳过-预报失败")
    n_skip = sum(1 for r in target if r["_step_handover"] == "跳过-上传未完成")
    handover_errors = sum(1 for r in target
                          if r["_step_handover"].startswith(("交运失败", "错误:")))
    bits = f"，跳过-预报失败 {n_fail}" if n_fail else ""
    bits += f"，跳过-上传未完成 {n_skip}" if n_skip else ""
    print(f"\n[汇总] 订单 {len(target)} 单（生成 {len(to_create)}/{len(target)}，"
          f"交运成功 {n_ok}{bits}）| 批次 {len(batch_nos_new)} 个"
          f"（成功 {len(success_nos)}/失败 {len(failed_nos)}/未完成 {len(still_nos)}）"
          f" | 明细: {csv_path}")
    if n_fail:
        print("[⚠ 告警] 存在预报失败批次：对应订单未交运；请到马帮后台核查后重跑 mabang-process。")
    if n_skip:
        print("[⚠ 告警] 存在上传未完成批次：对应订单未交运；稍后重跑 mabang-process 幂等补齐。")
    failed = auth_interrupted or n_fail or n_skip or handover_errors or failed_nos or still_nos
    if not failed:
        print("（本次已轮询确认批次预报成功；如需核对可运行 sellerops mabang-forecast --check）")
    return 1 if failed else 0
