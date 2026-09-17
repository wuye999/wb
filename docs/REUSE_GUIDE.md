# wb\_ops · 开发复用指南（REUSE\_GUIDE.md）

> **这份文档解决一件事**：新增功能时**先查这里**，直接复用已有的模块与函数，不必通读源码找轮子。
> 配套阅读：[ARCHITECTURE.md](ARCHITECTURE.md)（分层与业务规则）｜[DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)（规范与 6 步流程）｜[CLI.md](CLI.md)（现成命令）。
> 维护约定：**新增/改名任何公共函数时，同步登记到第 3 节**；第 7 节有一键重扫命令，可核对是否漏登记。

---

## 一、先查这张表：我要做 X → 直接用哪个

| 我要做的事 | 直接调用 | 位置 |
| --- | --- | --- |
| 拉某店 / 全店在架商品快照 | `products.fetch_shop(shop_id, out_file, no_sync)` / `products.fetch_all(no_sync)` | `services/catalog/products.py` |
| 读某店快照（按 vendorCode 索引，已滤回收站） | `ProductSnapshotRepository.load_shop_rows(shop_id)` → `{vc: row}`；`load_shop_products(shop_id)` → 原始 list | `storage/product_repo.py` |
| 读映射总表状态（vc → 中文名/双倍售价/店铺价/折扣/库存/nmId…） | `MappingRepository.load_mapping_state()` → `(state, excluded)` | `storage/mapping_repo.py` |
| 读商品价格表（权威清单：SKU/中文名/双倍售价/尺寸/前缀码） | `MappingRepository.load_boss()` → `[{idx,sku,cn,dp,img,floor,prefix}]` | 同上 |
| **vendorCode → 中文名**反查（含纠偏池 + 前缀码兜底，毫秒级） | `MappingRepository.build_vc_resolver()` → `(resolve_cn(vc, title), vc_cn)` | 同上 |
| **nmId → 供应商代码 vendorCode**（如投诉/订单只给 nmId） | 本店快照 `load_shop_rows` 反查 vc，兜底映射总表 `load_mapping_state()[0][vc]["nmId"]`（能同时给 vc+中文名） | `services/support/complaints.py` → `_LocalResolver`（**只用本地真源，查不到就标注，不联网核实**） |
| **nmId → 中文名**（按店；同一 vc 各店 nmId 不同） | 店快照 `load_shop_rows` 反查 vc → 再走上面的 resolver（模板 T3） | 同上 |
| 前缀码（4 位）→ 商品 | `MappingRepository.load_prefix_map()` | 同上 |
| 任何 WB 卖家后台请求（任意子域） | `wb_client.make_session(shop, root_version)` + `wb_client.request(...)` | `adapters/wb_client.py` |
| WB 商品 card.json（标题/颜色/描述/选项/尺寸） | `wb_client.fetch_product_info(nm_id, vc, own)` / `fetch_card_json(nm_id)` / `basket_base(nm_id)` | 同上 |
| WB 原生批量改折扣（含按折扣倒序拉取） | `WBClient(shop).fetch_discount_goods_desc(...)` + `.upload_batch_discount(payload)` | 同上 |
| **折扣 < N 的商品列表**（降序接口覆盖不到低折扣侧） | `WBClient(shop).fetch_discount_goods_asc(threshold=N, limit=0)`（`sortOrder=1` 升序，首条 ≥N 即截断） | 同上 |
| 查某店 WB 已取消订单号集合 | `wb_client.fetch_canceled_ids(shop_id, max_pages=10)` | 同上 |
| 投诉单（列表 + 详情含商品 nmId） | `callcenter_client.fetch_appeals(session, ...)` / `fetch_appeal_detail(session, id)` | `adapters/callcenter_client.py` |
| **BCS 一切操作**（店铺/商品/仓库/改价/库存/下架/上架/同步） | `BCSClient()` 的 19 个方法（见 3.5） | `adapters/bcs_client.py` |
| 马帮订单/库存/预报/上传/交运 | `mabang_client` 的 24 个函数（见 3.5） | `adapters/mabang_client.py` |
| LLM 生成客服回复 | `support_svc.generate_ai_reply(question, product_info)`；或 `LLMClient(...)` | `services/support_svc.py` / `adapters/llm_client.py` |
| 异步任务：提交 + 轮询到完成 | `AsyncTaskRunner.run_until_complete(submit_fn, check_fn, timeout, interval, max_retries)` | `adapters/task_runner.py` |
| 状态文件原子写 / 安全读 / 跨进程互斥 | `atomic_dump_json` / `safe_load_json` / `FileLock` | `framework/safe_io.py` |
| 凭证（BCS / WB 各店铺 / AI / 飞书地址） | `credentials.get()` → 见 3.2 | `credentials.py` |
| 各类路径常量（data/、logs/、shops/、映射表…） | `config.*` → 见 3.2 | `config.py` |
| 标准异常（鉴权/限流/平台错误/校验/一致性） | `framework/exceptions.py` 全套（别自己造） | `framework/exceptions.py` |
| 命令注册（新子命令） | `cli.py` 加 parser + `framework/registry.py` 加 `registry.register(...)` | 见模板 T6 |
| 写操作安全三件套（dry-run / `--apply` / `--yes`） | 模板 T2 + `ops.confirm_irreversible(action, amount, yes)` + `common.print_write_hint()` | `services/replicate/ops.py` |
| 结果 CSV 落盘 | 模板 T1（`config.LOG_DIR` + `utf-8-sig`） | 各模块自带写法 |
| 商品中文名归属纠偏（级联全部单表 + 总表） | `mapping_sync.set_vc_override(vc, new_cn, reason, file_path)` | `services/catalog/mapping_sync.py` |
| 写后同步+合并（**仅用户显式要求时**） | `catalog_svc.post_write_merge(fetch=True)` | `services/catalog_svc.py` |

