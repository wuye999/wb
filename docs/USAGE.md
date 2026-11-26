# CLI 命令用法

本文件按业务模块汇总 CLI 命令用法、参数示例与执行边界，供命令行使用者和 AI 按需查阅。普通电商运营请使用 [用户使用手册](../user-guide/README.md)。AI 执行规范、命令选型和汇报要求见 [AI 日常操作指南](AI_GUIDE.md)。

## 通用命令约定

- 在项目根目录运行示例。店铺 ID `1001`、商品码 `123456789`、商品名和计划单号均为占位值，提交时使用本次预览的单号。
- 批量商品命令须指定店铺和商品范围。只读查询与履约任务的默认范围见对应模块。
- `--json` 输出与回执字段见 [AI 汇报口径](AI_GUIDE.md#根据什么汇报)。完整参数见 [CLI 命令参考](CLI.md)，环境设置见 [配置指南](CONFIGURATION.md)。

## 看懂读写边界

| 操作类型 | 命令 | 执行行为 |
| --- | --- | --- |
| 本机检查与预览 | 任意 `--help`、`doctor`、不加 `--apply` 的 `task run` 和 `task schedule` | 检查本机或列出步骤。`doctor` 在缓存失效时短时检查 GitHub，平台鉴权使用 `--check-online` |
| 本地查询、工作台与导出 | `shop list`、`catalog product-summary`、`map workbench`、`map check-mismatch`、`report mapping` | 前两项查询本地快照，其他命令按需生成 HTML 或 Excel |
| 本地归属 | `map batch-set`、`catalog merge`、`catalog rename` | 前两者导入审核 JSON，后者修正归属 |
| 平台查询与报表 | `report products`、`order list`、`product check-dimensions`、`marketing ads`、`marketing audit`、`report vc-sales`、`report delivery`、`report orders`、`report ratings`、`support chats`、`support appeals` | 读取对应平台，按命令生成本地输出。`order list` 默认先同步订单 |
| 商品快照更新 | `sync`、`catalog fetch` | 默认先同步 BCS 快照再读缓存，使用 `--source wb-openapi` 从 WB 官方接口采集商品、价格及各店默认业务仓库存 |
| 商品计划与提交 | `ops`、`apply --plan` | 先预览生成计划单，经运营确认后按单号提交 |
| 其他写入与任务 | `marketing enroll`、`product dimensions`、`product clean`、`listing sync-cards`、履约、飞书和客服写命令、`task run`、`task schedule` | 按各命令规则使用 `--apply`。每日折扣只生成待确认计划 |
| 会话与 AI | `login`、`support ai-test`、`support watch-questions`、`support watch-chats` | 按命令回写凭证或状态，或调用 LLM |

所有 WB/BCS 写入提交并记录结果后结束，不做写后查询，不追加同步或映射表合并。终端中的“提交成功”不是即时最终状态证明。本地日志、接口错误和任务号是本次汇报依据。

官方上架计划提交后，建卡受理后的标识轮询是完成后续上架所需的业务步骤。仅按待办完整 VC 查询本店 nmID/chrtID 并核对生成条码，有时间与轮次上限。图片和库存提交后结束。

## 当前运行约定

- 涉及 WB 仓库的读写只用本店已核实的默认业务仓。配置和 ID 核对见 [仓库范围](CONFIGURATION.md#仓库范围仅允许配置的本店默认业务仓)。
- WB 后台鉴权失败时停止该店操作。会话恢复见 [平台连接配置](CONFIGURATION.md#通过-workbuddy-连接平台)。
- 操作限制与续期边界见 [AI 日常操作指南](AI_GUIDE.md)。
- `doctor` 在本机 `main` 分支优先使用仓库更新缓存，缺失或过期时短时检查 GitHub。`doctor --check-updates` 可跳过缓存。发现可快进的更新时显示手动拉取命令。

## 商品映射与人工审核

`shop list` 显示每店快照来源、同步时间、在架和回收站留存条数。JSON 的 `data.shops` 包含 `snapshot_present`、`active_item_count` 和 `trashed_item_count`。空快照计 0，未同步店的计数为 `null`。
`catalog product-summary` 按中文名查询活跃店铺的已归属、在架货盘商品，逐店展示条数、各规格原价区间、商品折扣区间及默认业务仓库存。原价按币种分组。仓库证据或规格库存记录缺失时，库存显示未知。可用 `--shops` 限定在营店，`--name` 和 `--name-contains` 分别精确或按子串查询。`--sort` 按条数（`count`）、覆盖店数（`shops`）或中文名（`name`）排序，`--limit` 限制返回的中文名组数。两条命令均仅查本地数据。

`doctor`、`shop list` 和 `catalog product-summary` 会检查范围内的在营店铺。改价、库存、下架及按商品定向的 WB 折扣预览有准备范围时也会检查。`data.snapshot_freshness` 区分 `stale`（距上次确认同步默认已满 24 小时）、`cache_unconfirmed`（本次使用 BCS 缓存）、`never_synced`（无快照）和 `unknown_age`（确认时间未知）。缓存采集更新 `last_fetched_at`，沿用原来的 `last_confirmed_sync_at`。用户要求更新时按店单独运行 `sync --shop-id`。

```powershell
python run.py shop list --json
python run.py catalog product-summary --shops 1001,1002 --name-contains "示例" --sort shops --limit 10 --json
```

首次配置且运营要求获取快照时，先运行 `sync --all`，再进入 `map workbench`。`catalog mapping` 打开同一套流程。

```powershell
python run.py sync --all --json
python run.py map workbench --json
```

待认领商品按同源码或同图归组，先看图和出现店铺，再用前缀、倍数参考或中文名确认。点击缩略图放大，使用分类、处理状态及 VC／中文名／价格搜索定位商品。倍数参考按最接近的正整数倍计算，误差在 1 元内或相对误差在 2% 内时展示。

`map candidates --json` 默认返回前 20 组，可用正整数 `--limit` 调整。`data.counts` 统计全部符合范围的商品。`prefix`、`multiplier`、`unmatched` 按组计数，`groups` 为组总数，`vcs` 为跨店去重的完整 VC 总数。`data.limit` 给出本次上限，`returned_groups` 和 `returned_vcs` 对应本次返回的 `data.groups`，`truncated` 表示仍有组未列出。

`map candidates`、`map workbench` 和 `catalog mapping` 在零组时显示当前没有待认领商品，JSON 回执的 `next_action` 为空。两个工作台入口仍会生成可打开的 HTML。

一键采纳只填入尚未选择的前缀命中组，包括筛选隐藏的组。浏览器保存审核进度。可以分批导出，文件每次包含所有已处理组的完整 VC。导出后把 JSON 和页面上的交接话术发给 WorkBuddy，或自行使用以下任一命令导入。

```powershell
python run.py map batch-set --file "_scratch/待认领审核.json" --dry-run --json
python run.py map batch-set --file "_scratch/待认领审核.json" --json
python run.py catalog merge "_scratch/待认领审核.json"
```

`map batch-set` 也可从标准输入读取 JSON 数组，支持 `map`（或 `mapped`）、`rebind`、`exclude`、`block`、`unblock`。禁上架使用 `shops: "ALL"` 或店铺 ID 列表，保留原有归属。两个导入入口都支持 `--dry-run` 只读预检。确认后用同一份审核数据去掉 `--dry-run`，命令重新校验并整批写入本地库。Excel 占用时，关闭提示中的工作簿后重试。导出单店表时，已确认的空店可生成空表，缺快照会提示无法导出该店。

JSON 回执的 `data.counts` 按完整 VC 统计归属（`mapped`）、换绑既有 SKU（`rebound`）、排除（`excluded`）、禁上架（`blocked`）、解除限制（`unblocked`）和业务字段相同（`unchanged`）。`remaining_pending` 为全库在营、在架的唯一待认领 VC 数，`data.dry_run` 为真时是预计值，为假时是本批保存后的值。按顶层 `next_action` 继续核对。`data.updated_vcs` 按输入顺序默认返回前 100 个拟处理或已处理的 VC。`updated_vcs_total` 和 `updated_vcs_truncated` 给出总数和截断状态。预检或保存时可与 `--json` 一起加 `--full`，返回完整列表。

`report mapping` 的映射总表按本地在架快照列出归属。价格表有商品名但目前没有在架归属是正常情况。「待核查清单」列出已归属在架 VC 对应的最低售价缺失项。实际未归属在架商品仍见「未映射商品」或单店表的「未映射在架商品」。总表仍校验价格表的「产品中文名」和「最低售价」表头。

商品从价格表删行后，映射总表的「历史归属」页和单店表的「匹配类型」仍显示历史归属，最低售价留空。`catalog product-summary` 按 SQLite 快照和已保存商品资料汇总，JSON 另列 `historical_item_count` 与 `historical_items`。历史 VC 退出待认领工作台。订单、评价、销量和飞书登记可读最后保存的中文名，依赖现行价格、包装、库存 SKU 和发货配置的流程只取当前商品行。同一卖家 SKU 加回后恢复现行资料。

```powershell
python run.py map check-mismatch --cn "示例商品" --days 7 --json
python run.py report mapping --shop-id 1001
python run.py catalog rename --vc "BCS-TEST-123456789" --cn "修正后的商品名"
```

`map check-mismatch` 和 `catalog check-mismatch` 在 `exports/workbench/` 生成同一份 HTML，每次覆盖上次结果。`--cn` 精确匹配中文名，`--begin`、`--end` 和 `--days` 按创建日期筛选。`map check-mismatch --json` 的回执含 `data.artifacts.html` 及 `data.counts`。计数包含总条数、中文名组数、异常 VC 总数（`anomalies`）、前缀冲突（`prefix_conflicts`）和价格倍数异常（`price_anomalies`）。同一 VC 同时命中两类时，每类计一次，异常总数只计一次。两入口的终端摘要都显示细分计数。

快照可读但没有已归属在架商品，或名称、日期筛选命中零条时，命令仍生成 HTML，全部计数均为 0，`next_action` 为空，退出码为 0。没有可用快照或读取到损坏的快照时会报错。

画廊按中文名并排展示大图，异常组排在前面。程序仅标记前缀冲突和售价偏离最低售价合理正整数倍，图片由运营人工比对。逐店核对在架快照中的现价。各店可以采用不同的合理正整数倍，卡片会显示主店现价和匹配的倍数，例如「×2/×10」。若有店铺的现价偏离整数倍，标记会注明对应店铺。可以多选中文名，按 VC、中文名或俄文标题搜索，或切换「只看异常」「只看已勾选」。点击放大镜查看大图，点击卡片勾选货不对板，浏览器会保存勾选状态。WB 中尺寸图加载失败时依次尝试高清图、原链接。缺少主图链接时用 WB 商品码尝试加载首图。

勾选结果可导出 JSON 或复制逗号分隔的完整 VC。要下架时，核对目标店铺后，将复制的 VC 填入 `ops trash --vc` 预览计划单。

只有用户明确要求获取最新商品数据时，独立运行：

```powershell
python run.py sync --shop-id 1001
python run.py sync --shops 1001,1002 --json
python run.py sync --needs-refresh --json
```

`sync` 和 `catalog fetch` 共用快照流程和 `--json` 回执。默认先同步 WB → BCS，再读取该店 BASE 并保存。普通提交或轮询未确认时会尝试读取 BCS 缓存，`data.sync_warnings` 列出受影响店铺和阶段。`--no-sync` 直接读取缓存。两种情况下，成功保存的店铺均在 `data.fetched` 标注快照状态，摘要明确说明使用了缓存。缓存全部保存成功时退出 0。BASE 读取或保存的失败见 `data.errors`，失败店铺保留原快照。

首次同步前，可将 `examples/商品价格表.example.xlsx` 复制到本次数据目录并改名为 `商品价格表.xlsx`。填写唯一四位 ASCII 字母前缀可启用自动归属。两条命令按该前缀自动归属本次成功保存且在架、无禁上架限制的 `pending` VC，来源为 `prefix`。价格表缺失、占用、无效、版本变化或没有唯一前缀时仍保存合格快照，原因见 `data.fetched[].prefix_skip_code` 和 `data.prefix_mapping.skipped_shops`。

查看 `data.fetched[].prefix_mapped` 和 `data.prefix_mapping` 了解本轮新增归属和跳过原因，逐 VC 的跳过原因按店计次。每家成功保存店铺的 `mapped_vcs`、`pending_vcs` 为在架完整 VC 数量，跨店去重结果见 `data.mapping_counts`。按回执 `next_action` 用 `map check-mismatch` 复核新增归属，或用 `map workbench` 处理待认领。需要人工归属、纠偏或排除时，按完整 VC 用 `map batch-set --file`、`catalog merge` 或 `catalog rename` 处理。严禁直接修改 SQLite `vc_map`。

范围参数 `--shop-id`、`--shops`、`--all` 互斥，默认同步全部在营店铺。`--needs-refresh` 可叠加范围，按本地状态筛选快照超期、缓存未确认、确认时间未知或尚无快照的在营店铺。默认超期阈值为 24 小时，可用 `SELLEROPS_SNAPSHOT_MAX_AGE_HOURS` 调整。补刷支持 BCS 和 Open API 来源，与 `--no-sync` 互斥。首次同步尚未登记到本地的店铺时，使用普通范围参数。远端清单中缺失的本地目标店见 `data.missing_shops`。

批量 BCS 后台同步默认并发 2，`--bcs-parallel` 可设为 1 至 4。BCS 远程阶段使用 30 分钟协作式预算，其中同步提交和轮询最多占用前 25 分钟，单店轮询最多 600 秒。达到同步阶段截止时间后，剩余预算可用于读取 BASE 缓存。最终鉴权失败会停止后续 BCS 同步和 BASE 请求，已经保存的店铺结果继续列在回执中。

```powershell
python run.py catalog fetch --shop-id 1001 --no-sync --json
```

仅在需要绕开 BCS 同步、直接用官方接口重建快照时，显式选择 Open API 来源。该来源使用官方中国区 `.cn` 主机，不要求 WB 后台 Cookie、不触发 BCS 商品同步，也不会在失败时改读 BCS 旧缓存：

```powershell
python run.py catalog fetch --source wb-openapi --shop-id 1001
```

显式 Open API 来源的批量拉取默认最多 **4 店同时采集**。可用 `--parallel` 调整正整数并发数，`--parallel 1` 改为串行，实际并发不超过店铺数量。每店独立使用自己的令牌、会话和默认业务仓 ID，店内分页与请求仍按原有限流顺序执行。完整采集的店铺立即原子保存，终端按完成顺序显示店铺统计和总进度。

```powershell
python run.py catalog fetch --source wb-openapi --parallel 6
```

拉取成功的店铺更新库中快照，失败的店铺保留原快照并记录失败原因，命令返回非零。

显式 Open API 来源的快照包含无图片、零库存商品，创建时间和尺寸重量来自商品卡，规格价格按 ID 对应。缺失价格为 null、缺失图片为空、未返回的库存规格保持空 stockList，并输出缺失统计；空库存记录不代表已确认库存为零。无法确认该店唯一可用的默认业务仓、分页不完整或关键标识无效时，不覆盖该店快照。所有商品写入仍提交并记录后结束，不把 fetch 当即时写后验证。

默认 BCS 来源先核对接口 total 与原始行数，再对本店相同 nmId 和完整 VC 的商品去重：非空业务值一致且存在完整原行时保留该整行，不拼接字段；业务值冲突、标识歧义或没有完整原行时失败并保留旧快照。终端显示原始/保留/移除数量，快照 total/fetchedCount 为去重后行数。单店表生成也检查旧快照中的重复商品。完整规则见 [数据契约](DATA.md#映射快照与标识)。

无规格、无效或重复规格 ID 的商品仍保留完整原行并输出缺失告警，本地映射不显示附带的占位零价格/库存。任一规格库存缺失、无效或重复时，商品库存显示为空；库存操作预览中的未知值也不标成0。总表不会用其他店的库存补本店空值。接口明确返回的有效零值仍保留。BCS 响应缺少 rows、总数与返回条数不一致或混入其他店铺数据时，拉取失败并保留该店原快照，不能按空店继续合并。

## 改价与库存

商品范围与店铺范围各选一项。--shops 接受逗号分隔的明确 ID，--all-shops 表示该通道全部店铺。--name 是完整中文名，--name-contains 才是包含匹配。--all-products 代表全部商品。库存数量必须显式传入，含清零的 --amount 0。

大批量 `ops --json` 预览可能省略 `data.scope`，在 `data.preview` 提供计数、分店汇总和最多 10 条样例。优先查看命令生成的 `plan_csv`、`receipts_csv` 或 `skips_csv`，绝对路径按实际回执读取 `data.artifacts`。支持的子命令可加 `--full-preview` 取完整范围，`ops review-price` 无此选项。

范围明确时直接生成计划预览。运营确认一次后用 `apply --plan` 按对应单号整单提交。

```powershell
python run.py ops price --shops 1001 --name "示例商品" --price 55
python run.py ops price --shops 1001 --name "示例商品" --keep-price --discount 35
python run.py ops stock --shops 1001 --vc "BCS-TEST-123456789" --amount 20
```

确认后提交对应计划（`P018` 为示例单号）。

```powershell
python run.py apply --plan P018 --json
```

`ops price`、`ops discount` 和 `ops stock` 默认使用 WB 卖家后台，需指定店铺与商品范围。`ops review-price` 用 `--shops` 或 `--all-shops` 指定要查询待审核商品的店铺。完整中文名匹配所有归属明确的同名链接，`--sku` 只匹配指定价格表 SKU。同名多行无法判定 SKU 的链接会被阻止，并列出候选 SKU。手动统一原价（吊牌价）使用正整数 `--price`。WB 折扣百分比越大，折后价越低，反之越高。手动库存必须填写 `--amount`，归零先用 `--amount 0` 生成计划，再确认并提交对应单号。

逐店以同币种原价和最低售价基准核对倍率，并分别记录原价、币种、折扣百分比与折后价。常规折后价按 `原价 × (1 − 折扣百分比 / 100)` 计算。额外优惠及平台实取折后价单列并注明来源。

`ops price` 必须选择正整数 `--price`，或使用 `--keep-price` 搭配折扣。BCS 可用 `--channel bcs --club-discount D` 搭配任一价格意图。

`ops stock` 默认使用本地映射及快照中可确认归属的商品筛选供应商代码，并从本地商品快照解析规格 ID（`--resolve snapshot`）。库存归零直接将目标规格设为 0，不需要先联网读取当前库存，即使本地库存未知或已为 0 也照常提交。缺少必要标识时按下表定向补齐；仍无法确认仓库或完整规格的商品会被阻止，预览和执行均以非零状态报告。仓库 ID 仍需逐店确认，更新商品快照使用独立的 `catalog fetch`。

各类操作只依赖真正需要的字段：

| 操作 | 必要数据 | 缺失数据的处理 |
| --- | --- | --- |
| 设置库存 | 本店完整、归属唯一的 chrtId，以及已确认的本店默认业务仓 ID | 不要求旧库存、现价、图片或包装尺寸；规格标识缺失时定向查询本店商品卡 |
| 普通改价 | 本店 nmId 和 `--price` 指定的目标价 | 缺 nmId 时定向补齐 |
| WB `ops price --keep-price --discount` / 不限原折扣的 `ops discount` | 本店 nmId 和目标折扣 | 仅提交折扣，不传价格；缺现价、规格或旧折扣不阻止操作。按原折扣阈值筛选时，预览通过 WB 官方价格接口读取本店商品当前值 |
| 修改包装尺寸/毛重 | 本店 nmId，以及参数或价格表中的目标尺寸 | 不要求旧尺寸、规格或库存；缺 nmId 时定向补齐 |
| 下架 | 本店 nmId、完整 chrtId 和已确认的本店默认业务仓 ID | 不要求旧库存；先补齐必要标识，再提交全部规格清零，清零受理不完整时仍阻止下架 |

商品计数按本店商品卡，库存另列规格计数。摘要区分完整预览、完整提交、已确认无待办、部分成功及失败，店铺范围未知时对应数量为 `null`。按回执路径打开本次逐项明细，默认在 `data/logs/YYYY-MM-DD/商品操作回执_*.csv`。明细区分已受理、明确失败、结果未知、已阻止和正常跳过，`--auto-review` 单列审核结果。

定向补齐使用 BCS 保存的该店官方 apiKey 和中国区接口，只查询本地已有但必要字段不完整的商品；按完整 VC 精确匹配，核对已有 nmId，不从其他店借用标识。完整本地数据不额外查询商品；不自动全量扫描平台、不刷新本地快照、不触发 BCS 同步或映射合并，也不更换写入通道。该店官方鉴权失败会阻止整店提交。限流或网络读取失败时停止该店后续补齐，已具备必要字段的商品可继续。显式 BCS 保价写入（含独立折扣命令）要求每个规格价格可确认、全部相同，且商品级价格可精确到分提交。遇到异价、缺价或超精度时阻止该商品。

按名称或 SKU 筛选使用库中已确认的归属；新快照中待确认的完整 VC 可以先在工作台核对。批量操作完整的跳过明细保存在 `data/logs/YYYY-MM-DD/商品操作跳过_*.csv`。

改价与设库存可显式用 `--channel bcs` 选择 BCS 通道。鉴权失败和提交异常的处理见 [AI 日常操作指南](AI_GUIDE.md)。`ops stock --resolve live` 仍先确认完整规格计划，再仅查询已确认的本店默认业务仓当前库存；实时库存缺行保留未知，不减少待修改的规格，也不补成0。返回其他仓记录或读取失败时阻止该店对应商品并报告。

计划阻止目标价低于或等于已知原价一半的商品。规格 ID 同时被本店不同卡认领时，库存与下架清零均阻止涉及的目标卡，即使另一张卡未被选中。需要审核新价格时，用 `ops review-price` 预览，确认后用 `apply --plan` 提交。用户明确要求整合审核时才使用支持它的 `--auto-review`。待审接口成功返回 `quarantineGoods: null` 时视为空清单。鉴权、格式错误或分页不完整会记录到预览中的 `data.result.failed_shops` 并返回非零。

## 发布商品与跨店补齐

日常上架走 BCS 建卡，通道与命令选型见 [AI 日常操作指南](AI_GUIDE.md#选择与执行命令)。

```powershell
python run.py ops create-cards 123456789 --shops 1001 --sku SKU-1 --vc "BCS-TEST-123456789" --price 59 --dims "10*20*30/0.5" --stock 20 --json
python run.py ops create-cards --file "examples/publish.example.json" --shops 1001
python run.py ops replicate --shops 1001 --name "示例商品" --limit 1
```

这些示例生成固定计划单，经确认后用 `apply --plan` 提交。售价使用指定价格或本地快照价。缺少快照价时请传 `--price`。发布默认库存是 999。需要其他数量时显式设置 `--stock`，也可使用 `--cn-stock` 按中文名指定库存。BCS 建卡随请求提交初始库存。

用 `ops create-cards` 上架须确认价格表中的商品。有卖家 SKU 时可指定 `--sku`，批量 JSON 每行可填 `sellerSku`。没有 SKU 时可用唯一的产品中文名 `--cn`。编码优先保留完整 `--vc`，其次沿用 `--prefix`，然后使用已确认商品在价格表中唯一登记的前缀。缺失或多人共用时补 `--prefix`，也可经运营确认加 `--random-prefix`。随机前缀在预览时生成，随商品键、完整 VC 和编码来源一起保存。平台明确受理后本地保存完整 VC 的归属。升级前未提交的普通上架计划需要重新预览一次。

`ops replicate` 使用建卡协议保留原始完整 VC，同一 VC 在不同源店只选一次。WB 商品码取自选中的源店快照。上架和复制在每家目标店按完整 VC 或相同的最长连续数字段查重，数字段至少 6 位。BCS 建卡和复制使用目标店全状态本地快照及提交记录，回收站中的卡也会跳过。BCS 批量推送在有本地快照时也用它查重。快照滞后时，结合已有操作回执核对范围，并确认在架源商品仍然有效。

复制先按目标店查重和过滤，`--limit` 限制初筛后准备的候选 VC 数。复制以源完整 VC 和目标店组合统计跳过。`data.result.replication` 包含跳过原因和限额前后的候选数。候选为零时也可核对汇总，完整跳过明细见 `data.artifacts.skips_csv`。

BCS 建卡已停用商品详情代理。`ops create-cards` 和 `ops replicate` 默认用 `card.json` 合成详情，生成一条尺码为 `0` 的规格。`ops create-cards --file` 的 JSON 商品项仍可通过 `extra.wb_detail.sizes` 提供规格，建卡只取第一条并写入目标 VC 和价格。`ops replicate` 使用合成规格，源商品的非零尺码及其他规格不会带入新卡。

BCS 独立建卡在预览和提交前逐店检查本机今日已提交张数与本次待提交张数。合计超过 1000 张会显示 `[警告]` 和建议分天或分批上架，计划商品仍可提交。计数按本机提交记录汇总，同店同 VC 只计一次。

Python 入口 `sellerops.features.publishing.card_publish.publish_cards_simple` 接受字典或 `PublishItem`，必填 `nm_id`、`vendor_code`、`price`、`length`、`width`、`height`、`weight` 和 `target_shops`。可直接提供 `card_info`，库存默认 999，无需在价格表绑定商品。默认预览，经确认可用 `apply=True` 提交。`limit` 大于 0 时只选择查重后的前几个有效待提交商品，结果中的 `daily_limit_warnings` 按店列出超额提醒。

复制上架可分别限定源店与目标店，并按商品价格表的「最低售价」定价：

```powershell
python run.py ops replicate --source-shops 1001,1002 --shops 1003 --price-source minimum --price-multiplier 10 --stock 999 --all-products
```

示例生成计划单，经确认后用 `apply --plan` 提交。`--price-source` 默认 `snapshot`（源店价格），倍数默认 1。`minimum` 必须匹配唯一的同中文名最低售价。金额按分四舍五入，库存默认 999，显式 0 保留为 0。包装尺寸与毛重优先映射表，其次价格表，再次公开商品详情。上架前准备整批详情与载荷，逐店确认目标店的默认业务仓；任一目标仓无法确认时阻止该商品组，提交后记录受理响应并结束。

备用官方上架使用 `ops publish-openapi` 预览，经确认用 `apply --plan` 提交。支持自有源店与任意 WB 商品码，依次完成建卡、有限标识等待、图片和本店默认业务仓库存提交。超时用 `ops publish-openapi --resume --shops ... --vc ...` 预览剩余阶段，再确认并提交新单。跨店内容更新使用 `listing sync-cards`，按完整 VC 匹配本店商品码，用该命令的 `--apply` 提交。详见 [Open API 商品写入](OPEN_API_LISTINGS.md)。

历史随机 VC 前缀使用 `ops normalize-vc --shops 1001 --name "示例商品"` 生成计划单。写入只改前缀不一致的商品，保留原尾部；店铺或回收站资料不完整先停止。详见 [VC 前缀规范化](OPEN_API_LISTINGS.md#历史-vc-前缀规范化)。

## 导出标准商品表与导入他人表格上架

在项目根目录运行。导出只读本地快照，生成名为「商品列表」的单 Sheet XLSX，共 13 列，含来源店铺和快照售价。缺少店名时使用店铺 ID，未归属商品的中文名留空。售价取首规格价格，规格库存齐全时汇总库存。`--shops` 可指定单店或逗号分隔的多家店铺，`--all-shops` 选择全部活跃店铺。单店默认写入 `exports/shop_1001_products.xlsx`（以店铺 ID 命名），指定多店或使用 `--all-shops` 默认写入 `exports/shops_products.xlsx`。`--out` 可指定数据目录之外的 `.xlsx` 路径。

导出过滤回收站和人工排除商品。用原 VC 最长的连续数字段（至少 6 位）跨店及表内去重，无满足长度的数字段时使用完整 VC。优先保留售价有效且包装尺寸、毛重齐全的一行，其次按主店优先和店铺 ID 升序选择。终端打印扫描、过滤、去重和导出条数及路径。导出条数按源数字码汇总，店铺快照条数按店统计。外发时保留原表头及「源数字码」、原 `vendorCode`、本店 `nmId` 等交换列。

```powershell
python run.py catalog export-products --shops 1001
python run.py catalog export-products --all-shops
python run.py listing import-publish --file exports/shop_1001_products.xlsx --shops 1002,1003 --prefix ABCD --price-multiplier 1.2 --stock 999 --limit 50
```

导入预览按店显示核实后的默认业务仓名称和 ID。表格校验和目标店跳过明细写入同一份 `商品操作跳过_*.csv`，绝对路径见终端。`--limit` 提前停止时，终端另列已扫描的有效行及未扫描行数。

导入支持标准 `.xlsx` 和同表头的 UTF-8 `.csv`。`--prefix` 使用 4 位大写英文字母，目标编码为 `BCS-ABCD-源数字码`，依次取表中有效的源数字码、原 VC 的最长数字段（至少 6 位）或本店 nmId。`--price` 可覆盖整批售价，金额四舍五入到分。无有效 nmId、售价或包装资料的行会逐条列出并跳过。导入按同源商品和目标 VC 去除表内重复行。上架库存由 `--stock` 指定，默认 999。

预览会读取目标店默认业务仓和公开商品详情。先核对目标店、编码、售价、包装、库存、跳过原因和每日限额提醒，再经运营明确确认后在同一命令末尾加 `--apply`。`--limit` 按查重及载荷校验后的待提交商品分批，0 表示不限。两条命令均可用 `--data-dir` 指定该次运行的数据目录。

## 尺寸、下架和清理

```powershell
python run.py product dimensions --shops 1001 --name "示例商品"
python run.py product dimensions --shops 1001 --vc "BCS-TEST-123456789" --dims "10*20*30/0.5"
python run.py product check-dimensions --shops 1001 --type all
python run.py ops trash --shops 1001 --vc "BCS-TEST-123456789"
python run.py product banned --shops 1001 --limit 10
python run.py product clean --target draft --shops 1001 --limit 10
```

- `product dimensions` 沿用显式商品范围，确认后加 `--apply`。省略 `--dims` 时使用每个完整 VC 对应价格表行的包装尺寸。
- `ops trash` 从本地快照读取商品和全部规格 ID 生成计划。经确认用 `apply --plan` 提交，由计划先清本店默认业务仓库存，再移回收站。无法确认唯一可用的默认业务仓时阻止该店，商品 ID 或任一规格 ID 缺失时阻止该商品。
- 清库存失败或受理回执不完整时，受影响商品不会移回收站，其他正常商品继续；命令返回非零状态，日志记录被阻止原因。预览发现不完整计划也返回非零状态。收到清零受理回执后直接进入回收站提交，不进行写后查询验证。`ops trash --channel bcs` 同样遵守清库存前提，是独立 BCS 通道，不作为 WB 失败后的自动兜底。
- `product banned` 可列出被阻止的商品。要下架时用清单中的完整 VC 生成 `ops trash` 计划并确认单号。
- `product clean` 用 `--target` 选择草稿箱、回收站或两者。预览和执行会分别重新读取清单，`--limit` 只限制本次数量。删除须加 `--apply --yes`。回收站需要归零时通过 BCS 提交本店默认业务仓的规格清零，归零受理后才通过 WB 删除。结果依据按商品 ID 返回的受理回执。

这些操作不能仅凭“有 dry-run”就扩大范围或自动确认。

## 折扣、促销和推广审计

```powershell
python run.py ops discount --shops 1001 --name "示例商品" --target 50
python run.py ops discount --shops 1001 --threshold 55 --below 40 --target 50 --all-products
python run.py ops discount --channel bcs --shops 1001 --threshold 50 --target 50 --all-products
python run.py marketing enroll --shops 1001
python run.py ops review-price --shops 1001
python run.py marketing ads --shops 1001
python run.py marketing audit --shops 1001 --mode both --days 7
```

折扣和价格审核用计划单提交，促销报名使用该命令的 `--apply`。查询或审计结果位置以终端打印为准。独立折扣命令的 `--target` 必填，接受 0 至 100。对指定商品无阈值设折扣时，每条新指令均提交赋值，本地快照中的旧值不参与跳过判断。定向条件命令在预览时读取本店平台当前折扣并固定范围，`--threshold` 严格大于，`--below` 严格小于，两侧取并集。WB 全店按折扣排序分页，无法确认范围完整时整店零提交，完整的其他店铺可以继续。显式 BCS 折扣仅支持全店，可加 `--threshold`。

`marketing audit` 依赖飞书订单登记、目标店快照和推广信息，按目标店合计销量与各店推广状态计算缺口/浪费。它只输出建议，不自动创建或关闭广告，不应把其他店的商品码直接用于本店写入。

## 订单履约与飞书

```powershell
python run.py order list --shops 1001 --days 1 --no-sync
python run.py order match
python run.py order forecast --check
python run.py order process
python run.py order register
python run.py order deduplicate --limit 20
python run.py order stock-register
python run.py order stock-days
```

`order list` 不加 --no-sync 时会先同步订单。其 days 和其他命令的 days 不一定是同一日期口径。

马帮会话可用 `python run.py login mabang` 单独更新，需验证时在官网操作。启用自动续期时，马帮预览和执行均可更新本地认证。业务写请求若收到认证失败，先根据已有回执核对已提交部分，再继续处理。

商品价格表的「发货方式」决定订单分流。「马帮发货」须填写「马帮库存SKU」，才能进入马帮匹配、预报和交运。「WB发货」走下述 WB 供货流程，「不处理」跳过发货。发货方式留空、填错或马帮库存 SKU 缺失时，马帮匹配报告会标记 `NO_SKU` 并列出原因。

「处理新增订单」用 `order process` 预览，经运营确认后加 `--apply`。默认覆盖 `mabang.shop_map` 配置店铺的全部新增待处理订单，不限日期。处理顺序为库存 SKU 匹配、预报上传、成功状态轮询和交运。有飞书地址（`--url` 或 `feishu.base_url`）且能找到 `lark-cli` 时再登记。缺任一项会提示并跳过登记，马帮流程成功时仍返回 0。马帮处理失败或实际尝试登记失败时返回非零。

`order match`、`order forecast` 及飞书登记合并的待处理单也使用相同日期语义：`--days 0`（默认）不发送起止日期限制；明确要处理最近 N 天时传正数，例如 `--days 7`。仍只处理配置店铺与新增待处理状态，不包含其他店铺或历史已完成订单。列表完整翻页，达到安全上限或重复分页时报错，不用部分结果继续执行。更换失败、缺 SKU、预报失败或未完成的订单会按流程阻断后续履约，依据本次日志处理，不删除状态后盲目重跑。

预报状态等待是交运前必要步骤，不能当成写后验证而跳过。`--check` 只是查看预报批次，`order forecast --apply` 才执行整条预报/交运链。

`order register --apply` 按订单编号去重增量登记，latest 模式合并最近订单和待处理订单。历史补录可明确指定：

```powershell
python run.py order register --scope all --date 2026-10-01
```

用户确认后再加 --apply。去重命令只清已有重复记录，库存全量登记则会清空并重建库存表；都不能当作每天的无害初始化。`order stock-days` 默认补今天和更新现有日期列，显式 --begin/--date 会进入可删除旧日期列的区间模式，详见 [数据契约](DATA.md)。

`order register` 和 `order process` 的登记步骤分别显示「本次待登记」和「去重前候选行」的留空量。本次待登记按订单编号去除表内已登记及本批重复后计数。扫描汇总另列候选行、真实排除、表内已登记跳过和本批重复。CSV 的重复行标为「跳过-本批重复」。登记步骤生成的 `--json` 回执可读 `data.pending_orders`、`data.pending_blank_counts`、`data.candidate_blank_counts` 和导出路径 `data.artifacts.csv`。`order process` 跳过飞书时没有这些登记字段。

### WB 发货群供货与 PDF

`order group-ship` 单独查询 BCS 配置店铺的 WB FBS 新增订单。逐店按完整 VC 匹配商品价格表，在本店已确认的默认业务仓内为「WB发货」商品组建供货。同店同名合为一个供货。目标仓订单未认领或发货方式留空、无效时列为阻止项。店铺无法确认唯一可用的默认业务仓时停止该店，「WB发货」订单缺少可确认的仓库 ID 时阻止该单，明确属于其他仓库的订单跳过。

马帮把 WB 发货订单记录为 `GROUP_SHIPPING`，跳过库存匹配更换、预报和交运。飞书仍按订单编号登记，「库存SKU」留空。空 SKU 在马帮库存日统计中计入无 SKU 订单。

```powershell
python run.py order group-ship
python run.py order group-ship --shops 1001 --name "示例商品"
python run.py order group-ship --shops 1001 --name "示例商品" --apply
python run.py order group-ship --shops 1001 --resume --apply
```

预览会在 `data/logs/YYYY-MM-DD/` 写入 JSON 明细，路径见终端输出。加 `--apply` 后才建供货、加入全部订单、提交配送，再按本次供货号和订单 ID 取 WB 原码。加入每次最多 100 单，超过时分次加入同一个供货。订单的货物尺寸、跨境或 B2B 属性无法合并，或缺平台必填标识时，整组留待人工处理。

供货名如 `20261111-103500_示例店铺(1001)_示例商品_2单`，使用新加坡时间。PDF 用较短的文件名，例如 `示例店铺(1001)_示例商品_2单_1234567_供货码.pdf` 和 `示例店铺(1001)_示例商品_2单_1234567_标签.pdf`，数字编号对应 WB 供货 `WB-GI-1234567`。按供货创建日建文件夹，例如 `exports/fbs_shipments/20261111/`，同一天所有店铺的 PDF 集中保存，续办沿用原供货日期。供货二维码是 58 × 40 mm 的一页，订单标签是 40 × 30 mm 的每单一页。两份文件都通过检查并保存后才记录为导出完成。

配送已受理但 PDF 未导出时，用 `--resume --apply` 按原供货号和文件名补取；只加 `--resume` 查看记录。部分加入失败或写入结果不明时，按 `data/state/WB_FBS发货群供货.json` 中的批次回执人工处理。两份 PDF 是配送后的业务产物，结束后不附加回读验证或同步。

### WB 已供货订单的马帮侧作废

WB 配送受理后，马帮仍滞留的待处理单可用 `order mabang-void-wb` 清理。需要飞书登记时，先用 `order register --apply` 或现有 `order process --apply` 完成登记，以免该单从马帮待处理列表移除后被漏登。

```powershell
python run.py order mabang-void-wb --supply-id WB-GI-1234567 --json
python run.py order mabang-void-wb --shops 1001,1002 --json
python run.py order mabang-void-wb --json
python run.py order mabang-void-wb --supply-id WB-GI-1234567 --apply --json
```

默认只预览，核对范围和逐单明细后加 `--apply`。供货号可搭配 `--shops`，省略店铺时按本地日志确认唯一归属。只填 `--shops` 时扫描指定店铺，两项均省略时扫描所有已受理供货。批量模式先按马帮待处理订单缩小范围，指定供货时会列出该供货的全部订单，包括未在马帮待处理查到的单。

供货日志的加单和转配送必须有受理记录，WB 同店实时状态须证明订单仍在原供货且已转配送。马帮内按店铺和平台单号核对，要求价格表完整 VC 仍为「WB发货」、中文名一致，且未预报、未选渠道、未交运、未开票。只向马帮配货库提交作废。作废命令独立执行，飞书登记、WB 供货及 `--resume` 补 PDF 各走原入口。

预览和提交都在 `data/logs/YYYY-MM-DD/` 生成逐单 CSV，`data.artifacts.csv` 返回实际路径。`BLOCKED` 表示未通过校验，`FAILED` 表示马帮拒绝，`ACCEPTED` 表示马帮已受理，`UNKNOWN` 表示须人工核对。出现未知结果时先核对马帮实际状态，保留本地账本，勿重发。

## 查询报表与客服

商品运营综合报告使用 BCS、WB Open API 和已有本地快照生成 Excel，无需飞书或 `lark-cli`。旧参数 `--url` 和 `--table` 已移除。

```powershell
python run.py report products
python run.py report products --shops 1001,1002 --feedback-days 365 --out exports/product_report.xlsx
```

报告按 UTC+8 下单日统计，含今天。`--order-days` 控制退货统计窗口，默认 30 天。实际采集至少近 14 天，有效出单固定展示近 7/14 天，口径 A 仅计在途和买断，每行一件。评价默认取接口可访问的历史。默认输出本机目标店存在的低评分（≤3.5）、高退货率（完单口径兼容退货率≥30%）商品，以及排除已知风险且中文名可匹配价格表的近期出单候选。简洁表中的风险和缺口按 VC 最长数字段判断。加 `--details` 可查看订单明细和各项汇总。缺失数据会标为未知，报告有错误时仍导出并返回非零。[报告说明](PRODUCT_REPORT.md)

`report ratings` 使用 BCS 店铺对应的 WB Open API 令牌读取未处理、已处理和归档评价，按评价 ID 去重后以 UTC+8 的创建日筛选。默认近 365 天，含今天。`--days 0` 取接口可访问的全部历史。`--wb-region` 可选 `ru`（默认）或 `cn`。BCS 会话失效时通过 `login bcs` 更新。

```powershell
python run.py report ratings --shops 1001 --days 7 --stars 4,5 --json
python run.py report ratings --shops 1001 --days 30 --negative
python run.py report ratings --shops 1001 --days 7 --min-feedbacks 0
```

同店同一 `nmId` 汇总评价数、五档星数和窗口平均分（每条评价同权）。好评用 `--stars 4,5`，差评用 `--negative` 或 `--bad`（1 或 2 星）。命中商品仍展示全星级分布。可按完整中文名、名称子串、前缀或 VC 子串组合筛选。`--limit` 在每店完整聚合并排序后截取送入筛选的商品。`--min-feedbacks 0` 按同店在架快照补入零评价商品，零评价行的评分列为 0。评分条件仅匹配至少有一条评价的商品。返回非零时查看 `data.failed_shops`，可确认的结果仍导出，文件路径见 `data.artifacts`。

```powershell
python run.py report vc-sales --json
python run.py report vc-sales --shops 1001 --days 7 --json
python run.py report delivery --shops 1001 --days 7
python run.py report orders --shops 1001 --days 30
python run.py support appeals --shops 1001 --days-max 5
python run.py report ratings --shops 1001 --one-star
python run.py support questions --shops 1001 --no-detail
python run.py support chats --shops 1001 --limit 10 --no-detail
```

`report vc-sales` 读取 WB 订单动态，默认统计北京时间（`Asia/Shanghai`，UTC+8）近 7 天（含今天）。服务端按更新时间粗筛，本地按下单日期精筛。每行计一件，取消和退货仍计入下单总量，并列出退货数、退货率及未知状态提示。默认在 `exports/` 导出按商品中文名和按完整 VC 汇总的 Excel，零单时也导出两张表。用 `--out` 指定路径，`--top` 只限制终端展示。JSON 回执在 `data.artifacts.workbook` 提供文件路径。采集不完整时返回非零并列出 `data.failed_shops`。该命令可独立于飞书使用。

报告默认写入 `exports/`，部分命令支持 `--out`。`report orders` 默认时区为 America/Los_Angeles，统计窗口按该时区解释，不要直接当成本机当天；导出还区分订单创建时间/更新时间的口径。

提问回复先确定店铺和提问 ID：

```powershell
python run.py support questions --shops 1001 --question-id "QUESTION_ID" --reply "回复内容"
python run.py support questions --shops 1001 --question-id "QUESTION_ID" --reply "回复内容" --apply
```

第一条预览，第二条发送。批量回复还需 --reply-all 与 --yes；不能因一条提问授权而回复所有店铺全部提问。

```powershell
python run.py support watch-questions --shops 1001 --once
python run.py support watch-questions --shops 1001 --mode back --once
python run.py support watch-chats --shops 1001 --mode back --once
python run.py support ai-test --qa "examples/ai_test_qa.example.json"
```

监听默认 front，只展示并记录已展示状态；back 会调用 LLM 生成草稿，仍需 --apply 才自动提交/发送。--apply 也会切换到后台模式。不加 --once 会持续轮询；停止用 Ctrl+C。`support watch-chats` 默认只处理最近 72 小时，--since-hours 0 会扩大到历史消息，不作为普通补跑参数。

`support ai-test` 会联网调用 LLM 并在 `exports/` 写对照报告，不向买家发送；不要与 `scripts/check.py` 的离线测试混淆。

## 每日任务与 Windows 计划任务

```powershell
python run.py task run morning --all-shops
python run.py task run adjust-discounts --all-shops
python run.py task schedule
```

`task run` 默认只打印步骤。`task run adjust-discounts --apply` 生成待确认折扣计划单。`task run morning --apply` 提交促销报名，报名步骤成功后生成待确认折扣计划单。折扣单号见本次日志，经运营确认后用 `apply --plan` 提交。

```powershell
python run.py task run morning --shops 1001 --apply
python run.py task run adjust-discounts --shops 1001 --apply
python run.py task schedule --all-shops --apply
python run.py task schedule --shops 1001,1002 --apply
python run.py task schedule --remove --apply --yes
```

`task schedule` 注册 SellerOps_Morning（09:00）及 SellerOps_Check11/15/19（11:00、15:00、19:00），按运行电脑本地时间执行。Windows 计划任务沿用 `task run --apply` 的行为，早间任务提交促销报名，四项任务的折扣步骤都只生成待确认计划。可用 `--shops` 指定店铺或用 `--all-shops` 覆盖各步骤的可用店铺。每日折扣沿用 WB，目标为 `--target 50`。

临时店铺范围、倍率或折扣规则只用于本次确认的计划。运营明确要求变更内置计划任务时才使用 `task schedule`。

注册时固定当前解释器、代码路径和数据路径；移动目录或换解释器后需重新注册，并清理不再使用的任务以免重复执行。`task run` 每步最多等待 1 小时，某步非零退出会停止后续步骤。日志在 `data/logs/YYYY-MM-DD/daily_YYYYMMDD.log`。

## 定期清理本地文件

```powershell
python run.py task clean --json
python run.py task clean --days 30 --apply
```

`task clean` 默认预览，按目录报告待删除的文件数、空目录数、可释放 MB 和跳过的重解析点数。`--days` 为正整数，默认 20。加 `--apply` 后，删除修改时间早于该期限的文件及空的子目录，保留清理根目录。

清理范围是当前数据目录的 `logs/`、同级 `exports/`，以及使用项目默认数据目录时的 `_scratch/`。整个 `exports/workbench/` 和 `_scratch/README.md` 始终保留。遇到符号链接或目录联接时跳过并提示人工确认。`--json` 结果的分目录统计在 `data.groups`。
