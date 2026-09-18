# wb\_ops · 架构文档

> 面向：开发人员 / 接手维护者 / AI。想快速上手先读 [README.md](README.md)；查命令看 [CLI.md](CLI.md)。
> ⚠ 目录结构图里的根目录、店铺 ID、Python 路径均为作者环境示例，脚本用相对路径（见文末「运行环境」），换电脑/账号无需改代码。

## 一、目录结构

```
<仓库根目录>/   （如 D:\E\脚本\bcs_api\wb，可放到任意位置）
├── wb.py                    ★ 统一入口（薄启动器 → wb_ops.cli.main）
├── wb_ops/                  ★ 核心库（Python 包）
│   ├── __init__.py            版本号 + 公共导出
│   ├── cli.py                 ★ 统一 CLI 调度器（42 个子命令动态延迟分发）
│   ├── config.py              非敏感配置：路径常量（BASE_DIR→data/）、默认阈值、VC_PREFIX_RE
│   ├── credentials.py         ★ 统一凭证加载中枢（读 data/credentials.json）
│   ├── common.py              共享工具：UA / CookieExpiredError / jwt_payload / to_int / stdout UTF-8 / print_write_hint
│   ├── daily.py               每日任务启动器（morning/check）
│   ├── schedule.py            Windows 计划任务管理
│   ├── framework/             ★ 架构基础设施层
│   │   ├── safe_io.py         原子写入（atomic_dump_json, atomic_write_text）与跨进程互斥锁（FileLock）
│   │   ├── cli_args.py        CLI 参数定义公共件（ops 参数唯一实现，argparse-only）
│   │   ├── exceptions.py      统一异常分层（AuthenticationError, RateLimitError 等）
│   │   └── registry.py        命令与服务动态注册中心
│   ├── domain/                ★ 领域模型层
│   │   └── models.py          强类型业务实体（ProductCard, Shop, DiscountPlan, TaskResult, OrderStatus 等）
│   ├── storage/               ★ 仓储持久化与数据防腐层
│   │   ├── product_repo.py    ProductRepository：店铺快照存储与在架商品快速索引
│   │   └── mapping_repo.py    MappingRepository：映射总表、已知归属池与解耦品名反查解析器（build_vc_resolver）
│   ├── adapters/              ★ 外部系统通信适配层
│   │   ├── wb_client.py       WB 卖家后台 API 客户端（会话维护、原生改价改折扣与 fetch_canceled_ids 归位）
│   │   ├── bcs_client.py      BCS 云端 API 客户端（Bearer + X-Limit-Key 与退避重试）
│   │   ├── mabang_client.py   马帮 ERP API 客户端（三域会话、订单替换、预报交运）
│   │   ├── callcenter_client.py WB 客服沟通投诉单（callcenter）只读适配器（列表游标翻页 + 详情）
│   │   ├── llm_client.py      大模型应答客户端（OpenAI/通义兼容接口）
│   │   ├── task_runner.py     通用异步任务轮询引擎
│   │   └── cookies.py         从抓包 md 刷新凭证
│   └── services/              ★ 业务用例服务层（高内聚 5 大业务域 + 外观服务门面）
│       ├── catalog_svc.py     商品目录、映射与核对工作台门面服务
│       ├── discount_svc.py    折扣管理与批量改价门面服务
│       ├── order_svc.py       订单履约、马帮对接与飞书登记门面服务
│       ├── replicate_svc.py   商品搬家、跨店复制、库存与清理门面服务
│       ├── support_svc.py     客服与智能应答门面服务
│       ├── catalog/           商品目录、多店映射与工作台业务实现
│       │   ├── mapping_excel.py 8-Sheet 聚合全景总表 Excel 生成器（独立拆分）
│       │   ├── mapping.py     映射总表增量合并核心
│       │   ├── mapping_sync.py 单店映射表生成与同步
│       │   ├── mapping_check.py 映射核对检查
│       │   ├── mismatch_check.py 货不对板筛查
│       │   ├── products.py    快照拉取与店铺商品管理
│       │   ├── workbench.py   工作台 HTML 渲染器
│       │   └── keywords.py    关键词提取与分析
│       ├── discount/          折扣调整与促销活动业务实现
│       │   ├── promo.py       促销活动报名
│       │   ├── price_review.py 价格审查隔离区释放
│       │   └── discount_bcs.py BCS 模式折扣修改
│       ├── order/             订单履约业务实现
│       │   ├── mabang.py      马帮待处理订单匹配更换
│       │   ├── mabang_process.py 马帮全链路自动化一体
│       │   ├── mabang_stock.py 马帮库存表登记与管理
│       │   ├── feishu_register.py 飞书订单去重登记
│       │   ├── orders.py      WB 订单查询与同步
│       ├── replicate/         搬家上架、库存改价与清理业务实现
│       │   ├── ops.py         一键操作门面编排
│       │   ├── ops_plan.py    改价/库存/下架计划构建器（无副作用独立拆分）
│       │   ├── ops_executor.py 计划分批执行与日志追加记录（独立拆分）
│       │   ├── wb_card.py     WB 原生商品卡片与尺寸解析器（独立拆分）
│       │   ├── replicate.py   跨店复制上架
│       │   ├── import_shelve.py 他人映射表导入上架
│       │   ├── shelve_new.py  新版批量上架（POST /products/batch/push）
│       │   ├── shelve_old.py  旧版上品建卡（POST /system/wbCollection/wb/new）
│       │   ├── shelve_common.py 上架公共解析/查重/降级提取器
│       │   ├── dimension.py   批量尺寸毛重修改
│       │   ├── dims_check.py  偏差商品排查
│       │   ├── clean.py       草稿箱/回收站清理
│       │   ├── banned.py      被阻止商品移回收站
│       │   └── remote_wh.py   远端成都仓库处理
│       └── support/           客服提问监控与自动回复业务实现
│           ├── questions.py   买家提问抓取与人工回复
│           ├── questions_watch.py 后台 AI 智能问答常驻轮询
│           ├── ai_reply_test.py AI 回复效果测试
│           └── complaints.py  WB 平台投诉单查询（未处理/剩余天数 → 去重商品编号）
├── tests/                    ★ 自动化测试套件
│   ├── test_all_commands.py   覆盖全部 42 个 CLI 命令 / 43 个用例的集成测试（100% PASS，全量约 5 分钟）
│   └── run_tests.py           按需测试选择器（--changed / --cmd / --help-smoke，日常只跑改动相关）
├── data/                     ★ 统一数据目录（本地专属，不进 git）
│   ├── credentials.json       ★ 统一凭证（勿泄露 / 勿提交 git）
│   ├── 商品价格表.xlsx         唯一权威商品清单（用户维护）
│   ├── 价格映射表.xlsx         聚合全景总表（8-Sheet，merge 自动重建）
│   ├── shops/                ★ 单店映射表（shop_{id}_{name}.xlsx，各店独立资产）
│   │   └── _archive/         ★ 停用/归档店铺目录
│   ├── products/             shop{id}_products_all.json 快照
│   ├── state/                同步状态 / vc_known.json / vc_override.json 全局归属池
│   ├── har/                  抓包 md/har（cookies-update 输入）
│   ├── workbench/            生成的工作台 *.html
│   └── logs/                 运行日志 + 结果 CSV
├── docs/                     ★ 文档系统
│   ├── README.md             文档主索引与快速上手
│   ├── ARCHITECTURE.md       架构设计、分层与业务规则
│   ├── DEVELOPMENT_GUIDE.md  ★ 开发要求与代码格式规范（必读）
│   ├── REUSE_GUIDE.md        ★ 开发复用指南：依赖/函数速查 + 代码模板 + 按需测试速查
│   ├── CLI.md                子命令全集与调用参考
│   ├── USAGE.md              日常情景操作指南
│   └── CREDENTIALS.md        鉴权、会话与凭证配置
├── api/                      BCS API 抓包/内部文档（含真实 token，本地参考，不随公开仓库分发）
├── _archive/                 废弃脚本 + 一次性数据 + 旧文档（可回滚）
└── _scratch/                 ★ AI 临时工作区（写脚本/处理文件/中间产物；已 gitignore，不进公开仓库）
```