---

## 二、5 分钟上手

```bash
python wb.py --help                      # 40 个命令一览（或看 docs/CLI.md）
python wb.py shops                       # 验证凭证链路（BCS 通）
python tests/run_tests.py --changed      # 只跑「本次改动相关」的测试（见第六节）
```

写新功能前的最小阅读量：**本文件第 1、3 节 + 一个同类的现成模块**（同类=有写操作就抄 `services/replicate/dimension.py`，纯只读就抄 `services/support/complaints.py`/`replicate/dims_check.py`，列表→逐条详情抄 `services/support/questions.py`）。

---

## 三、可复用清单（按分层，含精确签名）

### 3.1 基础设施 `wb_ops/framework/`

| 函数 | 签名 | 用途 / 何时用 |
| --- | --- | --- |
| `safe_io.atomic_dump_json` | `(filepath: str, data, indent=2, use_lock=True, timeout=10.0)` | 原子写 JSON（临时文件 + fsync + os.replace，Windows 占用自动重试）。**写任何核心状态文件必须用它** |
| `safe_io.safe_load_json` | `(filepath: str, default=None, use_lock=False, timeout=5.0)` | 安全读 JSON：不存在/损坏返回 default |
| `safe_io.FileLock` | `(target_filepath: str, timeout=10.0, stale_after=300.0)` | 跨进程排他锁（可重入、死锁自愈）。读改写共享状态时 `with FileLock(path, 10.0):` |
| `exceptions.*` | `WBOpsError` 基类 → `AuthenticationError(401/403)`、`RateLimitError(429)`、`PlatformApiError`、`TaskTimeoutError`、`StorageLockError`、`ValidationError`/`BusinessValidationError`、`NetworkError`、`DataConsistencyError` | 所有业务异常必须继承自它；禁止 `except Exception: pass` |
| `registry.CommandRegistry` | `.register(name, module_path, func_name="run", alias=None)` / `.dispatch(cmd_name, args)` | 新命令注册与动态延迟分发（`framework/registry.py` 末尾集中登记） |
| `cli_args.add_ops_args` / `cli_args.parse_shops` | `add_ops_args(p, *, with_price=False, with_stock=False)` / `parse_shops("a,b") -> [int] \| None` | price/stock/trash 等 ops 参数定义**唯一实现**（argparse-only，`cli.py` 与 `ops.py` 共用；cli 因此保持启动零业务依赖） |

### 3.2 公共 / 配置 / 凭证

`wb_ops/common.py`

