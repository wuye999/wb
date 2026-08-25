# wb_ops · 架构文档

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
│   ├── import_shelve.py       他人映射表导入上架：按 WB原始nmId 差集 → 我方前缀优先生成新 vc 上架（复用 replicate 的 WB 数据获取/仓库/记录）
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
│   ├── 价格映射表.xlsx         唯一状态源（merge 自动重建）
│   ├── products/             shop{id}_products_all.json 快照
│   ├── state/                同步状态 / 自动新增清单
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
          │ 依赖
          ▼
支撑层   bcs / wb_api / products / workbench / keywords
         common / credentials / config
```

**规则**：业务模块之间**不互相 import**，只依赖支撑层。`cli.py` 只做解析与分发，不含业务逻辑。
（唯一例外：`mapping_sync` 复用 `mapping` 与 `workbench` 的公共函数，这是有意的组合关系。）

## 三、两套鉴权体系

| | BCS 云端 API | WB 卖家后台 |
|---|---|---|
| 域名 | wb.bcserp.com/prod-api | seller.wildberries.ru / discounts-prices / seller-content |
| 凭证 | Bearer JWT + X-Limit-Key + Cookie(Admin-Token/Limit-Key) | authorizev3 + wb-seller-lk + Cookie（cfidsw-wb 等） |
| 模块 | bcs.py | wb_api.py |
| 失效表现 | 401（token 过期）/ 405（缺 Limit-Key） | 403（cfidsw-wb 过期） |
| 存储 | credentials.json `bcs` 段 | credentials.json `wb` 段 |

详见 [CREDENTIALS.md](CREDENTIALS.md)。

## 四、ID 体系（关键）

| ID | 格式/来源 | 作用域 | 用途 |
|---|---|---|---|
| `vendorCode` | `BCS-{4位前缀码}-{WB原始nmId}` | 跨店唯一键 | 映射表主键、review/merge 比对、ops 定位 |
| `nmId`（BCS 内部） | 店铺 JSON 顶层字段 | 每店不同 | 改价（price/batch）、下架（removeToTrash） |
| `chrtId`（规格） | 店铺 JSON `sizeList[].chrtId` | 每店每规格 | 改库存（stock/batchSetByChrtIdsBatch） |
| `warehouseId`（仓库） | `stockList[].warehouseId` | 每店每仓 | 改库存的分组键 |

> 映射表**无 nmId/chrtId 列**——ops 操作前必须 `fetch`，从 `data/products/shop{id}_products_all.json` 按 vendorCode 查行取 ID。

## 五、核心业务规则（不可改变）

1. **半价口径**：商品价格表「双倍售价」= 最低售价 ×2（D 列公式）；店铺整数价 = `floor(双倍售价)`。
2. **折扣规则**：所有 `discount > 50%` → 改为 `50%`（`wb.py discount`）。
3. **先同步再查询**：BCS productList 是缓存，改价/报名/删除前后应先触发同步（~40-50s/店）否则漏查/误判。**默认不自动同步**：写操作（改价/库存/下架/折扣/清理/上架）默认**不自动同步、不写后验证、不自动 merge**，命令执行完成仅打印提示（因 WB/BCS 异步回填，当场验证不一定准确）；需同步在架并合并映射表时，给写命令加 `--sync`（命令内自动 fetch+merge）或显式 `wb.py fetch`。`fetch`/`orders` 属显式同步/查询工具，仍默认同步。
4. **增量 merge 唯一状态源**：映射表 xlsx = 状态；审核文件是一次性输入用完即弃；继承旧归属 + 追加审核 + 消失即移除（缺店保护）。
5. **价格下限**：目标价 ≤ 原价÷2 时 WB 静默拒绝（返回 200 不生效）→ ops 自动剔除。
6. **0 值商品是正常数据**（WB 延迟/受限）：照常修改并显式报告，复查仍 0 不反复操作。
7. **删除/下架不可逆**：默认 dry-run，需 `--apply`；trash/库存归零还需 `--yes`。
8. **5 店串行执行**（勿并行，实测并行触发限速慢 5 倍）；批量 ≤300/批、间隔 0.15-0.6s。
9. **下架两阶段**：先清库存为 0，再移回收站；清库存失败仍下架但显式报告。
10. **vendorCode 中段 4 字母**命中商品价格表前缀码 → 免人工审核自动补录。
11. **改折扣同样触发价格审核**（实测 2026-08-20）：WB 按「新价相对原价降幅」判定，改价**或改折扣**降幅落入 30–49.9% → 进隔离区（quarantine），**必须 `price-review --apply`「应用新价格」才生效**；>50% 直接被拒。因此**每次 `promo-apply` / `discount --apply` 之后必跑一次 `price-review`**（dry-run 预览 → 有货再 `--apply`）。

## 六、数据流全链路时序

```
wb.py fetch          ① 并发同步 5 店（WB→BCS ~50s）→ 逐店拉 BASE 在架 → data/products/shop{id}_json
wb.py mapping        ② 5 店并集按 vc 去重 → 四分类（已知跳过/前缀自动/候选池/未归属）→ 统一核对工作台
   （人工）          ③ 打开 workbench HTML 勾选归属/排除 → 导出 统一审核.json
