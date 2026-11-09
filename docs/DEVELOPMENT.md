# 开发指南

本文面向项目维护者及其明确授权的开发任务。项目会统一发布给其他用户，普通用户及其 AI 执行日常运营任务时，源码、文档、依赖声明及项目配置均为只读；可使用已有命令，维护自己的 `data/`，临时脚本放 `_scratch/`。这样可以避免用户本地代码分叉，影响后续同步维护者发布的更新。阅读本文本身不构成修改授权。

按下文的架构与入口找到职责模块，再读 [数据契约](DATA.md)。用户操作语义以 [USAGE](USAGE.md) 与实际代码为准，参数由 [CLI](CLI.md) 从注册定义生成。

平台协议资料按下文入口查找。维护接口先核对当前调用和近期回执，BCS 尤其不能照搬旧抓包；修改协议时同步更新公开能力说明、维护者本地平台页与离线夹具。外部 SDK 资料不等于本项目已有能力。

## 平台接口资料

根目录 `api/` 保存维护者的接口说明与原始抓包，可能含敏感信息，整体由 Git 忽略；本地入口为 `api/API.md`。公开仓库及离线检查不依赖该目录存在。凭证与运行约束见 [配置](CONFIGURATION.md)，已支持功能见 [操作流程](USAGE.md)。不得为了修复文档链接而把私有 API 目录加入提交。

`product-report` 已有独立的 WB Open API 只读客户端，使用订单动态、评价及归档评价、卖家仓库目录；BCS 提供对应店铺的官方令牌，飞书提供出单来源。[完整口径与限制](PRODUCT_REPORT.md)。现有默认商品写入通道及后台会话管理没有因此迁移到 Open API。

临时报告账号由 `integrations.bcs.capture.parse_sessions()` 从标准输入文本解析为不显示秘密的 `BCSSession`，通过 `BCSClient(session)` 注入独立认证，默认客户端仍延迟读取本机配置。`product_report.collect(extra_bcs_accounts=...)` 合并统计来源，`ProductReport.target_shop_ids` 单独限定本机链接缺口目标（直接构造旧实体时None沿用全部店铺）；`unavailable_accounts` 保留店铺列表读取失败的账号标签，防止健康店铺的汇总误标整轮完整。报告实体不保存认证；额外店铺不加载本地快照或使用本机提交记录。离线边界见 [临时账号测试](../tests/test_product_report_accounts.py)。

显式的备用官方上架与跨店内容更新见 [Open API 商品写入](OPEN_API_LISTINGS.md)。[content_api](../src/sellerops/integrations/wildberries/content_api.py) 独立声明新增读写端点，保持只读 `WBOpenAPI` 白名单不变；[open_api_payloads](../src/sellerops/features/listings/open_api_payloads.py) 负责完整覆盖更新与标识保留，[open_api_sources](../src/sellerops/features/listings/open_api_sources.py) 负责公开商品的类目属性映射，[open_api_publish](../src/sellerops/features/listings/open_api_publish.py) 编排完整上架，[open_api_finish](../src/sellerops/features/listings/open_api_finish.py) 负责有限标识轮询、后续提交与续办，[open_api_sync](../src/sellerops/features/listings/open_api_sync.py) 编排跨店更新。阶段状态在 [持久化模块](../src/sellerops/persistence/open_api_publish.py) 中按数据目录隔离，以 FileLock 覆盖整轮事务。写请求不重试，条码、建卡与媒体/库存分别记录，预览不生成条码或等待；离线边界覆盖见 [test_open_api_writes](../tests/test_open_api_writes.py) 与 [有限等待测试](../tests/test_open_api_publish_wait.py)。

`normalize-vc-openapi` 的编排在 [vc_normalization](../src/sellerops/features/listings/vc_normalization.py)，[纯规则](../src/sellerops/domain/vc_normalization.py) 处理明确归属和跨店一致的避重分配，只替换四位前缀并保留原尾部，冲突时追加正整数序号，[专用载荷](../src/sellerops/features/listings/vc_normalization_payloads.py) 保留现值并按实际 ASCII JSON 字节分批。[只读输入与历史](../src/sellerops/persistence/vc_normalization.py) 按表头加载所需字段，拒绝歧义、吞错和随机前缀兜底；日志目录的 FileLock 覆盖全轮，提交前保存 submitting，历史记录固定新码并阻止同店同卡重放。读取不完整整轮零提交，写后不查询；不建立别名或修改现有归属/快照/状态。边界见 [离线改码测试](../tests/test_vc_normalization.py)。