## 二、模块分层与依赖方向

项目已完成分层架构升级（Lightweight Clean Architecture），消除中心辐射耦合、大文件单体及无锁并发风险：

```
表现与调度层 (Presentation)
  └── cli.py（动态按需延迟加载路由，42 个命令启动零业务依赖，防雪崩）/ daily.py / schedule.py
        │ 动态调度 (Command DTO)
        ▼
业务用例服务层 (Services)
  ├── 领域外观门面: catalog_svc / discount_svc / order_svc / replicate_svc / support_svc
  └── 领域业务实现: services/{catalog, discount, order, replicate, support}/*.py
        │ 编排调用
        ▼
领域模型层 (Domain)
  └── domain/models.py（ProductCard, Shop, DiscountPlan, TaskResult, OrderStatus 等强类型实体与枚举）
        ▲ 转换实体
        │
仓储持久化与数据防腐层 (Storage)
  ├── storage/mapping_repo.py（MappingRepository: 映射总表、全局归属池、单店表与解耦的 build_vc_resolver 品名反查器）
  └── storage/product_repo.py（ProductRepository: 快照持久化、在架商品多维快速索引、缓存失效）
        │ 数据交互
        ▼
基础设施与适配层 (Adapters & Framework)
  ├── adapters/（wb_client 封装原生接口与归位后的 fetch_canceled_ids; bcs_client; mabang_client; llm_client; cookies）
  ├── framework/（safe_io 原子存储与 FileLock、cli_args 参数定义、exceptions 统一分层异常、registry 动态注册中枢）
  └── credentials.py（统一凭证加载器，管理 data/credentials.json 安全读取与解析）
```

