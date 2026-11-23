# 日常操作流程

## WorkBuddy 入门

```powershell
python run.py doctor --json
python run.py sync --all --json
python run.py shop list --json
python run.py map candidates --limit 20 --json
```

体检默认只查本机。显式 `--check-online` 进行只读鉴权。用户要求拉取快照时运行 `sync`，已在 BCS 同步时可加 `--no-sync`。完整列表发现缺失店铺时返回 `missing_shops`，由运营确认后按店铺 ID 执行 `shop archive` 或 `shop restore`。

`map batch-set` 从标准输入或 `--file` 读取 JSON 数组。例 `[{"vc":"BCS-TEST-123","action":"map","sku":"SKU-1"}]`。动作可用 `map`、`rebind`、`exclude`、`block`、`unblock`。禁上架使用 `shops: "ALL"` 或店铺 ID 列表，保留原有归属。
也可导出六列审核表，最终确认列填唯一商品中文名、卖家 SKU 或「排除」，留空继续待定。

```powershell
python run.py map export-pending --json
python run.py map import-excel exports/待确认商品.xlsx --json
python run.py ops price --shops 1001 --name "示例商品" --price 55 --json
python run.py apply --plan P018 --json
```

`P018` 为示例，提交时用本次预览的单号。改价、折扣、库存、下架、上架、改码的旧命令也可用于预览，实际写请求通过 `apply --plan` 发送。每日折扣任务会生成待确认单号。

命令均在项目根目录执行，店铺 ID `1001`、商品码 `123456789` 和商品名仅为示例。批量商品命令要求明确指定店铺和商品范围。只读查询与履约任务的默认范围见对应命令。完整参数见 [CLI](CLI.md)，配置见 [CONFIGURATION](CONFIGURATION.md)。

`doctor`、`sync`、`shop`、`map`、`ops`、`apply`、`report` 加 `--json` 后，标准输出始终是一条 JSON。进度和日志走 stderr；失败也会返回中文摘要和下一步提示。报表文件路径见 `data.artifacts`。

## 看懂读写边界

| 操作类型 | 命令 | 执行行为 |
| --- | --- | --- |
| 不联网的帮助/任务预览 | 任意 `--help`、daily/schedule 不加 --apply | 显示定义，不执行平台业务任务 |
| 平台查询与报表 | shops、product-report、orders、dims-check、promo-goods、promo-gap、feishu-vc-stats、fbs-delivery、order-stats、chats、appeals、ratings | 读取对应平台，通常生成本地输出；orders 默认先同步订单 |
| 本地工作台与映射 | catalog 下的工作台、审核导入和纠偏，report mapping | 使用本地库，按需生成 HTML 或 Excel |
| 数据更新 | fetch | 默认走 BCS 快照（先同步再读 BASE 缓存）；显式 --source wb-openapi 才从 WB 官方接口完整采集商品、价格及莫斯科仓库存 |
| 业务写入 | 商品改价、折扣、库存、下架、上架、改码 | 用 ops 预览并按单号 apply。清理、促销、履约、飞书和客服按各自命令提交。 |
| 会话/AI | login、ai-test、客服监听 | 可能回写凭证/状态或调用 LLM；细节见下文 |

所有 WB/BCS 写入提交并记录结果后结束，不做写后查询，不追加同步或映射表合并。终端中的“提交成功”不是即时最终状态证明。本地日志、接口错误和任务号是本次汇报依据。

`listing publish-openapi` 的建卡受理后标识轮询是完成后续上架所需的业务步骤：仅查询待办完整 VC 的本店 nmID/chrtID 和生成条码，有时间与轮次上限；图片/库存提交后不查询最终结果。

## 当前运行约定

