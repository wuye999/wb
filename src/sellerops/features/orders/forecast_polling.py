"""forecast polling。"""


import time


from sellerops.integrations.mabang.forecast import get_forecast_list as mb_client_get_forecast_list
from sellerops.integrations.mabang.session import MabangAuthError

DEF_UPLOAD_TIMEOUT = 900

DEF_POLL_INTERVAL = 20

def _resolve_upload_timeout(args, cred):
    """上传完成轮询超时（秒）：CLI --upload-timeout > cred.mabang.upload_timeout_seconds > 900"""
    v = getattr(args, "upload_timeout", None)
    if v is None:
        v = cred.get("upload_timeout_seconds")
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEF_UPLOAD_TIMEOUT

def _resolve_poll_interval(args, cred):
    """批次状态轮询间隔（秒）：CLI --poll-interval > cred.mabang.upload_poll_interval > 20"""
    v = getattr(args, "poll_interval", None)
    if v is None:
        v = cred.get("upload_poll_interval")
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEF_POLL_INTERVAL

def _ceil_pages(total, size):
    """按总数与页大小算页数；参数非法返回 0。"""
    try:
        total, size = int(total), int(size)
    except (TypeError, ValueError):
        return 0
    return (total + size - 1) // size if total > 0 and size > 0 else 0

def find_batches(cred, batch_nos, status, rows_per_page=200, max_pages=50):
    """在指定 status 列表中检索 batch_nos；首批未命中则按 pageHtml 自动翻页。

    Args:
        status: 1=待预报 / 3=预报成功 / 99=预报失败 / 5=历史预报。
        max_pages: 翻页上限（防异常时无限翻页）。

    Returns:
        {"found": {batchNo: row}, "missing": set, "all_found": bool,
         "pages_fetched": int, "stats": dict}
    """
    want = {str(b) for b in (batch_nos or []) if b}
    res = {"found": {}, "missing": set(want), "all_found": not want,
           "pages_fetched": 0, "stats": {}}
    if not want:
        return res
    page, total_pages = 1, None
    while page <= max_pages:
        rows, stats = mb_client_get_forecast_list(cred, status=status,
                                        rows_per_page=rows_per_page, page=page)
        res["pages_fetched"] = page
        res["stats"] = stats or {}
        for r in rows:
            bn = str(r.get("batchNo"))
            if bn in want:
                res["found"][bn] = r
        res["missing"] = want - set(res["found"])
        if not res["missing"]:
            res["all_found"] = True
            break
        if len(rows) < rows_per_page:          # 末页（不足一页）
            break
        if total_pages is None:                # 页数优先取 pageHtml，其次按总数折算
            total_pages = (res["stats"].get("pageCount")
                           or _ceil_pages(res["stats"].get("pageTotal"), rows_per_page))
        if total_pages and page >= total_pages:
            break
        page += 1
    return res

def _warn_batch_failnum(found_rows, warned):
    """批次行内 failNum>0 告警（每批次只告警一次）。"""
    for bn, row in (found_rows or {}).items():
        try:
            fn = int(row.get("failNum") or 0)
        except (TypeError, ValueError):
            fn = 0
        if fn and bn not in warned:
            warned.add(bn)
            print(f"  [⚠ 告警] 批次 {bn}[{row.get('shopId')}] 行内失败 {fn} 单"
                  f"（成功={row.get('successNum')}/{row.get('total')}），请核查")