### 1. 核心设计原则与解耦成果

1. **严格依赖单向流动**：
   - 表现层 → 服务层 → 仓储与适配器层 → 领域实体与基础设施层。
   - 严禁任何反向依赖（Domain/Framework 不得感知上层业务）。
   - 详细开发约束与编码格式规范请参考专篇文档：**[DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)**。
2. **跨域品名反查彻底解耦（Catalog.Mapping 沉降）**：
   - 彻底消除了原 `replicate` 与 `support` 对 `catalog.mapping` 私有实现函数的直接引用。
   - 所有品名反查逻辑统一收口在 `MappingRepository.build_vc_resolver()`，通过仓储层对外提供轻量、统一、只读的反查闭包函数。
   - 安全写入提示 `print_write_hint()` 已统一下沉至 `wb_ops.common`，跨模块零侵入共享。
3. **平台原生 API 放置偏差解耦**：
   - 原放置在 `services/replicate/` 的 WB 原生取消订单查询接口 `fetch_canceled_ids`，已正确归位至外部适配层 `wb_ops/adapters/wb_client.py`，`order` 域直接调用适配器，消除不合理的跨业务域引用。
4. **单体大文件拆分（Anti-Monolith Pattern）**：
   - **一键操作（ops）拆分**：将原庞大的单体模块重构为计划构建器 `services/replicate/ops_plan.py`（纯函数构造调价/库存/下架 Plan，无副作用）、批量执行引擎 `services/replicate/ops_executor.py`（负责分批 API 交互与 CSV 审计追加）以及薄门面 `ops.py`。
   - **映射全景总表拆分**：将近千行的 8-Sheet 复杂 Excel 格式渲染剥离至 `services/catalog/mapping_excel.py`，让 `mapping.py` 专注增量合并算法。
   - **马帮底层通信抽离**：将三域名 Cookie 会话维持、底层请求封装抽取为 `adapters/mabang_client.py`，业务脚本 `mabang.py` 仅关注订单状态转换。
   - **商品卡片解析抽离**：将复杂的 WB 原生商品解析与包装尺寸提取抽象为 `services/replicate/wb_card.py`。
5. **数据原子安全与无锁化保护**：
   - 关键快照、凭证写入一律使用临时文件 + `os.replace` 原子覆写（`safe_io.atomic_dump_json`），并在全局池修改时配合 `FileLock` 跨进程互斥锁，从根本上杜绝断电或并发写造成的文件损坏。



## 三、两套鉴权体系

| <br /> | BCS 云端 API                                               | WB 卖家后台                                                   |
| ------ | -------------------------------------------------------- | --------------------------------------------------------- |
| 域名     | wb.bcserp.com/prod-api                                   | seller.wildberries.ru / discounts-prices / seller-content |
| 凭证     | Bearer JWT + X-Limit-Key + Cookie(Admin-Token/Limit-Key) | authorizev3 + wb-seller-lk + Cookie（cfidsw-wb 等）          |
| 模块     | adapters/bcs_client.py                                   | adapters/wb_client.py                                     |
| 失效表现   | 401（token 过期）/ 405（缺 Limit-Key）                          | 403（cfidsw-wb 过期）                                         |
| 存储     | credentials.json `bcs` 段                                 | credentials.json `wb` 段                                   |
| 失效表现   | 401（token 过期）/ 405（缺 Limit-Key）                          | 403（cfidsw-wb 过期）                                         |
| 存储     | credentials.json `bcs` 段                                 | credentials.json `wb` 段                                   |

详见 [CREDENTIALS.md](CREDENTIALS.md)。

