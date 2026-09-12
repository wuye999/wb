# wb\_ops · 架构文档

> 面向：开发人员 / 接手维护者 / AI。想快速上手先读 [README.md](README.md)；查命令看 [CLI.md](CLI.md)。
> ⚠ 目录结构图里的根目录、店铺 ID、Python 路径均为作者环境示例，脚本用相对路径（见文末「运行环境」），换电脑/账号无需改代码。

## 一、目录结构

```
<仓库根目录>/   （如 D:\E\脚本\bcs_api\wb，可放到任意位置）
├── wb.py                    ★ 统一入口（薄启动器 → wb_ops.cli.main）
├── wb_ops/                  ★ 核心库（Python 包）
│   ├── __init__.py            版本号 + 公共导出
│   ├── cli.py                 ★ 统一 CLI（argparse 子命令 → 分发到各业务模块）
│   ├── config.py              非敏感配置：路径常量（BASE_DIR→data/）、默认阈值、VC_PREFIX_RE
│   ├── credentials.py         ★ 统一凭证加载（读 data/credentials.json）
│   ├── common.py              共享工具：UA / CookieExpiredError / jwt_payload / to_int / stdout UTF-8
│   ├── bcs.py                 BCS 云端 API 客户端（Bearer + X-Limit-Key）
│   ├── wb_api.py              WB 卖家后台 API 客户端（cookie 三件套会话）
│   ├── products.py            商品拉取 / 快照 / 同步 / 空商品判定
│   ├── mapping.py             商品价格表解析 + 映射表构建（8-Sheet）
│   ├── mapping_sync.py        多店 review / 增量 merge
│   ├── mapping_check.py       映射表核查工作台（带图，可疑项标记）
│   ├── mismatch_check.py      货不对板筛查工作台（看图勾选，导出 vc 下架；支持按映射表创建时间时间段筛选 --begin/--end/--days）
│   ├── workbench.py           HTML 工作台渲染（合并 4 处重复模板）
│   ├── ops.py                 一键操作：改价 / 库存 / 下架（两段式 dry-run；改价可 --auto-review 自动应用新价格）
│   ├── replicate.py           跨店复制上架：部分覆盖 vc → 缺失店铺（vendorCode 与源店一致；WB detail 经 BCS 代理 + card.json CDN）
│   ├── import_shelve.py       他人映射表导入上架：按 WB原始nmId 差集 → 我方前缀优先生成新 vc 上架（复用 replicate 的 WB 数据获取/仓库/记录；支持他人 `BCS-{前缀}-ozon-card-{WB商品码}` 格式并保留 `ozon-card-` 尾段）
│   ├── promo.py               促销报名
│   ├── discount.py            折扣改价（>50%→50%）
│   ├── banned.py              查询并删除被阻止的商品（WB banned：tableListImprovable 查询 / moveNmsToTrash 移回收站 / count 复核）
│   ├── clean.py               草稿箱 / 回收站清理（回收站 deleteAllSize 一键清空）
│   ├── price_review.py        价格审核「应用新价格」（WB 隔离区 quarantine/goods）
│   ├── orders.py              订单查询（BCS ozonOrder：同步/进度/列表/状态计数）
│   ├── questions.py           买家未处理提问查询 + 回复（WB questions/answer）
│   ├── cookies.py             从抓包 md 刷新凭证
│   ├── daily.py               每日任务启动器（morning/check）
│   └── schedule.py            Windows 计划任务管理
├── data/                     ★ 统一数据目录
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
├── docs/                     ★ 文档（README/ARCHITECTURE/CLI/USAGE/CREDENTIALS）
├── api/                      BCS API 抓包/内部文档（含真实 token，本地参考，不随公开仓库分发）
├── _archive/                 废弃脚本 + 一次性数据 + 旧文档（可回滚）
└── _scratch/                 ★ AI 临时工作区（写脚本/处理文件/中间产物；已 gitignore，不进公开仓库）
```

## 二、模块分层与依赖方向

```
入口层   cli.py（子命令分发）
          │ 调用
          ▼
业务层   mapping / mapping_sync / mapping_check / mismatch_check / ops
         promo / discount / banned / clean / cookies / daily / schedule
         price_review / orders / questions
         mabang / feishu_register / order_pipeline
          │ 依赖
          ▼
支撑层   bcs / wb_api / products / workbench / keywords
         common / credentials / config
```