| 函数 | 签名 | 用途 |
| --- | --- | --- |
| `ensure_utf8_stdout` | `()` | **`run(args)` 首行必调**：Windows 控制台 UTF-8，防中文乱码 |
| `to_int` | `(v, default=0)` | 平台字段常见 int/str/None 混杂，统一转 int |
| `extract_wb_nm` | `(vc)` | 从 vendorCode 提 WB 原始 nmId（纯数字才有效，否则 None） |
| `jwt_payload` | `(jwt)` | 解 JWT payload（取 Z-Sid 等） |
| `print_write_hint` | `()` | 写操作未加 `--sync` 时的统一提示（写命令末尾调用） |
| `CookieExpiredError` | 类 | **403 = cookie 失效**；店循环里 `except` 它并 `continue` |
| `UA` | 常量 | 与 WB 后台一致的 UA（`make_session` 已用） |

`wb_ops/config.py`（全部为常量/路径，脚本**必须**用它们，别拼相对路径）

`REPO_ROOT`｜`DATA_DIR`｜`PRODUCTS_DIR`（快照）｜`STATE_DIR`（vc\_known/vc\_override/status）｜`HAR_DIR`｜`WORKBENCH_DIR`｜`LOG_DIR`（CSV 落盘）｜`SHOPS_DIR`（单店映射表）｜`SHOPS_ARCHIVE_DIR`｜`CREDENTIALS_JSON`｜`BOSS_XLSX`（商品价格表）｜`MAPPING_XLSX`（聚合总表）｜`VC_KNOWN_JSON`｜`VC_OVERRIDE_JSON`｜`VC_EXCLUDED_JSON`｜`RESULT_CSV`（`data/logs/ops_result.csv`）｜`OUT_*_HTML`（工作台产物）｜`AI_TEST_QA`｜`MAIN_SHOP`／`DEFAULT_SHOP_ID`｜`VC_PREFIX_RE`｜`DISCOUNT_THRESHOLD_DEF=50`｜`DISCOUNT_TARGET_DEF=50`｜`DEFAULT_WAREHOUSE_NAME="莫斯科"`

函数：`shop_json_path(shop_id)`（快照路径）｜`shop_mapping_xlsx(shop_id, shop_name)`（单店映射表路径）｜`is_shop_archived(shop_id)`

`wb_ops/credentials.py` — `credentials.get()` 单例，属性/方法：

`base_url`｜`token`｜`limit_key`｜`cookie_extra`｜`bcs_headers()`（Bearer + X-Limit-Key + Cookie）｜`ai_key`｜`ai_base_url`｜`ai_model`｜`ai_max_tokens`｜`ai_watch_interval`｜`feishu_base_url()`｜`wb_shops()`（三件套齐全的店铺列表）｜`wb_shop(shop_id)`｜`wb_shop_ids()`｜`validate()` → `(ok, 问题列表)`｜`reload()`

每个 WB 店铺 dict 的键：`shopName` / `shopId` / `authorizev3` / `wb_seller_lk` / `cookie`。

### 3.3 领域模型 `wb_ops/domain/models.py`

| 实体 | 关键字段 / 说明 |
| --- | --- |
| `Product`（别名 `ProductCard`） | `nm_id, vendor_code, cn_name, current_discount, current_price, currency, title, shop_id, raw_data`；`from_wb_dict(...)` / `from_snapshot_dict(...)` / `is_valid` / `to_dict()` |
| `Shop` | `shop_id, shop_name, authorizev3, wb_seller_lk, cookie, is_active`；`has_full_credentials` |
| `DiscountPlan` | 折扣批量修改规划输入（threshold/target_discount/name_filter/target_vcs/target_shops/chunk_size/is_apply…） |
| `CustomStockPlan` | 库存调整规划（target_stock/name_filter/target_vcs/skip_confirmation/sync_after…） |
| `TaskResult` | 异步任务结果（task_id/success/processed_count/error_message/shop_id） |
| `OrderStatus` | 订单状态常量（NEW / WAITING_SHIPPED / SHIPPED / COMPLETED / CANCELLED） |
| `Complaint` / `ComplaintProduct` | WB 投诉单及其关联商品；`Complaint.from_list_dict(shop_id, shop_name, data, products)`、`is_pending`（status\_id==1） |

> 跨层传递数据**必须**在这里定义 `@dataclass(slots=True, frozen=True)`，不要传松散 dict。

### 3.4 仓储 `wb_ops/storage/`

`MappingRepository`（映射与全局归属池，全部为 classmethod/staticmethod）