## 四、ID 体系（关键）

| ID                | 格式/来源                       | 作用域   | 用途                                |
| ----------------- | --------------------------- | ----- | --------------------------------- |
| `vendorCode`      | `BCS-{4位前缀码}-{WB原始nmId}` 或 `BCS-{4位前缀码}-{中间标识}/{WB原始nmId}` | 跨店唯一键 | 单店表与总表主键、review/merge 比对、ops 定位（兼容 ozon-card 与 `/` 格式） |
| `nmId`（BCS 内部）    | 店铺 JSON 顶层字段                | 每店不同  | 改价（price/batch）、下架（removeToTrash） |
| `chrtId`（规格）      | 店铺 JSON `sizeList[].chrtId` | 每店每规格 | 改库存（stock/batchSetByChrtIdsBatch） |
| `warehouseId`（仓库） | `stockList[].warehouseId`   | 每店每仓  | 改库存的分组键                           |

> 单店映射表（`data/shops/shop_{id}_{name}.xlsx`）记录单店的 vendorCode、nmId、真实价格与库存；聚合全景表（`data/价格映射表.xlsx`）以 vendorCode 为跨店主键汇聚各店数据。ops 操作前需 `fetch` 结合快照中的 `nmId/chrtId/warehouseId` 执行底层操作。

## 五、核心业务规则（不可改变）

1. **半价口径**：商品价格表「双倍售价」= 最低售价 ×2（D 列公式）；店铺整数价 = `floor(双倍售价)`。
2. **折扣规则**：所有 `discount > 50%` → 改为 `50%`（`wb.py discount`）。
3. **写操作默认不同步、不合并、不做写后验证（严禁频繁同步与盲目回验）**：
   - **底层机理**：写后验证必须依赖 BCS 全量同步，若不同步，拉取的快照只是未修改前的旧数据；而触发 BCS 全量同步（`fetch`）极其缓慢（约 40~50s/店），频繁同步极易触发平台接口限流与账号风控。
   - **业务事实**：写操作（改价 price、改库存 stock、下架 trash、改折扣 discount、清理 clean、改尺寸 dimension、上架 replicate/import-shelve 等）直接调用 WB 或 BCS 写入接口，成功返回即代表平台侧已生效。
   - **AI 与使用者铁律**：所有写操作执行完毕后在控制台打印提示并**直接结束**。**严禁在写操作后擅自补跑 `fetch`、`merge` 或自写脚本调接口去验证写后结果**。仅在极低频的阶段性全局大盘点、或用户明确手动要求时，才由人工显式执行同步与合并。
4. **单店独立表 + 全局归属池 + 增量聚合总表（多店解耦核心）**：
   - **单店映射表**（`data/shops/shop_{id}_{name}.xlsx`）：每家活跃店铺拥有一张独立的映射表，反映该店铺当前存活在架的真实商品清单、单店 nmId、各店在架价格与库存，是店铺级真实资产。
   - **全局 VC 归属与纠偏池**（`data/state/vc_known.json` 与 `vc_override.json`）：解耦「商品中文名归属」与「店铺生命周期」。无论是统一审核、前缀自动识别，还是人工通过 `mapping-rename` 纠偏的 VC 归属，均沉淀入全局池；老商品上新店时免审核自动认领。
   - **聚合全景总表**（`data/价格映射表.xlsx`）：`wb.py merge` 自动同步所有活跃单店表并执行 Outer Join，重建 8-Sheet 全景总表。下游 `ops`、`mabang` 等全量跨店操作完全基于总表无缝兼容。
   - **“消失即移除”与店铺增删解耦**：
     - **停用/归档店铺**：将单店表移入 `data/shops/_archive/` 后执行 `merge`，总表立即剔除该店，且该店独有的 VC 从总表中彻底消除（防止跨店批量操作发脏请求）；日后店铺恢复只需移回并 `merge` 即可瞬间无损复原。
     - **在架商品下架**：单店下架（`wb.py trash`）或清理（`wb.py clean`）完成后直接结束，**日常业务中绝对不要在下架后跑 fetch + merge**！下架在 WB/BCS 平台端已实时生效并将库存清零；映射总表保留历史行完全不影响后续业务逻辑（后续批量改价等操作时系统根据快照与接口自动跳过已下架/0值商品）。仅在未来的周期性全局大盘点或用户显式要求重构映射表时，才做 `fetch + merge`，届时已下架商品才会自然从单店表与总表中移除。