涉及仓库的读取与修改，**默认且仅允许各店自己的莫斯科仓库 ID**。这包括库存、下架前清库存、回收站归零和上架；“全店”“全部商品”也只在莫斯科仓范围内操作。先核对仓库名称和 `id`，不跨店复用 ID，不把 office 字段 `warehouseId` 当成库存仓库；没有可确认的莫斯科仓时中止，不换成都或其他俄罗斯地区仓。详见 [仓库范围](CONFIGURATION.md#仓库范围仅允许莫斯科)。

WB 后台鉴权失败时先停止该店操作。用户可独立运行 `login wb`，按 [平台连接配置](CONFIGURATION.md#通过-workbuddy-连接平台) 在专用浏览器恢复会话。

## 商品映射与人工审核

已有原 data 时，按需要生成工作台：

```powershell
python run.py catalog mapping
python run.py catalog review
python run.py catalog check-mapping --tol 5
python run.py catalog check-mismatch --cn "示例商品" --days 7
```

按输出路径打开 `data/workbench/` 中的 HTML，核对候选归属、图片、中文名或商品码。`catalog check-mismatch` 滚动时加载 WB 中尺寸图，失败时自动尝试高清图和原链接，点击图片可放大。映射行缺少主图链接时，会尝试使用该行的 WB 商品码加载首图。

默认 mapping 和 review 的 VC 中心审核导出交给 merge；不同工作台的结果协议不能混用：

```powershell
python run.py catalog merge "_scratch/review.json"
python run.py report mapping --shop-id 1001
python run.py catalog rename --vc "BCS-TEST-123456789" --cn "修正后的商品名"
```

这些命令直接更新本地映射或状态。旧单店格式核对结果使用 `catalog import-mapping 文件.json`，不要用它替代多店 review→merge 流程。`catalog merge` 不从平台重新拉取商品，也不表示远端状态已经更新。

审核导入和纠偏直接写入本地库。导出单店表时，已确认的空店可以生成空表；缺快照会提示无法导出该店。

如果提示 Excel 占用，关闭提示中的工作簿，重新运行刚才的命令。程序会自动续完已准备的本地文件，即使原审核文件已移走也能完成。请等终端显示保存完成后再打开表格。若改为运行新的映射写命令，程序会先续完上一批，再处理新请求。

只有用户明确要求获取最新商品数据时，独立运行：

```powershell
python run.py catalog fetch --shop-id 1001
python run.py catalog merge
```

`catalog fetch` 默认等同于 `catalog fetch --source bcs`：先同步 WB → BCS，再按 BASE 读取该店缓存并原子保存快照，保留原有 BCS 行契约。用户已经在 BCS 后台同步过时可加 `--no-sync` 跳过同步直接读取上次结果；同步失败不会中止本次拉取，命令按原流程打印警示后继续读旧缓存。默认读取全部店铺；`--shop-id` 只更新指定店铺。

```powershell
python run.py catalog fetch --no-sync
python run.py catalog merge
```

仅在需要绕开 BCS 同步、直接用官方接口重建快照时，显式选择 Open API 来源。该来源使用官方中国区 `.cn` 主机，不要求 WB 后台 Cookie、不触发 BCS 商品同步，也不会在失败时改读 BCS 旧缓存：

```powershell
python run.py catalog fetch --source wb-openapi --shop-id 1001
python run.py catalog merge
```

显式 Open API 来源的全店拉取默认最多 **4 店同时采集**。可用 `--parallel` 调整正整数并发数，`--parallel 1` 改为串行；实际并发不超过店铺数量。每店独立使用自己的令牌、会话和莫斯科仓 ID，店内分页与请求仍按原有限流顺序执行。完整采集的店铺立即原子保存，终端按完成顺序显示店铺统计和总进度。`--shop-id` 单店拉取及默认 BCS 来源不受此参数影响。

```powershell
python run.py catalog fetch --source wb-openapi --parallel 6
```

拉取成功的店铺更新库中快照，失败的店铺保留原快照并记录失败原因，命令返回非零。

显式 Open API 来源的快照包含无图片、零库存商品，创建时间和尺寸重量来自商品卡，规格价格按 ID 对应。缺失价格为 null、缺失图片为空、未返回的库存规格保持空 stockList，并输出缺失统计；空库存记录不代表已确认库存为零。无法确认该店唯一可用莫斯科仓、分页不完整或关键标识无效时，不覆盖该店快照。所有商品写入仍提交并记录后结束，不把 fetch 当即时写后验证。

默认 BCS 来源先核对接口 total 与原始行数，再对本店相同 nmId 和完整 VC 的商品去重：非空业务值一致且存在完整原行时保留该整行，不拼接字段；业务值冲突、标识歧义或没有完整原行时失败并保留旧快照。终端显示原始/保留/移除数量，快照 total/fetchedCount 为去重后行数。单店表生成也检查旧快照中的重复商品。完整规则见 [数据契约](DATA.md#映射快照与标识)。

无规格、无效或重复规格 ID 的商品仍保留完整原行并输出缺失告警，本地映射不显示附带的占位零价格/库存。任一规格库存缺失、无效或重复时，商品库存显示为空；库存操作预览中的未知值也不标成0。总表不会用其他店的库存补本店空值。接口明确返回的有效零值仍保留。BCS 响应缺少 rows、总数与返回条数不一致或混入其他店铺数据时，拉取失败并保留该店原快照，不能按空店继续合并。

`catalog merge 文件.json` 只保存审核确认的归属。没有审核文件时，查看 Excel 使用 `report mapping`。

## 改价与库存

商品范围与店铺范围各选一项。--shops 接受逗号分隔的明确 ID，--all-shops 表示该通道全部店铺。--name 是完整中文名，--name-contains 才是包含匹配。--all-products 代表全部商品。库存数量必须显式传入，含清零的 --amount 0。

先看预览，确认后才执行相同范围的写入：

```powershell
python run.py ops price --shops 1001 --name "示例商品"
python run.py ops price --shops 1001 --name "示例商品" --from-price-book --discount 35
python run.py ops stock --shops 1001 --vc "BCS-TEST-123456789" --amount 20
```

| 用户意图 | WorkBuddy 首选命令 | 修改字段 |
| --- | --- | --- |
| 按表改价 | `product price` | 对应价格表行的实际售价，向下取整 |
| 指定价格 | `product price --price P` | 价格 |
| 同时指定价格与折扣 | `product price --price P --discount D` | 价格、折扣 |
| 按表改价并设折扣 | `product price --from-price-book --discount D` | 对应价格表行的价格、折扣 |
| 只设折扣 | `product discount --target D` | 折扣 |
| 设置库存 | `product stock --amount N` | 本店莫斯科仓的每个目标规格库存 |

三项命令默认使用 WB 卖家后台。每条命令都需加店铺范围与商品范围。完整中文名匹配所有归属明确的同名链接，`--sku` 只匹配指定价格表 SKU。同名多行无法判定 SKU 的链接会被阻止，并列出候选 SKU。手动统一价格使用正整数 `--price`。手动库存必须填写 `--amount`，归零先用 `--amount 0` 生成计划，再确认并提交对应单号。

`product price --keep-price --discount D` 仍可只设折扣。单独给 `--keep-price` 或单独给 `--discount` 会报参数错误。`--price`、`--keep-price`、`--from-price-book` 互斥。BCS 俱乐部折扣也需明确价格意图，参数为 `--channel bcs --club-discount D`。

`product stock` 默认使用本地映射及快照中可确认归属的商品筛选供应商代码，并从本地商品快照解析规格 ID（`--resolve snapshot`）。库存归零直接将目标规格设为 0，不需要先联网读取当前库存，即使本地库存未知或已为 0 也照常提交。缺少必要标识时按下表定向补齐；仍无法确认仓库或完整规格的商品会被阻止，预览和执行均以非零状态报告。仓库 ID 仍需逐店确认，更新商品快照使用独立的 `catalog fetch`。

各类操作只依赖真正需要的字段：

| 操作 | 必要数据 | 缺失数据的处理 |
| --- | --- | --- |
| 设置库存 | 本店完整、归属唯一的 chrtId，以及已确认的莫斯科仓 ID | 不要求旧库存、现价、图片或包装尺寸；规格标识缺失时定向查询本店商品卡 |
| 普通改价 | 本店 nmId 和目标价 | 不要求规格库存或旧包装尺寸；缺 nmId 时定向补齐，目标价仍取手工参数或价格表 |
| WB `product price --keep-price --discount` / 不限原折扣的 `product discount` | 本店 nmId 和目标折扣 | 仅提交折扣，不传价格；缺现价、规格或旧折扣不阻止操作。按原折扣阈值筛选时，每轮通过 WB 官方价格接口读取本店商品当前值 |
| 修改包装尺寸/毛重 | 本店 nmId，以及参数或价格表中的目标尺寸 | 不要求旧尺寸、规格或库存；缺 nmId 时定向补齐 |
| 下架 | 本店 nmId、完整 chrtId 和已确认的莫斯科仓 ID | 不要求旧库存；先补齐必要标识，再提交全部规格清零，清零受理不完整时仍阻止下架 |

`product price`、`product stock`、`product discount` 和 `report products` 在标准输出末尾附加一行 UTF-8 JSON。用 `artifacts` 定位本次实际生成的 CSV 或 Excel。商品计数按本店商品卡，库存另列规格计数。摘要区分完整预览、完整提交、已确认无待办、部分成功及失败，店铺范围未知时对应数量为 `null`。改价和库存的历史 `ops_result.csv` 沿用原表头。逐项回执区分已受理、明确失败、结果未知、已阻止和正常跳过，`--auto-review` 单列审核结果。

定向补齐使用 BCS 保存的该店官方 apiKey 和中国区接口，只查询本地已有但必要字段不完整的商品；按完整 VC 精确匹配，核对已有 nmId，不从其他店借用标识。完整本地数据不额外查询商品；不自动全量扫描平台、不刷新本地快照、不触发 BCS 同步或映射合并，也不更换写入通道。该店官方鉴权失败会阻止整店提交。限流或网络读取失败时停止该店后续补齐，已具备必要字段的商品可继续。显式 BCS 保价写入（含独立折扣命令）要求每个规格价格可确认、全部相同，且商品级价格可精确到分提交。遇到异价、缺价或超精度时阻止该商品。

按名称或 SKU 筛选使用库中已确认的归属；新快照中待确认的完整 VC 可以先在工作台核对。批量操作完整的跳过明细保存在 `data/logs/商品操作跳过_*.csv`。

改价与设库存默认走 WB；`--channel bcs` 是另选的 BCS 通道。WB 网页会话失效时先用 `login wb` 恢复，不要让 AI 自动换通道提交同一批可能已被接受的操作。`product stock --resolve live` 仍先确认完整规格计划，再仅查询已确认的本店莫斯科仓当前库存；实时库存缺行保留未知，不减少待修改的规格，也不补成0。返回其他仓记录或读取失败时阻止该店对应商品并报告。

计划在预览和执行前均阻止目标价低于或等于已知原价一半的商品。规格 ID 同时被本店不同卡认领时，库存与下架清零均阻止涉及的目标卡，即使另一张卡未被选中。明确需要审核新价格时，可单独预览/执行 `product review-price`；用户明确要求整合审核时才使用支持它的 `--auto-review`。

## 发布商品与跨店补齐

```powershell
python run.py ops publish 123456789 --shops 1001 --sku SKU-1 --price 59 --dims "10*20*30/0.5" --stock 20 --json
python run.py ops create-cards 123456789 --shops 1001 --sku SKU-1 --vc "BCS-TEST-123456789" --price 59 --dims "10*20*30/0.5" --stock 20 --json
python run.py ops publish --file "examples/publish.example.json" --shops 1001
python run.py ops replicate --shops 1001 --name "示例商品" --limit 1
python run.py ops import "_scratch/foreign.xlsx" --shops 1001 --limit 1
```

这些示例生成固定计划单，经确认后用 `apply --plan` 提交。发布默认库存是 999。需要其他数量时显式设置 `--stock`，跨店/导入可使用 `--cn-stock`。默认 BCS 上架把初始库存随建卡或推送请求提交。

普通上架须确认价格表中的商品。有卖家 SKU 时可指定 `--sku`，批量 JSON 每行可填 `sellerSku`。没有 SKU 时可用唯一的产品中文名 `--cn`。编码优先保留完整 `--vc`，其次沿用 `--prefix`，然后使用已确认商品在价格表中唯一登记的前缀。缺失或多人共用时补 `--prefix`，也可经运营确认加 `--random-prefix`。随机前缀在预览时生成，随商品键、完整 VC 和编码来源一起保存。批量接口无法原样生成的完整 VC 改用 `ops create-cards` 预览。平台明确受理后本地保存完整 VC 的归属。升级前未提交的普通上架计划需要重新预览一次。

`listing replicate` 使用建卡协议保留原始完整 VC，同一 VC 在不同源店只选一次；WB 商品码取自选中的源店快照，不从 VC 尾段猜测。默认使用本地快照和提交记录计算缺失目标，不逐商品联网扫描列表。目标本地全状态快照中已有的卡（包括回收站）会跳过；此命令不自动恢复回收站卡。快照可能滞后，执行前需结合已知操作回执核对范围，不能把刚下架的旧快照行再次当作在架源商品。

BCS 建卡已停用商品详情代理。`listing create-cards` 和 `listing replicate` 默认用 `card.json` 合成详情，生成一条尺码为 `0` 的规格。`listing create-cards --file` 的 JSON 商品项仍可通过 `extra.wb_detail.sizes` 提供规格，建卡只取第一条并写入目标 VC 和价格。`listing replicate` 使用合成规格，源商品的非零尺码及其他规格不会带入新卡。

复制上架可分别限定源店与目标店，并按商品价格表的「最低售价」定价：

```powershell
python run.py ops replicate --source-shops 1001,1002 --shops 1003 --price-source minimum --price-multiplier 10 --stock 999 --all-products
```

示例生成计划单，经确认后用 `apply --plan` 提交。`--price-source` 默认 `snapshot`（源店价格），倍数默认1；`minimum` 必须匹配唯一的同中文名最低售价，不能回退到「实际售价」或整数价。金额按分四舍五入；库存默认999，显式0保留为0。包装尺寸与毛重优先映射表，其次价格表，再次公开商品详情。上架前准备整批详情与载荷，仅解析目标店莫斯科仓；任一目标仓缺失时阻止该商品组，提交后记录受理响应并结束。

备用官方上架与跨店内容更新使用 `listing publish-openapi`、`listing sync-cards`，详见 [Open API 商品写入](OPEN_API_LISTINGS.md)。原有 BCS 上架默认入口保留。上架支持自有源店与任意 WB 商品码，统一完成建卡、有限标识等待、图片/莫斯科库存提交；超时用 `ops publish-openapi --resume --shops ... --vc ...` 预览剩余阶段，再用新单号续办。内容同步按完整 VC 匹配本店商品码，单独使用其 `--apply`。

历史随机 VC 前缀使用 `ops normalize-vc --shops 1001 --name "示例商品"` 生成计划单。写入只改前缀不一致的商品，保留原尾部；店铺或回收站资料不完整先停止。详见 [VC 前缀规范化](OPEN_API_LISTINGS.md#历史-vc-前缀规范化)。

## 尺寸、下架和清理

```powershell
python run.py product dimensions --shops 1001 --name "示例商品"
python run.py product dimensions --shops 1001 --vc "BCS-TEST-123456789" --dims "10*20*30/0.5"
python run.py product check-dimensions --shops 1001 --type all
python run.py ops trash --shops 1001 --vc "BCS-TEST-123456789"
python run.py product banned --shops 1001 --limit 10
python run.py product clean --target draft --shops 1001 --limit 10
```

- 自定义尺寸沿用显式商品范围。省略 `--dims` 时使用每个完整 VC 对应价格表行的包装尺寸。
- `ops trash` 从本地快照读取商品和全部规格 ID，先清莫斯科仓库存，再移回收站；无需先单独运行 stock，也不全量查询远端商品或库存。无法确认唯一可用的莫斯科仓库时阻止该店；商品 ID 或任一规格 ID 缺失时阻止该商品，不只清部分规格后下架。
- 清库存失败或受理回执不完整时，受影响商品不会移回收站，其他正常商品继续；命令返回非零状态，日志记录被阻止原因。预览发现不完整计划也返回非零状态。收到清零受理回执后直接进入回收站提交，不进行写后查询验证。`product trash --channel bcs` 同样遵守清库存前提，是独立 BCS 通道，不作为 WB 失败后的自动兜底。
- `product banned` 可列出被阻止的商品。要下架时用清单中的完整 VC 生成 `ops trash` 计划并确认单号。
- `product clean` 以 --target 选择草稿箱、回收站或两者。预览和删除使用同一份受 --limit 约束的清单，提交须 --apply --yes。回收站需要归零时通过 BCS 提交本店莫斯科仓对应规格，归零已受理后才通过 WB 删除。结果依据按商品 ID 返回的受理回执。

这些操作不能仅凭“有 dry-run”就扩大范围或自动确认。

## 折扣、促销和推广审计

```powershell
python run.py ops discount --shops 1001 --name "示例商品" --target 50
python run.py ops discount --shops 1001 --threshold 55 --below 40 --target 50 --all-products
python run.py ops discount --channel bcs --shops 1001 --threshold 50 --target 50 --all-products
python run.py marketing enroll --shops 1001
python run.py product review-price --shops 1001
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

`order process --apply` 默认处理 `mabang.shop_map` 配置店铺的全部新增待处理订单，不限日期。顺序为库存 SKU 匹配、预报上传、成功状态轮询、交运；配置飞书地址时再登记飞书。`order match`、`order forecast` 及飞书登记合并的待处理单也使用相同日期语义：`--days 0`（默认）不发送起止日期限制；明确要处理最近 N 天时传正数，例如 `--days 7`。仍只处理配置店铺与新增待处理状态，不包含其他店铺或历史已完成订单。列表完整翻页，达到安全上限或重复分页时报错，不用部分结果继续执行。更换失败、缺 SKU、预报失败或未完成的订单会按流程阻断后续履约，依据本次日志处理，不删除状态后盲目重跑。

预报状态等待是交运前必要步骤，不能当成写后验证而跳过。`--check` 只是查看预报批次，`order forecast --apply` 才执行整条预报/交运链。

`order register --apply` 按订单编号去重增量登记，latest 模式合并最近订单和待处理订单。历史补录可明确指定：

```powershell
python run.py order register --scope all --date 2026-10-01
```

用户确认后再加 --apply。去重命令只清已有重复记录，库存全量登记则会清空并重建库存表；都不能当作每天的无害初始化。`order stock-days` 默认补今天和更新现有日期列，显式 --begin/--date 会进入可删除旧日期列的区间模式，详见 [数据契约](DATA.md)。

### WB 发货群供货与 PDF

`order group-ship` 单独查询 BCS 配置店铺的 WB FBS 新增订单。逐店按完整 VC 匹配商品价格表，在本店已配置的 WB 仓库中为「WB发货」商品组建供货。同店同名合为一个供货。已确认商品中文名但发货方式留空或无效的订单会列为阻止项。

马帮把 WB 发货订单记录为 `GROUP_SHIPPING`，跳过库存匹配更换、预报和交运。飞书仍按订单编号登记，「库存SKU」留空。空 SKU 在马帮库存日统计中计入无 SKU 订单。

```powershell
python run.py order group-ship
python run.py order group-ship --shops 9352 --name 黑丝假发
python run.py order group-ship --shops 9352 --name 黑丝假发 --apply
python run.py order group-ship --shops 9352 --resume --apply
```

默认预览，不创建供货。加 `--apply` 后建供货、加入全部订单、提交配送，再按本次供货号和订单 ID 取 WB 原码。加入每次最多 100 单，超过时分次加入同一个供货。订单的货物尺寸、跨境或 B2B 属性无法合并，或缺平台必填标识时，整组留待人工处理。

供货名如 `20261111-103500_袁州1(9352)_黑丝假发_2单`，使用新加坡时间。PDF 用较短的文件名，例如 `袁州1(9352)_黑丝假发_2单_1234567_供货码.pdf` 和 `袁州1(9352)_黑丝假发_2单_1234567_标签.pdf`，数字编号对应 WB 供货 `WB-GI-1234567`。按供货创建日建文件夹，例如 `data/fbs_shipments/20261111/`，同一天所有店铺的 PDF 集中保存，续办沿用原供货日期。供货二维码是 58 × 40 mm 的一页，订单标签是 40 × 30 mm 的每单一页。两份文件都通过检查并保存后才记录为导出完成。

配送已受理但 PDF 未导出时，用 `--resume --apply` 按原供货号和文件名补取；只加 `--resume` 查看记录。部分加入失败或写入结果不明时，按 `data/state/WB_FBS发货群供货.json` 中的批次回执人工处理。两份 PDF 是配送后的业务产物，结束后不附加回读验证或同步。

## 查询报表与客服

商品运营综合报告使用 BCS＋WB Open API＋飞书，只生成本地 Excel，不修改商品或飞书，不自动同步、拉取或合并快照：

```powershell
python run.py report products
python run.py report products --shops 1001,1002 --feedback-days 365 --out data/logs/product_report.xlsx
```

默认近30天退货、接口可取历史评价，按UTC+8日期计算。默认输出评分≤3.5且本店存在、高退货率≥30%且本店存在，以及排除已知风险且中文名在价格表的近7/14天出单清单；完整VC只显示一行，列存在/缺少/待确认店铺，不列商品链接。风险与缺口按VC最长数字段合并；加 `--details` 才追加原始证据表。目标店无快照或授权失败时标为未知，报告仍输出并返回非零；不要把本地缺口视为实时缺货。配置、工作表和完整口径见 [报告说明](PRODUCT_REPORT.md)。

```powershell
python run.py report vc-sales --days 7
python run.py report delivery --shops 1001 --days 7
python run.py report orders --shops 1001 --days 30
python run.py support appeals --shops 1001 --days-max 5
python run.py report ratings --shops 1001 --one-star
python run.py support questions --shops 1001 --no-detail
python run.py support chats --shops 1001 --limit 10 --no-detail
```

报告一般写入 data/logs，部分命令支持 --out。`report orders` 默认时区为 America/Los_Angeles，统计窗口按该时区解释，不要直接当成本机当天；导出还区分订单创建时间/更新时间的口径。

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

`support ai-test` 会联网调用 LLM 并写对照报告，不向买家发送；不要与 `scripts/check.py` 的离线测试混淆。

## 每日任务与 Windows 计划任务

```powershell
python run.py task run morning --all-shops
python run.py task run adjust-discounts --all-shops
python run.py task schedule
```

morning 执行促销报名和折扣调整，adjust-discounts 只执行折扣调整。`task run adjust-discounts --all-shops --apply` 会写平台。daily 预览只打印步骤，用户授权后可以使用：

```powershell
python run.py task run morning --shops 1001 --apply
python run.py task schedule --all-shops --apply
python run.py task schedule --shops 1001,1002 --apply
python run.py task schedule --remove --apply --yes
```

schedule 注册 SellerOps_Morning（09:00）及 SellerOps_Check11/15/19（11:00、15:00、19:00），按运行电脑本地时间执行。自动任务包含 `--apply`，支持 `--shops` 固定指定店铺，也可用 `--all-shops` 覆盖相应步骤的全部可用店铺。每日折扣沿用 WB，显式传入策略目标 `--target 50`。

注册时固定当前解释器、代码路径和数据路径；移动目录或换解释器后需重新注册，并清理不再使用的任务以免重复执行。daily 每步最多等待 1 小时，某步非零退出会停止后续步骤；日志在 data/logs/daily_日期.log。