| 方法 | 签名 | 用途 |
| --- | --- | --- |
| `load_mapping_state` | `()` → `(state, excluded)` | `state={vc:{cn,dp,shop_price,discount,stock,nmId,…}}`（自动应用 vc\_override 纠偏） |
| `load_boss` | `()` → `[{idx,sku,cn,dp,img,floor,prefix}]` | 商品价格表（唯一权威清单） |
| `build_vc_resolver` | `()` → `(resolve_cn(vc, default_title), vc_cn)` | vc→中文名 秒级反查（override > known > 前缀码） |
| `load_prefix_map` | `()` → `{前缀: {cn,…}}` | 4 位前缀码识别（免人工审核自动补录的依据） |
| `load_vc_known` / `save_vc_known` | `()` / `(data)` | 全局已知归属池 `vc_known.json` |
| `load_vc_override` / `save_vc_override` | 同上 | 人工纠偏池 `vc_override.json`（改名优先级最高） |
| `load_vc_excluded` / `save_vc_excluded` | 同上 | 排除清单 `vc_excluded.json` |

`ProductSnapshotRepository`（别名 `ProductRepository`）

| 方法 | 签名 | 用途 |
| --- | --- | --- |
| `load_shop_rows` | `(shop_id)` → `{vc: row}` / `None` | **最常用**：在架商品索引（自动滤 `trashedAt`），row 内含 `nmId`/`sizeList`/`price` 等 |
| `load_shop_products` | `(shop_id)` → `list` | 原始快照 list（需要回收站商品时用它） |
| `save_shop_products` | `(shop_id, products, use_lock=True)` | 原子保存快照（走 safe\_io） |
| `shop_ids_from_disk` | `()` → `[sid]` | 扫描 `data/products/` 得店铺 id |
| `get_path` / `exists` | `(shop_id)` | 快照路径 / 是否存在 |

### 3.5 适配器 `wb_ops/adapters/`

**`wb_client.py`（WB 卖家后台，cookie 会话）**

| 项 | 签名 | 用途 |
| --- | --- | --- |
| `make_session` | `(shop: dict, root_version=None) -> Session` | 建会话（塞 cookie + `authorizev3`/`wb-seller-lk`/`seller-lk`/`root-version`/UA）。**任何 WB 子域都复用它** |
| `request` | `(session, method, url, **kwargs) -> Any` | 统一请求：超时 30s、`RETRY_SLEEPS=[1,3,8]` 重试、403→`CookieExpiredError`、≥400→`PlatformApiError`；`params=`/`json=` 透传 |
| `request_post` | `(session, url, payload, allow_400_json=False)` | POST 专用（`allow_400_json=True` 时 400 也回 JSON） |
| `WBClient` | `(shop_dict, root_version=None)`；`.fetch_discount_goods_desc(threshold, limit=0, page_size=100, max_pages=200)`；`.upload_batch_discount(data_payload) -> TaskResult` | 折扣批量：按折扣倒序拉取 + 分批提交 |
| `basket_base` / `fetch_card_json` | `(nm_id)` | nmId → basket CDN 路径 / card.json dict（失败 None） |
| `fetch_product_info` | `(nm_id, vc='', own=None)` | 整合标题/品牌/颜色/价格/选项等（客服模块用） |
| `fetch_canceled_ids` | `(shop_id, max_pages=10) -> set` | 该店已取消订单号集合 |

**`bcs_client.py`（BCS 云端 ERP：Bearer + X-Limit-Key）** — `BCSClient()` 实例

`request_json(method, url, retry=3, **kwargs)`（429 退避 / 401 明确报错）｜`get(url, retry)`｜`post(url, data, retry)`｜`fetch_shop_list()`｜`get_main_shop()`｜`fetch_shop_products(shop_id, filter_type='BASE')`｜`count_by_filter(shop_id)`｜`fetch_warehouses(shop_id)`｜`default_warehouse_id(shop_id)`｜`remove_to_trash(shop_id, nm_ids)`｜`batch_push_products(shop_configs, sku_prices, mode=1, carry_brand=1)`｜`sync_shop(shop_id, filter_type='ALL')`｜`wait_sync_done(task_id, shop_id, timeout, interval, quiet)`｜模块级 `print_shops(args)`

**`mabang_client.py`（马帮 ERP，三域三套会话）**