wb.py merge [审核]   ④ 增量合并 → 继承旧归属 + 追加审核 + 前缀补录 + 消失即移除 → 重建 8-Sheet 映射表
wb.py price/stock/trash  ⑤ 按映射表定位 nmId/chrtId/warehouseId → dry-run 预览 → --apply 执行 → ops_result.csv → 默认不自动同步/合并（仅提示），加 --sync 自动 fetch + merge（改价/库存/下架提交后自动增量合并映射表）
wb.py fetch + merge  ⑥ 写后验证（改价/库存写 WB 侧，需再同步才在 BCS 可见；下架立即可见）
wb.py replicate      ⑥b 快照覆盖判断（vc×5店，默认不自动同步、用本地快照）→ WB detail（BCS代理）+ card.json（CDN）→ /wbCollection/wb/new 上架缺失店铺（vc 与源店一致）→ 加 --sync 才自动 fetch 复核覆盖率 + 写后 merge（上架后映射表自动补录/同步覆盖；否则仅打印提示）
wb.py import-shelve  ⑥c 他人映射表「映射总表」解析 → 按 WB原始nmId 与我方快照差集（默认不自动同步、用本地快照）→ 我方前缀优先生成新 vc → 逐店上架 → 加 --sync 才自动 fetch 复核 + 写后 merge；否则仅打印提示
（★ 改价/库存/下架/上架/清理写操作默认不自动同步/不写后验证/不自动 merge，完成后仅打印提示；加 `--sync` 才自动 fetch + 增量 merge 映射表；下架/清理的 merge 会「消失即移除」对应商品）

（促销线）
wb.py promo-apply    ⑦ cookie 会话 → timeline 查可参加 → detail 取 periodID → applyAll（幂等）
wb.py discount-scan  ⑧ WB 实时（模式1，混合引擎）：list/goods/filter 按折扣从高到低找 >阈值 → 本地快照可定位价的商品经 BCS shopKeeper/price/batch 批量改（一次≤300）→ 快照缺失/无价回退 WB nm/upload/task 单条 + 提示 → 同一列表回验；不触发 BCS 全量同步
wb.py discount       ⑧a BCS 全量（模式2，慢）：默认不自动同步 → 查（全量用 --threshold -1）→ 批量改 → 仅提示；加 --sync 才前置同步 + 提交后同步复核
wb.py price-review   ⑧b ⚠ 报名/改折扣后必跑：查隔离区（quarantine/goods）待审商品 → 应用新价格（改折扣同样触发审核，不应用则新折扣不生效）
wb.py clean          ⑨ 草稿箱删除（nmUuid）+ 回收站删除（nmId，失败归零库存）
wb.py banned         ⑨b 查询被阻止商品（tableListImprovable 分页）→ dry-run → --apply moveNmsToTrash 移回收站 → count/列表自动复核
wb.py daily          ⑩ morning=报名+改价（含价格审核）/ check=只改价（含价格审核）（可手动跑，或仅在主动运行 wb.py schedule 后由计划任务 9:00/11/15/19 点触发；默认不建计划任务）
```

## 七、映射表 8 Sheet 结构

| Sheet | 内容 | 用途 |
|---|---|---|
| 映射总表 | 14 列（中文名/vendorCode/双倍售价/主店价/折扣/club/库存/俄文标题/主图/尺寸/毛重/店铺覆盖） | 主数据，ops 筛选依据 |
| 多重映射冲突 | 同一 vc 被多商品勾选 | 归属裁决 |
| 未映射商品 | 店铺在架但未映射 | 排查漏配/非货盘 |
| 已排除清单 | vc + 排除原因 | 增量 merge 排除状态源 |
| 待核查清单 | 商品价格表有但无归属 vc | 人工补录 |
| 店铺全量商品 | 主店全部在架明细 | 对照 |
| 店铺覆盖矩阵 | vendorCode × 5 店（单元格=库存） | 跨店覆盖 |
| 多店价格一致性 | 同品跨店价格不一致告警 | 复核 |

## 八、历史沿革

| 时间 | 事件 |
|---|---|
| 早期 | 全量重建 merge（依赖审核文件） |
| 2026-08-16 | 增量 merge（映射表=唯一状态源）；fetch 改 filter=BASE；集成 product/sync 并发同步；网络层 urllib→requests；ops 一键操作；统一核对工作台（5 店并集一页两区）；移除笔记本特殊处理；商品价格表重建 7 列 |
| 2026-08-17 | BASE_DIR 相对化；ops_result.csv 追加；mapping-check、trash 两阶段（先清库存再下架） |
| 2026-08-18 | **本次重构**：检查价格 + 促销折扣整合为 wb_ops 包，统一凭证 credentials.json、统一入口 wb.py、统一文档 docs/，清理废弃脚本 |

## 九、外部依赖与运行环境

- **运行**：用你自己的 venv Python（勿用系统 python；作者示例路径 `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，换电脑请替换）。脚本内部用 `sys.executable`/`os.path.abspath(__file__)` 推导，**不硬编码任何绝对路径**。
- **Python 依赖**：`requests`、`openpyxl` 3.1.5、`pypinyin`（前缀码生成，暂未在新代码中使用，保留）
- **接口文档**：`api/BCS_API完整文档_核对版.md`（23 个 API 速查 + 7 章；含真实 token/账号信息，已随 `api/` 整体 gitignore，公开仓库不含此目录，如需请向作者索取）
- **限流参数**：429 指数退避（bcs.py）；ops 批间 150ms / 店间 600ms；促销活动间 1s