5. **价格下限**：目标价 ≤ 原价÷2 时 WB 静默拒绝（返回 200 不生效）→ ops 自动剔除。
6. **0 值商品是正常数据**（WB 延迟/受限）：照常修改并显式报告，复查仍 0 不反复操作。
7. **删除/下架不可逆**：默认 dry-run，需 `--apply`；trash/库存归零还需 `--yes`。
8. **店铺串行执行**（勿并行，实测并行触发限速慢 5 倍）；批量 ≤300/批、间隔 0.15-0.6s。
9. **下架两阶段**：先清库存为 0，再移回收站；清库存失败仍下架但显式报告。
10. **vendorCode 中段 4 字母**命中商品价格表前缀码 → 免人工审核自动补录（兼容 `BCS-{前缀}-{nm}`、`BCS-{前缀}-ozon-card-{nm}` 与 `BCS-{前缀}-{标识}/{nm}` 如 `BCS-QQNN-WRLINWI/1078999444`）。
11. **改折扣同样触发价格审核**（实测 2026-08-20）：WB 按「新价相对原价降幅」判定，改价**或改折扣**降幅落入 30–49.9% → 进隔离区（quarantine），**必须** **`price-review --apply`「应用新价格」才生效**；>50% 直接被拒。因此**每次** **`promo-apply`** **/** **`discount --apply`** **之后必跑一次** **`price-review`**（dry-run 预览 → 有货再 `--apply`）。
12. **上架** **`shopDatas[].nmId`** **提交前置空** **`null`**（BCS 插件 1.2.2，2026-08-26）：新版后端按 `nmId` 判「是否已有卡」，带值会被当「更新已有卡」处理导致上架失败；WB 原始 nmId 由 `sourceSku` 保留，勿在上架请求体携带。
13. **马帮订单处理顺序（2026-09-07）**：新订单**先登记飞书「订单登记」表**（订单编号去重），再执行 匹配更换→预报批次→上传→物流交运「莫斯科仓-七库海外仓」；处理后订单离开待处理页进入全部订单，故不可颠倒。各步幂等：`order_label` 含「已预报」跳过生成批次、已上传批次不在待上传列表、`cansend1logisticsHtml` 已选交运跳过；改折扣/报名后必跑 `price-review` 的规则不变。
14. **wb 码按店不同**：同一 vendorCode 在各店的 nmId 各不相同；飞书登记/统计用的 wb编号 = **下单店铺自己的 nmId**（快照 vendorCode→nmId 反查），映射表 WB商品码只是主店码。
15. **飞书销量统计用仪表盘**：实时聚合图表（销量看板）直接引用「订单登记」表，手动/脚本改动即时反映；不另建聚合数据表。

## 六、数据流全链路时序

```
wb.py fetch          ① 并发同步各活跃店（WB→BCS ~50s）→ 逐店拉 BASE 在架 → data/products/shop{id}_json（仅按需低频运行）
wb.py mapping / review ② 活跃店铺并集按 vc 去重 → 四分类（已知跳过/前缀自动/候选池/未归属）→ 生成统一核对/审核工作台
   （人工）          ③ 打开 workbench HTML 勾选归属/排除 → 导出 统一审核.json（或使用 mapping-rename 快速纠偏）
wb.py merge [审核]   ④ 增量合并与多店聚合：
                        a. 刷新活跃店铺单店表（data/shops/shop_*.xlsx），写入新归属到全局池（vc_known.json / vc_override.json）；
                        b. 跨活跃店铺 Outer Join 汇聚各店在架状态（自动跳过 _archive/ 目录）；
                        c. 重建 8-Sheet 聚合全景总表（价格映射表.xlsx）。
wb.py shops-mapping  ④a 【独立维护】刷新单店映射表（支持 --shop-id 指定单店或刷新全部活跃店铺）
wb.py mapping-rename ④b 【纠偏改名】修改某商品中文名：自动持久化全局纠偏池，并级联更新全部单店表与聚合总表
wb.py price/stock/trash  ⑤ 按映射表定位 nmId/chrtId/warehouseId → dry-run 预览 → --apply 执行 → ops_result.csv → 写入接口成功即代表完成，默认直接结束（★ 严禁擅自补跑 fetch+merge 或自写验证）
wb.py dimension         ⑤a 按商品价格表「尺寸」列批量改尺寸 → POST shopKeeper/dimension/batch → 写 CSV → 结束（不同步/不做写后验证）
wb.py replicate      ⑥ 跨店复制上架（vc×多店，基于本地快照；单批50个批量推送）→ 上架成功即结束（默认不同步/不自动 merge）
wb.py import-shelve  ⑥a 他人映射表导入上架（按 WB原始nmId 差集与我方前缀码）→ 一次请求多店上架 → 结束（默认不同步/不自动 merge）
wb.py shelve         ⑥b 新版批量上架（指定/智能解析 nm/vc/前缀/价格/尺寸/店铺，分批 50 推送）→ 成功即结束
wb.py shelve-old     ⑥c 旧版上品建卡（指定/智能解析 nm/任意自定义vc/俄文标题/主图建卡，单品多店推送）→ 成功即结束
（★ 铁律：写操作默认禁止写后验证与同步合并）：写后验证必须依赖全量同步，不同步拉取的快照是未修改前的旧数据；而频繁全量同步耗时极长且极易触发限流风控。改价/库存/下架/折扣/清理/上架/尺寸等操作执行完毕即代表完成，默认严禁自行执行 fetch、merge 或自写脚本验证，除非用户显式手动要求。

（促销线）
wb.py promo-apply    ⑦ cookie 会话 → timeline 查可参加 → detail 取 periodID → applyAll（幂等）
wb.py discount-wb    ⑧ WB 原生批量（按需调用）：list/goods/filter 按折扣排序找目标（降序取 >阈值；`--below N` 走升序取 <阈值；只给 `--below` 时不再拉降序侧）→ **两阶段提交** `upload/task?checkChange=true`（预检，仅回 price/quarantine 弹窗标记）→ `?checkChange=false`（真正落库，回 `data.id` 任务号）→ 默认不做写后验证（生效延迟）；日常自动化默认仍走 BCS discount
wb.py discount       ⑧a BCS 全量（模式2，慢）：默认不自动同步 → 查（全量用 --threshold -1）→ 批量改 → 仅提示；加 --sync 才前置同步 + 提交后同步复核
wb.py price-review   ⑧b ⚠ 改折扣后**必跑且须 `--apply`**：查隔离区（quarantine/goods）待审商品 → 应用新价格；0%→49% 这类降幅落 30-49.9% 的商品会进隔离区，不「应用新价格」折扣不生效（实测隔离区会逐个列出对应 nmID）
wb.py clean          ⑨ 草稿箱删除（nmUuid）+ 回收站删除（nmId，失败归零库存）；回收站统计以 countByFilter(TRASH) 实时计数为准（list(TRASH) 为列表缓存可能滞后）
wb.py banned         ⑨b 查询被阻止商品（tableListImprovable 分页）→ dry-run → --apply moveNmsToTrash 移回收站 → count/列表自动复核
wb.py appeals        ⑨c 只读投诉单：callcenter v1/appeals 列表（游标倒序翻页）→ 筛 status_id=1(等待回复) + decide_counter=N → v3/appeals/{id} 取 brands[].products[].nmid → 本地真源反查供应商代码（店快照→映射表，未收录即标注）→ 明细表 + 去重 nmId 行 + 去重 vendorCode 行 + CSV（不写平台）
wb.py daily          ⑩ morning=报名+改价（含价格审核）/ check=只改价（含价格审核）（可手动跑，或仅在主动运行 wb.py schedule 后由计划任务 9:00/11/15/19 点触发；默认不建计划任务）
```

### 每日新订单处理链路（2026-09-07 新增；原 `orders-pipeline` 编排已于 2026-09-17 由 `mabang-process` + `feishu-register` 取代，对应 order_pipeline.py 已删除）

```
wb.py mabang-orders   ① VC→映射表中文名→价格表库存SKU → replaceOrderItem 强制更换（先匹配）
wb.py feishu-register ② 登记飞书「订单登记」（数据源=orderalllist 最近500 + 待处理订单两路合并，
                        订单编号去重；只匹配未进预报/上传/交运的单也登记；
                        库存SKU=本地商品价格表「库存SKU」列（查不到留空）；中文名=本地映射表；排除已取消订单）
wb.py mabang-forecast ③ 生成预报批次（已预报跳过）→ aamz 上传 → 等待 150s → 物流交运（已选跳过）
                      ④ 归属统计（店铺×中文名单量 CSV）
```

- 飞书侧：Base 内「订单登记」明细表（日期精确到分钟/店铺短名/BCS编号/中文名/wb编号/商品链接/订单量/下单日期公式字段）+「销量看板」仪表盘（实时图表：每天×中文名柱状图、中文名与商品(链接)排行）。
- 马帮接口（order.oTc 两变体 / showOrderItems / searchStockList / replaceOrderItem / getForecastLogistics / doBatchCreateForecast / getForecastOrderList / uploadForecastBatch / getReportingInformation / doReportingInformation）参数与实测结论见 `api/BCS_API完整文档_核对版.md` 第八章。

## 七、映射表 8 Sheet 结构

> 聚合全景总表（`data/价格映射表.xlsx`）由 `wb.py merge` 汇集所有活跃单店表（`data/shops/shop_*.xlsx`）自动 Outer Join 生成，为跨店运营及下游脚本提供统一视图。

| Sheet   | 内容                                                          | 用途             |
| ------- | ----------------------------------------------------------- | -------------- |
| 映射总表    | 14 列（中文名/vendorCode/双倍售价/主店价/折扣/club/库存/俄文标题/主图/尺寸/毛重/店铺覆盖） | 主数据，ops 筛选依据   |
| 多重映射冲突  | 同一 vc 被多商品勾选                                                | 归属裁决           |
| 未映射商品   | 店铺在架但未映射                                                    | 排查漏配/非货盘       |
| 已排除清单   | vc + 排除原因                                                   | 增量 merge 排除状态源 |
| 待核查清单   | 商品价格表有但无归属 vc                                               | 人工补录           |
| 店铺全量商品  | 主店全部在架明细                                                    | 对照             |
| 店铺覆盖矩阵  | vendorCode × 多店（单元格=库存）                                    | 跨店覆盖           |
| 多店价格一致性 | 同品跨店价格不一致告警                                                 | 复核             |

## 八、历史沿革

| 时间         | 事件                                                                                                                                 |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| 早期         | 全量重建 merge（依赖审核文件）                                                                                                                 |
| 2026-08-16 | 增量 merge（映射表=唯一状态源）；fetch 改 filter=BASE；集成 product/sync 并发同步；网络层 urllib→requests；ops 一键操作；统一核对工作台（5 店并集一页两区）；移除笔记本特殊处理；商品价格表重建 7 列 |
| 2026-08-17 | BASE\_DIR 相对化；ops\_result.csv 追加；mapping-check、trash 两阶段（先清库存再下架）                                                                  |
| 2026-08-18 | 检查价格 + 促销折扣整合为 wb\_ops 包，统一凭证 credentials.json、统一入口 wb.py、统一文档 docs/，清理废弃脚本                                               |
| 2026-09-12 | **单店独立映射表与多店解耦架构重构**：将原本单一映射表拆分为各店铺独立的单店映射表（`data/shops/shop_{id}_{name}.xlsx`），引入全局 VC 归属与纠偏池（`data/state/vc_known.json`, `vc_override.json`），`merge` 采用各活跃店 Outer Join 机制自动聚合 8-Sheet 全景总表，支持店铺一键归档解耦（`_archive/`）与 VC 级联纠偏更名（`mapping-rename`） |
| 2026-09-15 | **轻量整洁架构升级与深度解耦**：全面落地 5 层分层架构（domain/adapters/storage/services/framework），消除跨域私有依赖；彻底消除跨域品名反查耦合（沉降至 MappingRepository.build_vc_resolver）；WB 取消订单 API 归位至 wb_client；拆分 4 大单体脚本（抽取 wb_card、ops_plan、ops_executor、mabang_client、mapping_excel）；建立全量自动化测试套件 tests/test_all_commands.py 覆盖全部 39 个 CLI 命令（100% PASS）；发布 DEVELOPMENT_GUIDE.md 明确后续开发规范与格式要求。 |
| 2026-09-16 | **新增 `appeals` 只读投诉单查询**：接入 WB callcenter 子系统（列表 `v1/supplier/appeals` 游标倒序翻页 + 详情 `v3/supplier/appeals/{id}`），按「未处理=等待回复 status_id=1」＋「剩余天数（decide_counter）恰好=N」筛选，输出控制台明细表 + 英文逗号分隔的去重商品编号（nmId）+ CSV；新增 `adapters/callcenter_client.py` 与 `services/support/complaints.py`，CLI 命令数 39 → 40。 |
| 2026-09-16 | **开发体验补齐**：新增 `docs/REUSE_GUIDE.md`（能做 X 用哪个模块/函数速查 + 代码模板 + 复用铁律 + 一键重扫公共 API）；新增 `tests/run_tests.py` 按需测试选择器（`--changed` 自动选档 / `--cmd` 定向 / `--help-smoke` 秒级回归），门禁从「每次全量」改为「只跑改动相关」，全量仅在跨层改动或发版时执行。 |
| 2026-09-16 | **改折扣支持双侧区间**：`wb.py discount` 新增 `--below N`（折扣<N 侧，与 `--threshold` 并集去重）；适配层新增 `WBClient.fetch_discount_goods_asc()` 走 WB 折扣**升序**列表（`sortOrder=1`，抓包已验证）——原降序实现「首条 ≤ threshold 即截断」无法覆盖低折扣区间，故不能再靠本地快照兜底。 |
| 2026-09-17 | **结构审查整改（可移植性/去重/分层/卫生/测试覆盖）**：① 账号写死治理 —— 代码与文档里的「5 店 / 旧店铺ID」改为中性或动态文案，`replicate.KNOWN_WAREHOUSES` 改由数据文件 `data/state/known_warehouses.json` 驱动；② 真重复实现合并 —— ops 参数定义下沉 `framework/cli_args.py`（cli 与 ops 共用，保持启动零业务依赖）、`products.shop_ids_from_disk` 转发仓储、`support_svc` 删除与 `questions_watch` 重复的状态读写；③ 清理死代码/遗留 shim —— 删除 `order_pipeline.py`（零引用）、`replicate.fetch_wb_detail` 弃用桩、`llm_client` 兼容函数、CLI `--detail-source` 弃用参数；④ 分层修正 —— `order/mabang_stock.py` 内直接 requests 调用下沉到 `adapters/mabang_client`（`fetch_stock_list`/`download_file`），services 内已无原生 HTTP；⑤ 运维卫生 —— `ops_result.csv` 按月自动归档到 `data/logs/archive/`、技能去掉仓库镜像副本（唯一份在 `~/.workbuddy/skills/`）；⑥ 测试补全 —— 新增 9 个只读用例，**40 个命令全部有专属用例**（共 41 用例）。 |
| 2026-09-17 | **改折扣两阶段提交修复（抓包驱动）**：据 `api/网络请求/wb批量修改折扣+降价提示.har` 确认 `upload/task` 必须**两步**——`?checkChange=true` 只做预检（仅回 `priceModal`/`quarantineModal` 弹窗标记，**无任务号、不落库**），`?checkChange=false` 才真正提交并回 `data.id`。旧实现 URL 写死 `checkChange=true`，导致 taskId 恒为 None、平台侧从未落库（表现为「改折扣没生效」）。适配层 `upload_batch_discount` 改为「预检 → 自动确认 → 提交」，新增 `precheck_batch_discount` 与 `DiscountUploadResult`（透出弹窗标记与真实任务号）；同时修两处逻辑/性能缺陷：① 只给 `--below` 时不再拉降序侧（`threshold=-1` 会翻遍全量目录，单次 12+ 分钟 → 修复后 42 秒）；② `_disc_matched` 在「两侧阈值均未启用」（`--vc` 精确定向）时放行，原先恒判不匹配导致 `discount --vc` 永远输出「无匹配」。新增离线用例 `test_04c_discount_upload_two_phase`（mock 断言 checkChange 两次调用顺序与任务号）。 |
① 数据源由单一 orderalllist 改为 **orderalllist 最近500 + 待处理订单（tabId=7）两路合并去重**（只做了匹配、未进预报/上传/交运流程的订单只出现在待处理列表，仅按 orderalllist 会漏登；`--no-pending` 可关闭）；② **库存SKU 改为以本地商品价格表「库存SKU」列（第 8 列）为准，查不到一律留空**，不再回写马帮系统匹配值（消除 `BCS-xxx-40-56`、`ETPB-PINK` 等非法/错位值）；③ 商品中文名一律取本地映射表（查不到留空但**仍登记**）；④ 登记日志把「真排除」与「字段留空」分开计数，避免把仍登记的单误读成被排除。 |

## 九、外部依赖与运行环境

- **运行**：用你自己的 venv Python（勿用系统 python；作者示例路径 `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，换电脑请替换）。脚本内部用 `sys.executable`/`os.path.abspath(__file__)` 推导，**不硬编码任何绝对路径**。
- **Python 依赖**：`requests`、`openpyxl` 3.1.5、`pypinyin`（前缀码生成，暂未在新代码中使用，保留）
- **接口文档**：`api/BCS_API完整文档_核对版.md`（23 个 API 速查 + 7 章；含真实 token/账号信息，已随 `api/` 整体 gitignore，公开仓库不含此目录，如需请向作者索取）
- **限流参数**：429 指数退避（bcs.py）；ops 批间 150ms / 店间 600ms；促销活动间 1s