`get_mabang_cred()`｜`fetch_stock_list(cred)`（全部库存 SKU）｜`download_file(url, out_path)`（图片/附件下载）｜`api_ready(cred)`｜`refresh_api_token(cred)`（401 自动续期）｜`www_headers` / `api_headers` / `aamz_headers`｜`api_post(cred, path, body)`｜`fetch_pending_orders(cred, days=30, page_size=100, max_pages=20)`｜`fetch_all_orders(cred, page_size=500, max_pages=60)`｜`fetch_order_item_ids(cred, order_id)`｜`search_stock(cred, sku, warehouse_id, page_size)`｜`replace_order_item(cred, order_item_id, stock_id, warehouse_id)`｜`get_forecast_logistics(cred)`｜`batch_create_forecast(cred, order_ids, logistics, channel)`｜`get_forecast_list(cred, status, rows_per_page)`｜`get_forecast_config(cred, my_logistics_id)`｜`upload_forecast_batch(cred, batch_nos)`｜`discover_handover_channel(cred)`｜`get_handover_channel_value(cred, sample_order_id)`｜`set_handover(cred, order_ids, channel_value)`｜`cookie_value(cookie_str, name)`｜`parse_json(resp)`

**`callcenter_client.py`（WB 投诉单，只读）**

`fetch_appeals(session, appeal_type='in', limit=0, max_pages=200, stats=None)`（游标翻页，`stats` 出参回填 `total`/`pages`）｜`fetch_appeal_detail(session, appeal_id)`（平台错误降级 `None`）｜常量 `APPEALS_LIST` / `APPEALS_DETAIL` / `PAGE_SIZE` / `MAX_PAGES`

**`llm_client.py`**：`LLMClient(api_key='', base_url=…, model=…, max_tokens=1000)`、`.generate_reply(question, product_info, custom_system_prompt=None)`
**`task_runner.py`**：`AsyncTaskRunner.run_until_complete(submit_fn, check_fn, timeout, interval, max_retries)`（staticmethod）
**`cookies.py`**：`extract_sessions(md_text)`、`build_sid_map(cfg)`、`run(md_path)`、`run_cookies_update(args)`

### 3.6 业务服务 `wb_ops/services/`

**门面（`*_svc.py`）：新命令的入口就写在这里**，`registry` 指向门面的 `run_xxx`：

| 门面 | 对外函数 | 领域实现目录 |
| --- | --- | --- |
| `catalog_svc.py` | `run_fetch` `run_mapping` `run_mapping_import` `run_mapping_check` `run_mismatch_check` `run_review` `run_merge` `run_mapping_rename` `run_shops_mapping` | `catalog/`：`products.py`（快照）`mapping.py`（增量合并）`mapping_sync.py`（单店表/纠偏）`mapping_excel.py`（8-Sheet 生成）`mapping_check.py` `mismatch_check.py` `workbench.py`（HTML）`keywords.py` |
| `discount_svc.py` | `run_cli`（discount/discount-wb/discount-scan）`run_promo_apply` `run_discount_bcs` `run_price_review` | `discount/`：`promo.py`（报名）`price_review.py`（隔离区审核）`discount_bcs.py`（BCS 慢速改折扣） |
| `order_svc.py` | `run_orders` `run_mabang_orders` `run_mabang_forecast` `run_feishu_register` `run_mabang_process` `run_mabang_stock_register` `run_mabang_stock_daily` | `order/`：`orders.py` `mabang.py` `mabang_process.py` `mabang_stock.py` `mabang_stock_daily.py` `feishu_register.py` |
| `replicate_svc.py` | `run_price` `run_stock` `run_trash` `run_replicate` `run_import_shelve` `run_dimension` `run_dims_check` `run_banned` `run_clean` `run_remote_wh` | `replicate/`：`ops.py`（薄门面）+ `ops_plan.py`（**计划构造，无副作用**）+ `ops_executor.py`（**分批执行/审计**）+ `dimension.py` `dims_check.py` `banned.py` `clean.py` `replicate.py` `import_shelve.py` `foreign_table.py` `wb_card.py` `remote_wh.py` |
| `support_svc.py` | `run_questions` `run_questions_watch` `run_ai_test` `run_appeals`；类方法 `generate_ai_reply` / `load_replied` / `save_replied` / `load_shown` / `save_shown` | `support/`：`questions.py` `questions_watch.py` `ai_reply_test.py` `complaints.py` |