def wait_upload_done(cred, batch_nos, timeout=DEF_UPLOAD_TIMEOUT,
                     interval=DEF_POLL_INTERVAL, min_wait=0, rows_per_page=200):
    """③ 轮询等待本批预报单真正上传结束（**正向判据**）。

    完成判据：batch_nos 全部命中 status=3「预报成功」；为防成功后迅速归档移入
    status=5「历史预报」导致永不命中，**命中 status=5 亦算成功**。

    依据（2026-09-30 两次抓包实证）：
      - `uploadForecastBatch` 响应仅「批次已插入上传队列，请5-10分后查看预报结果」
        ⇒ **无任务 id / 无进度字段**，完成判定只能靠列表轮询；
      - 上传后 **≤0.3s 行级 status 由 1→2（上传中）**，期间 successNum **恒为 0**
        （没有「部分完成」判据），成功后出现在 status=3（successNum==total、
        handoverNumber 回填）；
      - 四统计（waitTotal/succesTotal/failTotal/historyTotal）为**全局值、不随 tab 变**。

    请求节奏（2026-09-30 精简，稳态 **1 请求/轮**，原为 2-3 次/轮）：
      预扫描：上传后立即查一次 status=1 —— 确认受理（逐批打印 排队中/上传中 相位）
              并建立 batchNo→shopId 店铺映射；失败自动重试一次；
      主循环：每轮只查一次 status=3（仅对未成功批次），命中即成功；
      status=5 兜底：仅当「连续 3 轮 status=3 未命中」或「已耗时 ≥ 超时一半」时
              各触发一次（限频，避免每轮往返）；
      **失败早退（2026-10-17）**：`failTotal` 相对上轮上涨 ⇒ 立刻查一次 status=99
              核实目标批次，命中即判失败并「不再空等」——旧逻辑只查 status=3，
              失败批次要等到满 900s 超时才定性（实测白等 40+ 轮空转）；
      超时定性：先 best-effort 补查一次 status=1 补齐缺失映射，再查一次 status=99。

    Returns:
        {"done": bool,           # True=全部批次已确认成功
         "success": set[str],    # 命中 status=3 或 5
         "failed": set[str],     # 超时定性命中 status=99
         "still": set[str],      # 超时仍未完成（未知态）
         "store_of": {bn: shopId},   # 店铺级映射（局部跳过交运用）
         "rows": {bn: row},      # 各批次最后快照
         "stats": dict, "reason": "ok"|"timeout"}
    """
    batch_nos = [str(b) for b in (batch_nos or []) if b]
    if not batch_nos:
        return {"done": True, "success": set(), "failed": set(), "still": set(),
                "store_of": {}, "rows": {}, "stats": {}, "reason": "ok"}
    want = set(batch_nos)
    success, failed, rows, store_of, last_stats = set(), set(), {}, {}, {}
    warned, prev_fail = set(), None
    start = time.time()
    mw = f"，最小等待 {min_wait}s" if min_wait else ""
    print(f"\n[③ 等待上传] 轮询 {len(batch_nos)} 个批次直至命中「预报成功(status=3)」"
          f"或「历史预报(status=5)」（超时 {timeout}s，间隔 {interval}s{mw}）...")

    def _merge(found, mark_success=False):
        """统一收口：写快照 / 建店铺映射 / 成功集合 / 行级失败告警。"""
        for bn, row in (found or {}).items():
            rows[bn] = row
            store_of.setdefault(bn, row.get("shopId"))
            if mark_success:
                success.add(bn)
        _warn_batch_failnum(found, warned)

    # —— 预扫描：status=1 一次（确认受理 + 建立 batchNo→shopId 映射；失败重试一次）——
    prescan = None
    for attempt in (1, 2):
        try:
            prescan = find_batches(cred, want, status=1, rows_per_page=rows_per_page)
            if prescan["stats"]:
                last_stats = prescan["stats"]
            _merge(prescan["found"])
            break
        except MabangAuthError:
            raise
        except Exception as e:
            prescan = None
            tail = "重试一次" if attempt == 1 else "跳过（映射稍后由 status=3/99 命中补齐）"
            print(f"  [警告] 预扫描 status=1 查询异常（{str(e)[:80]}），{tail} ...")
    if prescan is not None:
        for bn in batch_nos:
            row = prescan["found"].get(bn)
            if row is None:
                phase = "未出现（以 status=3 命中为准）"
            else:
                st = int(row.get("status") or 0)
                phase = ("排队中(status=1)" if st == 1 else
                         "上传中(status=2)" if st == 2 else f"status={st}")
            print(f"  [预扫描] {bn}[{store_of.get(bn) or '-'}] {phase}")
        print(f"  [预扫描] 已建立店铺映射 "
              f"{sum(1 for b in want if store_of.get(b))}/{len(want)}")

    miss_rounds, did5_miss3, did5_half = 0, False, False
    while True:
        # ① 每轮仅 1 次请求：status=3 预报成功（仅查未成功批次）
        pend = want - success
        added = 0
        if pend:
            try:
                r3 = find_batches(cred, pend, status=3, rows_per_page=rows_per_page)
                if r3["stats"]:
                    last_stats = r3["stats"]      # 四统计为全局值，无 status=1 查询时同样可用
                n0 = len(success)
                _merge(r3["found"], mark_success=True)
                added = len(success) - n0
            except MabangAuthError:
                raise
            except Exception as e:
                print(f"  [警告] status=3 查询异常（{str(e)[:80]}），稍后重试 ...")
        miss_rounds = 0 if added else miss_rounds + 1

        # ② status=5 兜底（成功后归档）：限频，每个触发条件各一次
        pend = want - success
        elapsed = int(time.time() - start)
        trig_miss3 = bool(pend) and miss_rounds >= 3 and not did5_miss3
        trig_half = bool(pend) and (elapsed * 2 >= timeout) and not did5_half
        if trig_miss3 or trig_half:
            try:
                r5 = find_batches(cred, pend, status=5, rows_per_page=rows_per_page)
                if r5["stats"]:
                    last_stats = r5["stats"]
                _merge(r5["found"], mark_success=True)
            except MabangAuthError:
                raise
            except Exception as e:
                print(f"  [警告] status=5 查询异常（{str(e)[:80]}），稍后重试 ...")
            did5_miss3 = did5_miss3 or trig_miss3
            did5_half = did5_half or trig_half

        fail_now = int((last_stats or {}).get("failTotal") or 0)
        if prev_fail is not None and fail_now > prev_fail:
            print(f"  [⚠ 告警] failTotal 上涨：{prev_fail} → {fail_now}"
                  f"（疑似批次上传失败，请留意）")
            # 立刻核实：目标批次是否已进「预报失败(99)」列表 —— 命中即早退，不空等到超时。
            # 背景（2026-10-17）：失败批次会直接落 status=99，既不进 status=3 也不再有
            # status=1 快照，旧逻辑只能死等满 900s 超时才去定性，白等 40+ 轮空转。
            pend99 = want - success - failed
            if pend99:
                try:
                    r99 = find_batches(cred, pend99, status=99, rows_per_page=rows_per_page)
                    if r99["stats"]:
                        last_stats = r99["stats"]
                    for bn, row in r99["found"].items():
                        failed.add(bn)
                        rows[bn] = row
                        store_of.setdefault(bn, row.get("shopId"))
                        print(f"  [✗ 失败] {bn}[{row.get('shopId') or '-'}] 已进入「预报失败」"
                              f"列表（失败 {row.get('failNum')}/{row.get('total')} 单），停止等待")
                except MabangAuthError:
                    raise
                except Exception as e:
                    print(f"  [警告] status=99 核实异常（{str(e)[:80]}），继续轮询 ...")
        prev_fail = fail_now

        still = want - success
        elapsed = int(time.time() - start)
        detail = "  ".join(
            (f"{bn}[{store_of.get(bn)}] status={rows[bn].get('status')} "
             f"{rows[bn].get('successNum')}/{rows[bn].get('total')}(fail={rows[bn].get('failNum')})"
             if bn in rows else f"{bn}（暂无快照）")
            for bn in sorted(still)) or "（无）"
        print(f"  [{elapsed}s/剩余 {max(0, timeout - elapsed)}s] 已成功 {len(success)}/{len(want)}"
              f"，未成功 {len(still)}: {detail}"
              f" | wait={last_stats.get('waitTotal')} succ={last_stats.get('succesTotal')}"
              f" fail={last_stats.get('failTotal')}")

        if not still and elapsed >= min_wait:
            print(f"  [完成] 全部 {len(want)} 个批次已确认预报成功（耗时 {elapsed}s）")
            return {"done": True, "success": success, "failed": set(), "still": set(),
                    "store_of": store_of, "rows": rows, "stats": last_stats, "reason": "ok"}
        # 未成功批次里若已无「未知态」（剩下的都已定性失败），无需再等 —— 立即收口
        if not (still - failed):
            print(f"  [完成等待] 成功 {len(success)} / 失败 {len(failed)} / 未完成 0"
                  f"（耗时 {elapsed}s，失败批次已定性，不再空等超时）")
            return {"done": False, "success": success, "failed": failed, "still": set(),
                    "store_of": store_of, "rows": rows, "stats": last_stats, "reason": "failed"}
        if elapsed >= timeout:
            break
        time.sleep(interval)

    # 超时：先 best-effort 补齐缺失的店铺映射，再查一次 status=99 定性「预报失败」
    missing_map = [b for b in (want - success) if not store_of.get(b)]
    if missing_map:
        try:
            rf = find_batches(cred, missing_map, status=1, rows_per_page=rows_per_page)
            if rf["stats"]:
                last_stats = rf["stats"]
            _merge(rf["found"])
        except MabangAuthError:
            raise
        except Exception as e:
            print(f"  [警告] 超时补查 status=1 映射异常（{str(e)[:80]}），映射可能不全")
    try:
        r99 = find_batches(cred, want - success, status=99, rows_per_page=rows_per_page)
        for bn, row in r99["found"].items():
            failed.add(bn)
            rows[bn] = row
            store_of.setdefault(bn, row.get("shopId"))
    except MabangAuthError:
        raise
    except Exception as e:
        print(f"  [警告] status=99 定性查询异常（{str(e)[:80]}），未成功批次按未完成处理")
    still = want - success - failed
    print(f"  [⚠ 超时] {int(timeout)}s 内：成功 {len(success)} / 失败 {len(failed)}"
          f" / 未完成 {len(still)}{('：' + ','.join(sorted(still))) if still else ''}")
    print("          失败批次订单将标记「跳过-预报失败」、未完成批次订单标记"
          "「跳过-上传未完成」，均不交运；稍后重跑本命令可幂等补齐。")
    return {"done": False, "success": success, "failed": failed, "still": still,
            "store_of": store_of, "rows": rows, "stats": last_stats, "reason": "timeout"}
