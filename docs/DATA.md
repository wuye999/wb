# 数据文件与业务契约

数据根目录由 [paths.py](../src/sellerops/settings/paths.py) 统一定义。用户数据可整体放入项目根目录的 `data/`；文件名、表头、商品编码和凭证结构遵守下文契约。

## 目录与所有权

| 路径（相对 data） | 用途 | 谁维护 |
| --- | --- | --- |
| `credentials.json` | BCS、WB、马帮、飞书和 AI 配置 | 用户及 [平台连接命令](CONFIGURATION.md#通过-workbuddy-连接平台) |
| `商品价格表.xlsx` | 商品名、卖家 SKU、目标售价、包装、前缀、发货方式、马帮库存 SKU | 用户，是商品主数据 |
| `sellerops.db` | 商品归属、各店商品快照、提交和续办记录 | 程序，按数据目录隔离 |
| `state/` | 客服去重、发货群供货等业务状态及改码进程锁 | 程序 |
| `logs/YYYY-MM-DD/` | 运行日志和单次执行回执 | 各业务命令生成，按记录日期查找 |
| `browser_profiles/<平台>/` | 固定 Chromium 的平台登录资料 | 本机专用浏览器，按数据目录隔离 |
| `ai_test_qa.json` | 本地 AI 对照样例集 | 用户；格式见下文 |

`exports/` 与选定的数据目录同级。不同数据目录若共用父目录，也会共用导出目录。报表、查询导出和 AI 对照报告默认写入该目录，支持 `--out` 的命令可指定输出路径。`exports/workbench/` 保存待认领与货不对板两个 HTML 工作台，每种工作台覆盖写入最新一份。`exports/fbs_shipments/YYYYMMDD/` 按供货日期保存供货码和标签 PDF。

商品操作逐项记录在当日的 `商品操作回执_*.csv`，旧的 `ops_result.csv` 追加写入和 `logs/archive/` 轮转已取消。可用 `task clean` 预览或清理过期日志和导出，包括到期的发货 PDF。`exports/workbench/` 始终保留。见 [清理规则](USAGE.md#定期清理本地文件)。

旧快照、归属池和映射 Excel 由 [旧数据迁移命令](MIGRATION.md#迁移旧商品数据) 一次性导入。通过 `report mapping` 从库导出总表或单店表，默认保存在 `exports/`。

普通用户可维护这些自己的数据。项目源码与说明文档按 [使用边界](../AGENTS.md) 保持只读，临时脚本放项目根目录的 `_scratch/`。

固定 Chromium 程序安装在项目根目录的 `.runtime/browsers/`。`--data-dir` 选择登录资料和凭证的位置，各数据目录共用项目内的浏览器程序。安装步骤见 [平台连接配置](CONFIGURATION.md#通过-workbuddy-连接平台)。

`report products` 在 `exports/` 生成商品运营工作簿，使用现有快照、归属、提交记录及本轮 WB Open API 订单。订单明细保留实际采集窗口，退货统计按 `--order-days` 过滤。近 7/14 天采用口径 A，仅统计在途和买断，每行一件。报告无需飞书或 `lark-cli`，旧参数 `--url` 和 `--table` 已移除。BCS 提供对应店铺的官方令牌，`--extra-bcs-stdin` 接收的临时账号仅驻留本轮内存。报告按账号标注来源，链接缺口仅针对本机目标店。[报告说明](PRODUCT_REPORT.md)

`order mabang-void-wb` 读取 `state/WB_FBS发货群供货.json` 的版本 1 供货记录。`state/mabang-void-wb.json` 是版本 1 作废账本，按 WB 店铺 ID 和平台订单 ID 记录供货号、马帮内部订单 ID、运行号及提交状态。匹配、预报交运和作废应用都使用同一数据目录的履约锁。状态先落为 `SUBMITTING`，收到回执后记录 `ACCEPTED`、`FAILED` 或 `UNKNOWN`，损坏的账本会阻止继续提交。勿删除或编辑提交中及结果未知的记录来重跑。逐单 UTF-8 BOM CSV 回执保存在日志目录，按 `data.artifacts.csv` 查找。

## 商品价格表

商品价格表使用工作表名 `Sheet1`，第 1 行是表头。列按名称定位，允许调整顺序。读取价格时要求「产品中文名」「最低售价」两个表头。只读取归属、尺寸或物流资料时要求「产品中文名」。卖家 SKU 可留空，此时中文名须在全表唯一。首次建表可复制 [商品价格表样例](../examples/商品价格表.example.xlsx)，改名为 `data/商品价格表.xlsx` 并替换示例数据。

| 表头 | 用途/格式 |
| --- | --- |
| 卖家SKU | 可选。填写时作为商品键，保持稳定 |
| 产品中文名 | 中文商品名，匹配价格、尺寸和发货配置 |
| 产品图片 | 图片信息，部分工作台/报表使用 |
| 尺寸 | `长*宽*高/毛重`，例如 `10*20*30/0.5`；尺寸 cm、重量 kg |
| 最低售价 | 数值或公式，用于价格参考及明确选用该价格来源的跨店复制 |
| vendorCode前缀码 | 4 位 ASCII 字母，如 `TEST`。全表唯一时可用于本次在架待认领 VC 的自动归属 |
| 发货方式 | 填写「马帮发货」「WB发货」或「不处理」 |
| 马帮库存SKU | 「马帮发货」须填写实际马帮库存编码，其他方式留空 |

公式解析支持有限的单元格引用、四则运算、括号、数字及 ROUND 外壳。直接求值失败时打印告警并读取 Excel 保存的计算缓存。缓存也缺失时，最低售价留空。修改复杂公式后，需用表格软件重新计算并保存。

实现：[表头定义](../src/sellerops/persistence/headers.py)、[价格读取](../src/sellerops/persistence/price_book.py)、[映射仓储](../src/sellerops/persistence/mappings.py)。

## 映射、快照与标识

- `vendorCode` / VC：供应商商品编码，常见格式 `BCS-TEST-123456789`。也支持 `ozon-card-`、斜杠尾段和多变体序号等格式。
- `nmId` / `nmID`：WB 商品码。同一商品在不同店铺可能有不同码，不能把主店码直接当作所有店铺的码。
- `chrtId`：库存规格标识，库存修改依赖它，不能用 nmId 替代。
- 马帮库存 SKU：价格表中登记的库存编码，用于马帮商品匹配。
- WB 仓库操作仅使用各店按 `data/credentials.json` 中 `wb.warehouse_keyword` 唯一匹配并核实的本店默认业务仓（如莫斯科仓或运营配置的其他业务仓）。未配置、匹配不到或匹配多个时中止该店操作，不猜测、不跨店复用 ID、不回退至未配置的仓库。业务请求使用仓库列表的 `id`，office 字段 `warehouseId` 不得代用。若原始快照含有其他仓库 ID，仍按配置核实本店默认业务仓。

快照在库内保留原有 `shopId`、`total`、`rows` 等字段。每家店的最新成功快照与失败原因单独保存，缺快照和已确认的空店是两种情况。`shop list --json` 的 `data.shops` 中，`snapshot_present` 表示是否已有快照，`active_item_count` 和 `trashed_item_count` 分别统计在架与回收站条数。缺快照时计数为 `null`。

默认 `sync` 和 `catalog fetch` 走 BCS 快照接口，先同步再读取 BASE，去重后整店写入 SQLite。单店和全店的 JSON 回执均含 `source`、`fetchedAt`、`shops`、`fetched`、`sync_warnings`、`errors`、`missing_shops`、`prefix_mapping`。回执中的 `fetchedAt` 是回执初始化时间，`fetched[].fetchedAt` 是对应店铺成功保存的采集时间。普通同步未确认时可降级读取缓存，警告列入 `sync_warnings`。读取或保存失败列入 `errors`，失败店铺保留旧快照。全店取得完整远程清单后才计算 `missing_shops`。

逐店事务以现行价格表的唯一四位 ASCII 字母前缀匹配本次快照的在架待认领 VC。命中且 `block_listing` 为 NULL 或空串时，写入商品键和 `last_cn`，将状态改为 `mapped`，来源设为 `prefix`。既有 `mapped`、`excluded`、禁上架范围、备注及源数字段保留。跨店出现同一完整 VC 时只计首次升级。BCS BASE 缓存及 WB Open API 使用同一规则。价格表缺失、独占、无效或版本变化时仅保存快照及待认领记录。`data.fetched[].prefix_mapped` 是逐店新增，`data.prefix_mapping` 的 `mapped` 是合计，`skipped_shops` 记录价格表降级原因，`skipped_vcs` 汇总本次在架待认领记录的前缀和禁上架跳过原因。

SQLite 的 `shops` 表提供以下快照字段。整店商品行、快照信封、这些列和失败原因清除使用同一事务。

| 列 | 快照信封字段 | 口径 |
|---|---|---|
| `snapshot_status` | `snapshotStatus` | `bcs_synced` 同次 BCS 确认并完整读取 BASE，`bcs_cache_fallback` 同步未确认后读取缓存，`bcs_cache_only` 显式跳过同步，`wb_openapi` 官方直读，`legacy_unverified` 旧快照缺少确认依据 |
| `last_fetched_at` | `fetchedAt` | 最近一次完整保存的本地采集时间。旧字段 `last_synced_at` 保留为兼容字段，口径同采集时间 |
| `last_confirmed_sync_at` | `lastConfirmedSyncAt` | 最近一次 BCS 确认后完整采集，或 WB Open API 直读成功的本地采集时间 |
| `last_confirmed_source` | `lastConfirmedSource` | 上次确认同步的来源 `bcs` 或 `wb-openapi` |

缓存入库沿用本店上次确认时间和来源，缺少依据时两列为 `NULL`。只读命令兼容旧库，旧 BCS 快照显示为 `legacy_unverified`，旧 WB Open API 快照可依据有效采集时间计算新鲜度。旧库在首次成功的写事务补齐列，已有快照在下次保存时写入新字段。快照新鲜度以确认时间作判断。缓存本身会提示 `cache_unconfirmed`，达到阈值时显示 `stale`。

显式 `--source wb-openapi` 使用 WB 官方中国区接口，保存本店默认业务仓及缺失统计。规格 `sizeList[].chrtId` 来自商品卡，价格按规格 ID 关联。`stockList` 仅含本店默认业务仓。库存缺失使用空列表，价格缺失使用 null，保留合法零值。

BCS 商品响应先按原始行数核对接口 total，确认完整后才按本店 nmId 和完整 VC 去重。忽略 BCS 记录元信息 `id/createTime/updateTime/createBy/updateBy` 的差异；其余非空字段必须一致，且必须存在一条包含全部已知业务值的完整原行。例如一行有币种/折扣、另一行这些字段为空时保留前者；不跨行拼接，0 和 false 都是有效值。业务值完全一致的完整行再按 BCS updateTime、id 选择代表。同 VC 对应不同或缺失 nmId、同 nmId 对应不同或缺失 VC、在架/回收站状态冲突、非空字段冲突或没有完整原行时，读取失败并保留旧快照。终端报告原始行数和移除条数；快照 `total/fetchedCount` 及同步状态中的店铺 total 均为去重后行数。直接读取 BCS 商品列表的业务也使用去重结果。单店表生成沿用同一规则，避免旧快照重复行进入 Excel。

无完整规格、无有效库存记录或规格重复时，价格与库存里的占位值保持未知。总表优先展示主店资料，缺失值留空；主店没有此 VC 时使用最小店铺 ID 的同一条代表记录。

归属使用完整 VC 关联价格表商品。商品键优先采用卖家 SKU，没有 SKU 时采用唯一的产品中文名。已明确排除的非货盘在统计中可单独标记，禁上架则只限制上架目标。报价或库存 SKU 若需要用于提交，请保存并关闭独占的价格表；普通查询可提示时间后使用上次缓存。

`products` 缓存最近一次成功保存的有效价格表商品行。`vc_map.last_cn` 保存已归属 VC 最后已知的中文名，价格表删行后仍保留 `mapped` 和商品键。历史名称用于订单、评分、销量、飞书和映射报表。现行售价、前缀、包装、库存 SKU 和发货方式只取当前商品行。同卖家 SKU 加回后沿用原 VC 归属。旧库首次成功写事务补 `vc_map.last_cn`、按现有缓存回填可核对的名称，并补 `products.shipping_mode`。只读查询按旧列兼容读取，SQLite 文件字节保持不变。

## 关键状态文件

库存、上架和改码使用各店自己的商品标识。上架和改码的旧回执迁入 `plans`，后续提交也在此保存。普通上架在计划单固定完整 VC、商品键和编码来源。获得明确受理后才将新 VC 写入 `vc_map`，归属来源为 `publish`。已受理的 Open API 上架可按原条码、图片、库存和阶段记录续办。上传图片或库存的结果未知时，停止对应阶段的自动重发。改码已受理、提交中或结果未知时继续保留旧新码分配。详见 [上架与改码流程](OPEN_API_LISTINGS.md)。

| 文件 | 含义 |
| --- | --- |
| `state/WB_FBS发货群供货.json` | 店铺、本店默认业务仓、订单 ID、供货与阶段回执，保留原记录以阻止重复提交 |
| `state/questions_replied.json` / `questions_front_shown.json` | 提问已回复与前台已展示集合 |
| `state/chat_replied.json` / `chat_front_shown.json` | 聊天已回复与前台已展示状态 |

请随完整数据目录备份数据库和其他业务状态文件。损坏的记录交维护者定位或从备份恢复。

## 工作台与批量输入

首次同步快照后，用 `map workbench` 生成带图待认领工作台。`catalog mapping` 生成相同文件。JSON 回执在 `data.artifacts.html` 返回路径，`data.counts` 给出 `prefix`、`multiplier`、`unmatched` 分类及 `groups`、`vcs` 总数。看图核对归属后可分批导出，浏览器保存审核进度。每次导出包含全部已处理组（含筛选隐藏的组），展开为每个完整 VC 一条记录的 JSON 数组。`map batch-set --file` 和 `catalog merge` 接受同一份审核文件。归属动作也识别 `mapped`。

```json
[
  {"vc": "BCS-TEST-123456789", "action": "map", "sku": "SKU-1", "cn": "示例商品"},
  {"vc": "BCS-TEST-987654321", "action": "exclude"}
]
```

`catalog rename --file` 接受 `[{"vc":"BCS-TEST-123456789","cn":"新中文名"}]` 形式的 JSON，会更新数据库里的归属，没有 `--apply` 开关。

`map check-mismatch` 和 `catalog check-mismatch` 写入 `exports/workbench/货不对板筛查工作台.html`，数据来自已归属商品、商品价格表和活跃店铺的在架快照。画廊把勾选状态保存到浏览器 localStorage。导出的 JSON 数组每条包含 `vc`、`cn`、`title`、`shops`，复制按钮输出可用于 `ops trash --vc` 预览的逗号分隔完整 VC。

`ops publish --file` 和 `ops create-cards --file` 支持 `.txt`、`.json`、`.csv`。纯文本每行一个商品码。JSON 可为商品码数组或带 `nmId`（也识别 `sku` / `nm`）的对象数组。CSV 支持按订单登记的中文名补包装尺寸。售价可在批量文件的 `price` 字段或命令的 `--price` 中指定，缺少指定价格时使用本地快照价。见 [发布样例](../examples/publish.example.json)。

## 飞书业务表

程序按以下列名读写，目标表必须事先存在，列类型须允许相应值：

| 表 | 主要列与约定 |
| --- | --- |
| 订单登记 | `订单编号`（去重键）、`日期`（付款时间）、`店铺`（多选标签列表）、`BCS编号`、`商品中文名`、`库存SKU`、`wb编号`、`商品链接`、`订单量` |
| 马帮库存登记表 | `库存SKU`、`商品中文名`、`库存总量`、`状态`、图片附件列 `图`（也识别图片/图片附件或附件类型列）、日期列、`总新增订单量` |

订单登记的「库存SKU」取自本地价格表的「马帮库存SKU」，WB 发货和缺 SKU 的订单留空。wb编号来自下单店铺自己的快照。登记会完整分页读取现有订单编号去重。已有重复可单独使用 `order deduplicate` 清理。

「去重前候选行」是本轮查询、合并和日期圈定后，通过目标店铺、取消单及平台订单编号检查的行。候选行按「表内已登记」「本批重复」「本次待登记」计数，先检查表内编号，再保留本批首次出现的编号。「本次待登记」的留空提示只从这批唯一待写入订单计算，另列去重前候选行的同口径计数。三类原因是 WB 发货导致库存 SKU 留空、有 VC 但中文名留空、无 BCS 编号。同一行可命中多类原因，原因量按行计数。飞书表内全量扫描的总行数、唯一编号和空编号为独立统计。

处理新增订单（`order process`）有飞书地址且能找到 `lark-cli` 时才尝试登记，缺任一项会跳过。`data.pending_orders`、`data.pending_blank_counts`、`data.candidate_blank_counts` 和 `data.artifacts.csv` 按该次登记步骤实际产生的回执提供。

`order stock-register --apply` 会先删除库存表已有记录再全量写入，原日期列对应的历史数据不能因此自动保留。`order stock-days` 的显式日期/区间模式会删除早于起始日期的旧日期列；默认模式和区间模式不能当作同一动作。

## VC 销量报表

`report vc-sales` 以 WB 订单动态的下单行作为数据源。默认按北京时间取近 7 天（含今天），每行计一件。取消、退货、残次退货都计入下单总件数及退货数。Excel 默认写入 `exports/`，含「按商品中文名汇总」和「按完整 VC 汇总」两张表。文件路径见 `data.artifacts.workbook`，未知状态和采集不完整的店铺分别见 `data.unknown_status` 和 `data.failed_shops`。该数据源独立于飞书。

## AI 对照样例集

`ai_test_qa.json` 是对象数组，每项有 `question`、`human_answer`、`product`。product 的 cn、title、brand、colors、price、description、options 按字符串提供。可参考 [问答样例](../examples/ai_test_qa.example.json)。`support ai-test` 会调用真实 LLM，不是离线单元测试。

## 备份与更新

更新代码或复制生产数据前，停止正在写同一目录的任务和监听进程；需要备份浏览器资料时关闭对应浏览器。整份备份 data，包含数据库、旧迁移来源和会话资料。新旧目录中的 data 是独立副本，不会自动互相同步。

收到发布更新时，保留自己的 data，不要用作者或其他用户的数据覆盖。data、临时副本、抓包、会话、日志由 Git 规则排除；仅不含敏感信息的说明与 `examples/` 样例可提交。