> 门面里对外函数统一「薄转发」写法：`def run_xxx(args): return xxx_svc.method(args)`；领域实现内部 `run(args)` 返回 `0/1/130`。

---

## 四、代码模板（直接抄）

### T1 新增一个「只读」命令（按店循环 + 限速 + 明细 + CSV）

参照实物：`services/support/complaints.py`（最新）、`services/replicate/dims_check.py`

```python
import csv, os, time
from datetime import datetime
from typing import Any, Dict, List
from wb_ops import common, config, credentials
from wb_ops.adapters import wb_client as wb_api
from wb_ops.storage.mapping_repo import MappingRepository

SHOP_SLEEP = 0.5      # 店间串行（实测并行会触发限速）

def run(args: Any) -> int:
    common.ensure_utf8_stdout()                     # 首行必调
    resolve_cn, _ = MappingRepository.build_vc_resolver()   # 需要中文名才加
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺"); return 1
    pairs = [(s, common.to_int(s.get("shopId"))) for s in wb_shops]
    if getattr(args, "shops", ""):                  # --shops 过滤（全仓无 helper，照抄此行）
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        pairs = [p for p in pairs if p[1] in want]
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）"); return 1
    names = ", ".join(f"{s['shopName']}({sid})" for s, sid in pairs)
    print(f"店铺 {len(pairs)} 个: {names}（只读）")

    rows: List[Dict[str, Any]] = []
    for shop, sid in pairs:
        name = shop["shopName"]
        try:
            session = wb_api.make_session(shop, cred.root_version)
            data = wb_api.request(session, "GET", "https://seller-xxx.wildberries.ru/ns/....", params={"limit": 50})
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {name}: {e}（该店中止，继续下一店；cookie 失效请跑 wb.py cookies-update）")
            continue
        except Exception as e:
            print(f"  [警告] 店铺 {name} 查询失败: {e}（该店中止，继续下一店）")
            continue
        for it in (data.get("data") or []):
            rows.append({"店铺": name, "ID": it.get("id")})
            print(f"  [列表] {it.get('id')}")
        time.sleep(SHOP_SLEEP)

    print(f"\n[汇总] 共 {len(rows)} 条")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"XXX_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:   # 编码硬性规定
            w = csv.DictWriter(f, fieldnames=["店铺", "ID"])
            w.writeheader(); w.writerows(rows)
        print(f"[日志] {len(rows)} 行 → {path}")
    else:
        print("\n没有匹配数据")
    return 0
```

### T2 新增一个「写」命令（dry-run / `--apply` / `--yes`，Plan + Executor）

参照实物：`services/replicate/dimension.py`（单文件够用）与 `ops.py + ops_plan.py + ops_executor.py`（复杂场景）

```python
# 1) 解析参数时用 framework.cli_args.add_ops_args(p, with_price=False, with_stock=False) 统一拿
#    （唯一实现在 wb_ops/framework/cli_args.py，argparse-only；ops.add_ops_args 为同名薄转发）
#    --sku/--name/--prefix/--vc/--shops/--apply/--yes/--sync
# 2) 纯函数构造计划（无副作用）→ 3) dry-run 打印 → 4) --apply 时先确认再分批执行
from wb_ops.services.replicate import ops, ops_plan, ops_executor

plans = ops_plan.plan_stock(vcs, shops, state, amount)      # 无副作用
ops_plan.dry_run(plans, "stock", amount)                    # 默认路径
if not getattr(args, "apply", False):
    print("[提示] 以上为 dry-run 预览；确认无误后加 --apply 执行")
    return 0
if amount == 0:                                             # 不可逆动作二次确认
    ops.confirm_irreversible("stock", amount, getattr(args, "yes", False))
ok, fail = ops_executor.run_apply(plans, "stock")           # 分批 ≤300 / 批间 sleep
print(f"结果: 成功 {ok} · 失败 {fail}")
common.print_write_hint()                                   # 统一「未做写后验证」提示
return 0
# ⚠ 写完即结束：绝不补跑 fetch / merge / 自写脚本回验
```

### T3 vc / nmId / 中文名 三种身份互换