**规则**：业务模块之间**不互相 import**，只依赖支撑层。`cli.py` 只做解析与分发，不含业务逻辑。
（唯一例外：`mapping_sync` 复用 `mapping` 与 `workbench` 的公共函数，这是有意的组合关系。）

## 三、两套鉴权体系

| <br /> | BCS 云端 API                                               | WB 卖家后台                                                   |
| ------ | -------------------------------------------------------- | --------------------------------------------------------- |
| 域名     | wb.bcserp.com/prod-api                                   | seller.wildberries.ru / discounts-prices / seller-content |
| 凭证     | Bearer JWT + X-Limit-Key + Cookie(Admin-Token/Limit-Key) | authorizev3 + wb-seller-lk + Cookie（cfidsw-wb 等）          |
| 模块     | bcs.py                                                   | wb\_api.py                                                |
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
8. **5 店串行执行**（勿并行，实测并行触发限速慢 5 倍）；批量 ≤300/批、间隔 0.15-0.6s。
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
（★ 铁律：写操作默认禁止写后验证与同步合并）：写后验证必须依赖全量同步，不同步拉取的快照是未修改前的旧数据；而频繁全量同步耗时极长且极易触发限流风控。改价/库存/下架/折扣/清理/上架/尺寸等操作执行完毕即代表完成，默认严禁自行执行 fetch、merge 或自写脚本验证，除非用户显式手动要求。

（促销线）
wb.py promo-apply    ⑦ cookie 会话 → timeline 查可参加 → detail 取 periodID → applyAll（幂等）
wb.py discount-scan  ⑧ WB 实时（模式1，混合引擎）：list/goods/filter 按折扣从高到低找 >阈值 → 本地快照可定位价的商品经 BCS shopKeeper/price/batch 批量改（一次≤300）→ 快照缺失/无价回退 WB nm/upload/task 单条 + 提示 → 同一列表回验；不触发 BCS 全量同步
wb.py discount       ⑧a BCS 全量（模式2，慢）：默认不自动同步 → 查（全量用 --threshold -1）→ 批量改 → 仅提示；加 --sync 才前置同步 + 提交后同步复核
wb.py price-review   ⑧b ⚠ 报名/改折扣后必跑：查隔离区（quarantine/goods）待审商品 → 应用新价格（改折扣同样触发审核，不应用则新折扣不生效）
wb.py clean          ⑨ 草稿箱删除（nmUuid）+ 回收站删除（nmId，失败归零库存）；回收站统计以 countByFilter(TRASH) 实时计数为准（list(TRASH) 为列表缓存可能滞后）
wb.py banned         ⑨b 查询被阻止商品（tableListImprovable 分页）→ dry-run → --apply moveNmsToTrash 移回收站 → count/列表自动复核
wb.py daily          ⑩ morning=报名+改价（含价格审核）/ check=只改价（含价格审核）（可手动跑，或仅在主动运行 wb.py schedule 后由计划任务 9:00/11/15/19 点触发；默认不建计划任务）
```

### 每日新订单处理链路（2026-09-07 新增，`orders-pipeline` 编排）

```
wb.py mabang-orders   ① VC→映射表中文名→价格表库存SKU → replaceOrderItem 强制更换（先匹配）
wb.py feishu-register ② 登记飞书「订单登记」（订单编号去重；库存SKU=匹配后实际值；排除已取消订单）
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

## 九、外部依赖与运行环境

- **运行**：用你自己的 venv Python（勿用系统 python；作者示例路径 `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，换电脑请替换）。脚本内部用 `sys.executable`/`os.path.abspath(__file__)` 推导，**不硬编码任何绝对路径**。
- **Python 依赖**：`requests`、`openpyxl` 3.1.5、`pypinyin`（前缀码生成，暂未在新代码中使用，保留）
- **接口文档**：`api/BCS_API完整文档_核对版.md`（23 个 API 速查 + 7 章；含真实 token/账号信息，已随 `api/` 整体 gitignore，公开仓库不含此目录，如需请向作者索取）
- **限流参数**：429 指数退避（bcs.py）；ops 批间 150ms / 店间 600ms；促销活动间 1s