## 当前必须保留的运行约定

报告默认筛选由 [product_report_screening](../src/sellerops/features/catalog/product_report_screening.py) 纯计算：最长数字段合并风险，完整VC去重输出，当前本店快照判断缺口并区分未知/回收站/已有提交。名称输入由 [report_catalog](../src/sellerops/persistence/report_catalog.py) 按表头仅读取价格表商品名、当前映射VC及中文名，不依赖价格公式、图片或库存。`--details` 才展开原证据；[离线筛选测试](../tests/test_product_report_screening.py) 覆盖边界、加权、跨店标识与缺口。

- 所有仓库操作默认且仅使用该店铺的莫斯科仓库 ID，包括读取、修改库存、清零、下架前清库存、回收站归零、上架和临时脚本。全店/全部商品不扩大仓库范围；不能回退到成都或其他俄语地区仓。名称和 ID 无法确认时中止该店操作。
- WB 仓库列表的 `id` 才是库存使用的仓库标识，同一响应中的 `warehouseId` 是 office 标识。每店独立解析和核对；马帮仓库 ID 属于另一套体系，也须核实为莫斯科业务仓。
- 当前 WB 凭证由用户人工登录、手动抓包后填写或导入；Playwright 尚未启动、自动登录/浏览器续期流程未启用。不得把可选依赖、浏览器模块、401 刷新钩子或 CLI 定义的存在写成“当前已启用自动续期”。保留 [受限浏览器手动抓包步骤](CONFIGURATION.md#手动抓包更新凭证当前方式)。
- 这些是现行操作约定，不能因重组代码、压缩文档或删除历史记录而丢失。将来变更运行方案时，必须同步修改 AGENTS、配置、操作指南及生成参考，不能只改某一处说明。

### 已有实现与约束的差异

[仓库解析器](../src/sellerops/features/listings/warehouse.py) 只接受唯一、可用且名称匹配莫斯科的仓库，不回退其他地区仓。[库存与下架计划](../src/sellerops/features/listings/plans.py) 优先使用本地快照，CLI通过 [定向字段解析器](../src/sellerops/features/listings/product_data.py) 在提交前补齐已有商品缺失的必要标识；仍缺少已确认仓库或完整规格的商品写入 `blocked_items`，预览和执行均返回非零状态，同店其他完整商品可以继续。[下架前清库存](../src/sellerops/features/listings/trash_execution.py) 按商品核对全部规格的受理回执，任一规格失败、缺失、重复或回执不一致时，阻止该商品移回收站；失败商品按商品卡计数，不按失败规格重复累加。

底层显式实时库存读取不传 store_id 时仍可读取多仓；`stock --resolve live` 已限定确认的莫斯科仓，仅补充当前库存，写入规格仍来自完整计划，不能因库存缺行减少目标。`warehouse-clear` 专门操作成都仓，当前不使用。调用方仍须核对仓库范围，不能把底层函数的存在当作其他仓库的操作授权。默认本地快照流程不调用全量远端商品或库存查询。

[WB 客户端](../src/sellerops/integrations/wildberries/client.py) 也保留 401 浏览器刷新钩子，当前不依赖或启用该机制。运营任务出现认证失败走人工抓包更新；未来若另行授权启用浏览器方案，应先处理部署和运行约定的一致性。

## 项目入口

本项目是 src 布局的 Python 包，源码在 `src/sellerops`。源码运行入口为 [run.py](../run.py)，包入口为 [__main__.py](../src/sellerops/__main__.py)。只有入口和开发脚本负责把 src 放入导入路径，业务模块不自行修改 sys.path。

## 调用链与依赖

```text
run.py / python -m sellerops
  → cli.main：选定 data 目录、构建参数、解析、处理退出码
  → cli.<组>.register：参数与处理器在一处 bind
  → cli.binding.dispatch：按需 import 具体处理器
  → features：业务计划、编排、结果输出
      → integrations：平台协议、会话、HTTP / CLI / 浏览器
      → persistence：本地价格表、映射、快照与索引
      → domain / core / settings
```

integrations 不依赖 features、cli、persistence；persistence 不依赖 features、cli、integrations；domain 为纯模型和编码规则。边界由 [离线检查](../scripts/check.py) 检查。跨功能可直接复用明确的公开函数，不要求再包一层单例门面。

`cli.main` 在注册带文件路径默认值的参数之前选定数据目录。`--help` 不导入业务处理器、不读取凭证或联网。凭证访问器在首次 get 时加载，更新会话后可 reload；直接库调用应在导入会缓存路径的业务模块之前先配置数据根目录。

## 去哪里修改功能

| 领域 | 入口与职责 |
| --- | --- |
| CLI | [分发](../src/sellerops/cli/binding.py)、[各命令组](../src/sellerops/cli/main.py)；完整处理器位置见 [CLI 表格](CLI.md) |
| catalog | [mapping](../src/sellerops/features/catalog/mapping.py) 归属/工作台，[mapping_sync](../src/sellerops/features/catalog/mapping_sync.py) 审核合并，[single_shop](../src/sellerops/features/catalog/single_shop.py) 单店表 |
| listings | [plans](../src/sellerops/features/listings/plans.py) 计划，[execution](../src/sellerops/features/listings/execution.py) 执行，[prices](../src/sellerops/features/listings/prices.py)/[stocks](../src/sellerops/features/listings/stocks.py) WB 用例，[bulk_publish](../src/sellerops/features/listings/bulk_publish.py)/[card_publish](../src/sellerops/features/listings/card_publish.py) 两种发布协议 |
| pricing | [discounts](../src/sellerops/features/pricing/discounts.py) 折扣，[promo_gap](../src/sellerops/features/pricing/promo_gap.py) 数据采集编排，[promotion_rules](../src/sellerops/features/pricing/promotion_rules.py) 纯计算，[promotion_report](../src/sellerops/features/pricing/promotion_report.py) 输出 |
| orders | [matching](../src/sellerops/features/orders/matching.py) SKU 匹配，[forecast_selection](../src/sellerops/features/orders/forecast_selection.py) 候选，[forecast_polling](../src/sellerops/features/orders/forecast_polling.py) 轮询，[forecast](../src/sellerops/features/orders/forecast.py) 预报交运，[mabang_process](../src/sellerops/features/orders/mabang_process.py) 全流程 |
| 飞书用例 | [feishu_register](../src/sellerops/features/orders/feishu_register.py) 登记，[mabang_stock](../src/sellerops/features/orders/mabang_stock.py) 重建库存表，[mabang_stock_daily](../src/sellerops/features/orders/mabang_stock_daily.py) 日期列与总量；通信在 [tables](../src/sellerops/integrations/feishu/tables.py) |
| support | [questions](../src/sellerops/features/support/questions.py) 手动回复，[questions_watch](../src/sellerops/features/support/questions_watch.py)/[chat_watch](../src/sellerops/features/support/chat_watch.py) 监听，[product_context](../src/sellerops/features/support/product_context.py) 商品上下文 |
| 自动化 | [daily](../src/sellerops/automation/daily.py) 流程与子进程，[schedule](../src/sellerops/automation/schedule.py) Windows 任务 XML 与注册 |

## 常用复用能力

| 需求 | 现有 API | 副作用 |
| --- | --- | --- |
| 价格表与映射 | `MappingRepository.load_price_book()`、`load_mapping_state()`、`build_vc_resolver()` | 读取本地表格/状态 |
| 店铺快照 | `ProductSnapshotRepository.shop_ids_from_disk()`、`load_shop_rows(shop_id)` | 本地读取，索引缓存区分文件路径和版本 |
| 编码前缀/源商品码 | `domain.vendor_codes.extract_vc_prefix()`、`core.utils.extract_wb_nm()` | 纯解析 |
| 价格公式与表头 | `persistence.price_book`、`persistence.headers` | 读取/解析，按表头定位 |
| 目标仓库 | `features.listings.warehouse.resolve_warehouse(shop_id)` | 读取 WB，成功结果有进程内缓存 |
| WB 两阶段写入 | `integrations.wildberries.client.WBClient` | 远端操作，不在导入时调用 |
| 飞书完整分页 | `integrations.feishu.tables.list_records()` | 调用 lark-cli，不能以单页代替去重全表 |
| 原子状态写入 | `core.files.atomic_dump_json()`、`FileLock` | 本地写入；读改写需持锁覆盖整个事务 |

正常商品编码、协议字段和用户列名是数据契约。不能随意改掉平台字段、马帮店铺键或工作表结构，详见 [DATA](DATA.md)。

## 三条主要数据流

**商品主数据**：用户价格表 → 本地快照/归属池 → mapping/review 人工判断 → merge/纠偏更新本地映射。远端快照只在独立 fetch 中更新。

`fetch` 默认走 BCS 快照：`features.catalog.products.fetch_all` 先通过 `sync_shops_parallel` 同步 WB → BCS，再逐店 `bcs.fetch_shop_products(filter=BASE)` 读取缓存并原子保存，`--no-sync` 跳过同步直接读取上次结果。显式 `--source wb-openapi` 时改用 BCS `fetch_open_api_shops()` 获取对应店铺 apiKey，再由 `integrations.wildberries.open_api.WBOpenAPI` 读取官方中国区商品卡、规格价格和单仓库存；`features.catalog.snapshot_open_api` 负责唯一莫斯科仓确认及旧快照字段转换，`products` 负责原子保存、统计和多店失败汇总。令牌仅在内存中使用，不写入快照或状态。新增接口继续沿用只读端点白名单，禁止将库存 POST 读取改成 PUT/DELETE，也不在失败后自动换区域、Cookie 或缓存。

显式 Open API 来源的全店采集使用有界线程池，默认4店并发，`--parallel` 可调整（1为串行）。BCS店铺密钥只在主线程读取一次，任务内各自创建并关闭 `WBOpenAPI` 会话，不共享限流计时或仓库 ID；店内分页、批次和重试规则不变。主线程按完成结果原子保存各店快照、输出进度并汇总状态，状态列表仍按原店铺顺序保存。单店失败及本地保存失败不取消其他店铺，最终返回非零；不把并发扩展到默认 BCS 流程。并发边界、跨店隔离及失败保护使用屏障同步的离线替身验证，不依赖真实接口或耗时对比。

旧 BCS 行的缺失语义统一使用 `domain.snapshot_values`：完整规格验证、有效非负整数库存、首规格代表价和指定仓库存读取均为纯计算。不要在报表中用0代替缺失，不从别店补本店库存；库存/下架计划遇到规格标识缺失或重复时阻止该商品。BCS 列表的缺失数组和不一致计数属于协议错误，不能规范化为空店。

BCS 重复商品由 [domain.bcs_products](../src/sellerops/domain/bcs_products.py) 处理：协议客户端先核对原始响应数量和店铺，再按本店 nmId/完整 VC 去重，快照只保存可确认的完整原行。业务值冲突或没有完整原行时失败，不按更新时间覆盖业务冲突、不拼接字段；缺失规格语义仍交给 snapshot_values。单店表生成复用同一纯函数处理旧快照，其他消费者继续读取原路径，无需增加一套业务读取层。[离线去重测试](../tests/test_bcs_product_duplicates.py) 覆盖响应计数、保存失败保护、映射合并及直接 BCS 折扣读取。

**商品写入**：读取现有本地数据和必要的执行前平台数据 → 筛选/构建计划 → 默认预览或显式提交 → 记录接口结果/任务号/日志 → 结束。不得在尾部插入查询验证、fetch 或 merge。

`ProductDataResolver` 只在必要字段不足时惰性读取BCS密钥，使用本店独立 `WBOpenAPI(region='cn')` 会话：完整VC定向商品卡查询最多20页，精确匹配唯一非回收站卡，核对已知nmId，完整验证所需规格；价格/折扣仅在对应动作需要时按本店nmId读取。库存动作不依赖旧库存，尺寸动作不依赖旧尺寸，WB只改折扣不传价格。补齐结果及密钥仅驻留一轮内存；认证、限流及网络失败按店停止补齐，其他完整项仍可继续。计划函数保留可选resolver供CLI注入，直接库调用默认只使用本地数据。

`load_state` 为本次操作补充快照中可确认中文归属的新VC，尊重既有纠偏与排除，不持久化归属变更。快照索引拒绝错店元信息和重复VC，错误店记录为阻止项；不将损坏文件视为空店。改价、尺寸的必要字段失败与库存/下架一样保留结构化 `blocked_items`，计入非零退出；[跳过报告](../src/sellerops/features/listings/skip_report.py) 将全部跳过明细写入CSV，不能只保留终端前30条。补齐及提交前后边界见 [离线测试](../tests/test_product_operation_data.py)。

`publish-openapi` 在建卡受理后允许插入有限的待办 VC 标识轮询，取得本店 nmID/chrtID 并核对本次生成条码后才提交图片和莫斯科库存。默认总等待1800秒、轮次间隔15秒，按完整 VC 使用 `cards(vendor_code=...)` 搜索并在本地精确匹配；分页、时间、轮数均有边界。超时返回非零并保留受理状态，可用同命令 `--resume` 续办；所有标识读取结束后才提交就绪项的媒体/库存，此后不回读。其他写命令不增加此例外。

**履约**：本地 VC→中文名→库存 SKU → 马帮匹配 → 预报与上传 → 有限轮询成功/失败/历史状态 → 成功部分交运 → 配置飞书时登记。失败与未完成的批次不绕过等待直接交运。

## 测试和变更范围

[tests](../tests/test_core.py) 按基础设施、平台协议、商品、订单、报表、客服和运行策略分组。生产数据不作为默认测试夹具；替身隔离网络和外部命令。文档语法和链接由 [check_docs](../scripts/check_docs.py) 校验，参数参考由 [generate_cli_docs](../scripts/generate_cli_docs.py) 生成。


## 结构与依赖

- `src/sellerops/cli/`：参数定义、命令绑定、退出码。每个命令的参数与处理器放在同一注册函数里；帮助信息不得读取凭证、联网或写文件。
- `features/`：catalog 商品映射、listings 上架与库存、pricing 折扣促销、orders 履约、support 客服。按实际职责拆分，禁止仅为保留历史名字增加转发类、单例或模块。
- `integrations/`：Wildberries、BCS、马帮、飞书 CLI、AI、浏览器会话的通信实现。可依赖 settings、core、domain；不得依赖 features、cli、persistence。
- `persistence/`：本地表格、快照、索引读写；不得依赖 features、cli、integrations。
- `domain/`：纯模型与规则；`core/`：通用 I/O、异常和基础工具；`settings/`：路径与凭证。底层不得导入业务层。
- 跨功能复用可以直接调用有明确名称的公开函数。不要为了「同层隔离」强建无业务意义的门面。协议客户端不负责商品中文名等业务补全。

## 实现要求

- Python 3.10+，UTF-8，4 空格缩进。新增公共 API 标注参数和返回值类型；短函数的文档只描述必要契约，不强制模板。
- 模块超过约 400 行时检查职责，优先按协议、纯计算、编排、报表拆分；不按行数机械切片。
- dict 可以用于原始 API JSON、表格行和简单局部结果；稳定的业务计划和跨步骤实体优先 dataclass。避免为了类型形式复制协议字段。
- 不在 import 时读凭证、创建目录、启动浏览器、运行子进程或联网。外部工具和可选依赖使用时再加载。
- 不吞掉影响结果的异常；预期的降级须记录原因。命令失败返回非零；异步提交成功与最终生效须区分。
- 批处理保留成功、失败和店铺级异常的统计；不能把读取失败当作空清单，不能因另一目标处理成功而覆盖此前失败。平台返回成功 ID 清单时核对数量与唯一性，缺失或不完整不能补算为全部成功。
- 组合履约允许明确排除失败订单并处理其他订单，但整轮最终仍报告失败；预报失败/超时返回非零，无法定位所属店铺的失败批次不得放行交运。持续客服监听可逐轮恢复，单轮 --once 必须把读取/生成/发送错误传到退出码。
- 状态覆盖使用同目录临时文件、fsync 与 os.replace。读改写事务需在同一锁内完成；不能用「锁文件太旧」推断进程已死亡。
- CSV 使用 utf-8-sig；报表和状态文件分开管理。数据与凭证不提交版本库，错误日志不输出完整 Cookie、Token 或请求头。

## 写操作与同步

- 远端业务写入、定时任务注册默认预览，显式 `--apply` 才执行；不可逆删除要求 `--yes`。本地报表生成可直接执行。
- WB 修改异步生效，无法通过即时查询判断最终结果；BCS 修改也异步，且需先同步再查询，耗时较长。因此不进行写后验证：写命令提交并记录接口结果后结束，不附加查询验证、同步、拉取或映射表合并。写命令不提供 `--verify`、`--no-verify` 或串联写后同步的 `--sync` 开关。
- 更新快照和映射表使用独立 `fetch` / `merge` 命令，只有用户另行明确要求时才运行，AI 不得顺手补跑。离线开发测试可以正常执行，与真实平台写后验证无关。
- WB 提交过程必需的两阶段确认、明确要求的价格审核，以及马帮履约必需的预报状态轮询仍属于业务步骤；保留其超时、分页和限流边界。不得把业务确认误删为写后验证。
- WB Open API 上架的有限新标识轮询也是必要业务步骤，仅适用于已受理且无未知写入状态的待办完整 VC；不得用它刷新快照、查询最终图片/库存结果，或自动重放建卡和未知提交。断点续办保留原图片/库存计划，不把新的来源、价格或库存参数混入 `--resume`。
- 不把 HTTP 成功视为最终生效；保留平台任务 ID、失败项目和部分失败信息。写请求是否重试由幂等性决定，不能盲目重放。
- 开发测试不使用真实店铺执行写操作。上线验证是独立操作，不能混进默认测试。

## 验证与扩展

1. 在对应 features 模块实现用例；协议和本地持久化分别放入 integrations / persistence。
2. 在 cli 对应组注册参数和处理器，避免另建一份命令映射。
3. 增加离线行为测试，网络与子进程使用替身。测试关注边界、计算结果、数据完整性与写入开关。
4. 运行 `python scripts/check.py`：语法、导入、架构约束、全部命令解析和离线行为测试。完整离线验证是正常开发流程。
5. 更新相关使用说明，运行 `python scripts/generate_cli_docs.py` 和 `python scripts/check_docs.py`。不要复制带凭证的抓包、运行日志、作者环境路径和旧自动化记忆。

## 新增命令与复用

先实现 features 中的用例函数，底层通信和本地存储分别放在 integrations / persistence。用例需要解析复杂参数时，在 CLI 组中定义，不在业务层重新维护一个完整 ArgumentParser。

在对应 `cli/<组>.py` 的 register 中，创建 parser、声明参数并 bind 到真实处理器。以下是授权开发时新增功能的代码示意，不是现有命令：

```python
from sellerops.cli.binding import bind

def register(sub):
    parser = sub.add_parser("inventory-summary", help="汇总本地库存")
    parser.add_argument("--shops", default="")
    bind(parser, "sellerops.features.catalog.inventory_summary", "run")
```

复用已有仓储与模型，不另读一套固定列号的 Excel。稳定数据对象放 domain；原始接口 JSON 不必全转 dataclass。需要数据路径时，在导入会缓存路径的业务模块前调用 settings.paths.configure；不要通过改变当前工作目录定位数据。

## 测试、文档与发布

- 业务或基础设施改动运行 `python scripts/check.py`。它检查语法、未解析名称、依赖边界、内部导入、全部命令帮助与处理器，并运行离线行为测试。替身应覆盖外部接口和子进程，不使用生产 data。
- 仅文档改动运行 `python scripts/check_docs.py`；若修改 help/参数文案，先重新生成 CLI 参考。该脚本只解析示例，不提交示例写入。
- 文档分别维护用户流程、配置与格式、AI 边界和开发入口，避免把未经实现的计划写成已支持功能。新增参数时同时核对示例、默认值与输出位置。
- 新增不可逆命令应显式确认并限制范围；现有命令的确认差异要如实说明，不能只在总则中声称所有删除都需要 --yes。
- 发布前检查 git status、忽略规则和暂存文件，确认 data、会话、抓包、日志、测试副本不在提交中。不要自动执行 git add、提交、推送或注册任务作为测试的一部分。
- 更新应保留用户自己的 data。协议或状态格式发生变化时，提供数据升级步骤和回归夹具，避免隐式覆盖映射、去重或提交记录。
- 真实接口测试独立于默认离线检查，限定用户授权的店铺与操作范围。读取测试不提交商品/订单/客服写入，也不触发同步；测试脚本及脱敏结果放 `_scratch/`，不把个人环境和测试流水写入使用文档。