```python
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops.storage.product_repo import ProductSnapshotRepository
from wb_ops import common

state, _ = MappingRepository.load_mapping_state()      # vc → {cn, dp, shop_price, discount, stock, nmId}
resolve_cn, vc_cn = MappingRepository.build_vc_resolver()

# A. vc → 中文名（带前缀码兜底）
cn = resolve_cn("BCS-ABCD-1234567", "")

# B. nmId → 中文名（⚠ 同一 vc 各店 nmId 不同 → 必须按店取快照）
def cn_by_nmid(shop_id: int, nm_id: int) -> str:
    rows = ProductSnapshotRepository.load_shop_rows(shop_id) or {}
    for vc, item in rows.items():
        if common.to_int(item.get("nmId") or item.get("nmID")) == nm_id:
            return resolve_cn(vc, "")
    return ""

# C. vc → WB 原始 nmId（⚠ 仅新格式 BCS-{前缀}-{nmId} 成立；旧格式末段可能是旧标识，不可当 nmId 搜）
nm = common.extract_wb_nm(vc)          # 纯数字才返回，否则 None
```

### T4 三种分页写法

```python
# ① 游标型（本页最后一条 id 作为下一页 cursor；有 total 就以 total 终止，防服务端封顶）
cursor, items = None, []
for _ in range(MAX_PAGES):
    params = {"limit": 50}
    if cursor is not None: params["cursor"] = cursor
    d = wb_api.request(session, "GET", URL, params=params)
    data = d.get("data") or []
    if not data: break
    items += data
    total = common.to_int(d.get("total"))
    if total and len(items) >= total: break
    if not total and len(data) < 50: break
    nxt = common.to_int(data[-1].get("id"))
    if not nxt or nxt == cursor: break        # 死循环防护
    cursor = nxt

# ② 页码型（BCS 常见）：pageNum/pageSize，或 offset += page_size
# ③ 提前截断型（已排序数据）：首条不满足条件即 break（见 WBClient.fetch_discount_goods_desc）
```

### T5 状态文件读写 + 跨进程互斥

```python
from wb_ops.framework.safe_io import atomic_dump_json, safe_load_json, FileLock

data = safe_load_json(path, default={}, use_lock=True)     # 读
with FileLock(path, timeout=10.0):                          # 读改写共享状态
    data["k"] = v
    atomic_dump_json(path, data, indent=2, use_lock=True)   # 原子写（内部再取锁，支持重入）
```

### T6 新增命令要改的 4 处（缺一不可）

```python
# ① wb_ops/cli.py：build_parser() 里加 subparser（放语义相近的命令旁边）
p = sub.add_parser("xxx", help="一句话说明（括号里标注只读/写操作、dry-run 默认）")
p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")

# ② wb_ops/services/<域>_svc.py：加薄门面 + 类方法
def run_xxx(args):
    return xxx_svc.method_xxx(args)

# ③ wb_ops/framework/registry.py：注册（别名用 alias=）
registry.register("xxx", "wb_ops.services.<域>_svc", "run_xxx")

# ④ tests/test_all_commands.py：命令名加进 subcommands 列表 + assertEqual 计数（现为 40）
#    并新增用例（只读、不得带 --apply）：def test_NN_xxx(self): ...
#    再在 tests/run_tests.py 的 PATH_HINTS 里为「文件→命令」加一行（见第六节）
```

---

## 五、复用铁律（别做这些）

1. **别自己搭 HTTP 会话/重试**：一律 `wb_api.make_session` + `wb_api.request/request_post`（已处理 403→`CookieExpiredError`、4xx/5xx 指数退避、30s 超时）；BCS 走 `BCSClient.request_json`（429 退避）；马帮走 `mabang_client`（api 域 401 自动续期）。
2. **别裸 `open(path, "w")` 写核心状态**（快照/凭证/映射池）：必须 `atomic_dump_json`；读改写加 `FileLock`。
3. **别跨域 import 别人的私有实现**：跨域只能经 `*_svc.py` 门面、`storage` 仓储或 `adapters` 适配器；公共能力下沉到 `common`/`framework`。
4. **别新增第三方依赖**（只有 `requests`/`openpyxl`/`pypinyin` 可用）；**别把 HTTP 细节写进 services**。
5. **别在写操作后跑 `fetch`/`merge`/自写脚本回验**：写接口返回成功即生效，全量同步慢且易触发风控。
6. **别重复造轮子**：需要新的通用能力（分页、CSV、重试、额度控制）时，先加到 `adapters`/`framework`/`common`，再在业务里引用。
7. **店铺串行**、批量 ≤300/批、批间 0.15~0.6s；仓库默认莫斯科。
8. **别经「服务层转发模块」调适配器函数**：`services/order/mabang.py` 等模块只做向后兼容 re-export，里面**不一定**有你新加的适配器函数（实测踩坑：`mabang.fetch_stock_list` 不存在 → 应直接 `from wb_ops.adapters import mabang_client as mbc; mbc.fetch_stock_list(cred)`）。
9. **异常只抛 `WBOpsError` 家族**，禁止 `except Exception: pass`（要降级必须打 `[警告]` 并写清原因）。

---

## 六、测试：只测「改动/新增」的部分（不必每次全量）

> 全量套件 `tests/test_all_commands.py` 会真连平台、跑 40 个命令，**约 5 分钟**；日常没必要全跑。
> 新增了轻量选择器 `tests/run_tests.py`：

| 命令 | 跑什么 | 典型耗时 |
| --- | --- | --- |
| `python tests/run_tests.py --changed` | **日常首选**：用 git 探测本次改动文件 → 映射到相关命令与用例（未能映射的按跨层处理=全量） | 20s ~ 5min |
| `python tests/run_tests.py --cmd appeals,discount` | 只跑指定命令：相关用例 + 这些命令的 `--help` 解析冒烟 | 10s~1min |
| `python tests/run_tests.py --help-smoke` | 只跑全部命令的 `--help`（最快回归：命令注册/参数解析没坏） | ~20s |
| `python tests/run_tests.py -k appeals` | 关键字透传给 unittest（`-k`） | 取决于匹配 |
| `python tests/run_tests.py --list` | 打印「命令 ↔ 用例」映射表，确认某命令有哪些覆盖 | 即时 |
| `python tests/run_tests.py --plan` | 只打印将要执行的用例，不执行（配合上面任一模式） | 即时 |
| `python tests/run_tests.py` | **全量**（= `python -m unittest tests/test_all_commands.py`）：发版 / 跨层重构 / 改 `cli.py`+`framework` 时跑 | ~5min |

**门禁规则（与 DEVELOPMENT_GUIDE 第七节一致）**

- 日常改动：`--changed`（或 `--cmd <本次涉及的命令>`）**全绿**即可提交。
- 新增命令：必须 `--cmd <新命令>` 全绿，且 `--help-smoke` 全绿（证明注册与参数解析无回归）。
- 跨层改动（`cli.py`、`framework/`、`common.py`、`config.py`、`credentials.py`、`storage/`）：跑**全量**。
- 测试永远是**只读**的：新写用例禁止带 `--apply`。
- 修改了测试文件本身或新增命令时，记得同步两处：`tests/test_all_commands.py`（命令列表 + 计数）与 `tests/run_tests.py`（`PATH_HINTS` 文件→命令映射）。

---

## 七、维护：一键重扫公共 API（核对本文件是否过期）

新增/改名公共函数后，跑下面这段即可列出**当前真实的**模块与顶层函数（含首行 docstring），与本文件第 3 节对照补登：

```bash
"<你的 venv python>" -c "
import ast, os
def d(n):
    s=(ast.get_docstring(n) or '').strip().splitlines(); return s[0][:70] if s else ''
for root,dirs,files in os.walk('wb_ops'):
    dirs[:]=[x for x in dirs if x!='__pycache__']
    for f in sorted(files):
        if not f.endswith('.py'): continue
        p=os.path.join(root,f)
        t=ast.parse(open(p,encoding='utf-8').read())
        tops=[n for n in t.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))]
        if not tops: continue
        print('##', p.replace(os.sep,'/'))
        for n in tops:
            print(('  class ' if isinstance(n,ast.ClassDef) else '  def ') + n.name + '  # ' + d(n))
            if isinstance(n,ast.ClassDef):
                for m in n.body:
                    if isinstance(m,ast.FunctionDef):
                        print('      .' + m.name + '  # ' + d(m))
"
```

需要**精确签名与默认值**时，用 `inspect`：

```bash
"<你的 venv python>" -c "
import inspect; from wb_ops.adapters import wb_client as w
print(inspect.signature(w.request)); print(inspect.signature(w.make_session))
"
```

**必须同步更新本文件的时机**：① 新增/改名公共函数或模块；② 新增 CLI 命令（第 1 节 + 第 6 节映射）；③ 新增/变更路径常量；④ 分层调整（模块搬家）。
