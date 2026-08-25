# wb_ops · 调用文档（CLI.md）

> 统一入口：仓库根目录 `python wb.py <子命令>`（等价 `python -m wb_ops <子命令>`）。
> 所有命令在**仓库根目录**下执行；`python` 指你的 venv Python（作者示例：`C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，**换电脑请替换成你自己的**，勿用系统 python）。
> ⚠ 下文示例中的店铺 ID（如 5272、5280）、`--shops` 值均为**作者店铺示例**，换成你自己的店铺 ID（`wb.py shops` 可查）。

## 一、通用约定

- **退出码**：`0` 正常（含 dry-run / 违规跳过）｜`1` 参数/环境错误｜`130` 中断。
- **dry-run 默认**：所有写操作（改价/库存/下架/报名/折扣/清理）不加 `--apply` 只打印清单，不调写接口。
- **不可逆确认**：`trash`、`stock --amount 0`、`banned --apply`（移回收站）执行时需 `--yes`（或交互输入 y）。
- **写后验证（默认不自动）**：改价/库存写 WB 侧，需再 `fetch`（带同步）才在 BCS 可见；下架/回收站立即可见；`banned --apply` 自动复核 WB count（bannedCard 前后对比 + 重查列表）。**所有写操作（改价 price、库存 stock、下架 trash、折扣 discount、清理 clean、上架 replicate/import-shelve）默认不自动 BCS 同步 / 不写后验证 / 不自动 merge**（因 WB/BCS 异步回填，当场验证不一定准确，避免反复「修改→同步→核查」）。命令执行完成后**仅打印提示**，告知可运行 `python wb.py fetch && python wb.py merge` 补做，或在原命令后加 **`--sync`** 让命令内自动完成「同步在架商品并合并映射表（fetch+merge）」（下架/清理会「消失即移除」对应商品）。`banned` 的 WB 原生复核除外（不做 BCS 同步、不耗时，仍默认自动复核）。`fetch`/`orders` 本身是显式同步/查询工具，保持默认同步 + `--no-sync`，不受本次改动影响。
- **仓库默认莫斯科**：改库存 / 下架清库存 / 回收站归零 / 上架等所有仓库操作**默认只操作「莫斯科仓库」**（`config.DEFAULT_WAREHOUSE_NAME`，按仓库 name 匹配），**成都仓库默认不操作**。成都仓库（国内仓）需单独用 `wb.py remote-wh` 命令处理；切换默认仓库改 `config.py` 的 `DEFAULT_WAREHOUSE_NAME`。

## 二、子命令全表

### 数据准备 / 映射表维护

| 子命令 | 参数 | 用途 | 产物 |
|---|---|---|---|
| `shops` | 无 | 打印账号店铺列表 | 控制台 |
| `fetch` | `--shop-id N`（默认全部）/ `--no-sync` | 同步+拉取商品快照；默认先并发同步 5 店（~1.5 分钟） | `data/products/shop{id}_products_all.json` |
| `mapping` | `--legacy`（仅主店候选） | 统一核对工作台（5 店并集一页两区） | `data/workbench/价格映射核对工作台.html` |
| `mapping-import` | `<核对结果.json>` | 导入核对 → 生成映射表（旧格式单店初建） | `data/价格映射表.xlsx` |
| `mapping-check` | `--tol N`（默认 5） | 映射表核查工作台（带图，可疑项标记） | `data/workbench/映射表核查工作台.html` |
| `mismatch-check` | `--cn` / `--begin` / `--end` / `--days` | 货不对板筛查工作台（看图勾选，导出 vc 下架）；`--begin`/`--end`/`--days` 按映射表创建时间（上架/建立时间）时间段筛选，如只审昨天上架用 `--days 1` | `data/workbench/货不对板筛查工作台.html` |
| `review` | 无 | 其余 4 店新商品待审核（前缀命中自动补录） | `data/workbench/多店铺待审核.html` |
| `merge` | `[审核.json]`（可选） | 增量合并（继承+追加+消失即移除）→ 重建映射表 | `data/价格映射表.xlsx` |

### 一键操作（ops）

| 子命令 | 用途 | 操作参数 |
|---|---|---|
| `price` | 改价/改折扣 | `--price N` / `--discount N` / `--club-discount N` / `--keep-price` / `--auto-review` |
| `stock` | 改库存 | `--amount N`（默认 0） |
| `trash` | 下架（不可逆，先清库存再移回收站） | 无 |
| `replicate` | 跨店复制上架：部分覆盖的商品上架到缺失店铺（vendorCode 与源店一致、价格=源店现价、直上）；**启动默认不自动同步（用本地快照判断，可能滞后），加 `--sync` 才先同步全部店铺（约 2 分钟）** | `--vc` / `--prefix` / `--name` / `--shops` / `--limit` / `--apply` / `--sync` / `--no-verify` / `--interval S` / `--cn-stock "中文名:库存,..."` |
| `import-shelve` | 他人映射表导入上架：他人有我方无的商品（按 WB原始nmId 匹配）上架到我的店铺；新 vc 我方前缀优先（中文名命中商品价格表前缀码），价格 = floor(双倍售价)；**启动默认不自动同步（用本地快照判断，可能滞后），加 `--sync` 才先同步全部店铺（约 2 分钟）** | `<他人映射表.xlsx>` + `--cn` / `--shops` / `--limit` / `--apply` / `--sync` / `--no-verify` / `--interval S` / `--cn-stock "中文名:库存,..."` |

筛选参数（互斥，不传 = 全部）：`--sku` / `--name` / `--prefix` / `--vc` / `--all`
通用参数：`--shops 5272,5280`（限店铺） / `--apply`（执行） / `--yes`（跳过不可逆确认） / `--sync`（执行后自动同步在架商品并合并映射表，见「一、通用约定」第 12 条）

### 促销 / 折扣 / 清理 / 运维

| 子命令 | 用途 | 关键参数 |
|---|---|---|
| `promo-apply` | 促销报名（cookie 会话 applyAll） | `--apply` / `--shops` / `--days` / `--days-back` / `--sleep` |
| `banned` | 查询并删除被阻止的商品（WB 标记 banned，dry-run 默认；`--apply` 移到回收站+自动复核） | `--apply` / `--shops` / `--limit` / `--yes` / `--no-verify` |
| `discount` | 折扣改价**全量**（BCS，慢）：各店 >阈值→目标；**所有商品→50% 用 `--threshold -1`**；**默认不自动同步**，加 `--sync` 才前置同步 + 提交后同步复核；改折扣同样触发价格审核，之后必跑 `price-review` | `--apply` / `--threshold` / `--target` / `--shops` / `--sync` |
| `discount-scan` | 折扣改价**快速**（**混合引擎**）：WB 实时列表按折扣从高到低找 >阈值 → 快照可定位价的商品用 **BCS 批量改**（一次≤300，提速），快照缺失/无价的商品自动改走 **WB 单条**并提示（不做 BCS 全量同步）→ 同接口回验；逐店逐商品（同 vc 各店折扣不同） | `--apply` / `--threshold`(默认50) / `--target`(默认50) / `--shops` / `--limit` |
| `clean` | 清草稿箱/回收站（回收站一键清空：先归零有库存再 `deleteAllSize`）；**默认不自动同步/不自动合并**，加 `--sync` 才清理前同步 + 清理后自动 merge | `--target basket\|draft\|all` / `--apply` / `--shops` / `--limit` / `--sync` |
| `price-review` | 价格审核：查隔离区待审商品并「应用新价格」（**改价或改折扣降幅 30-49.9% 都会触发**） | `--apply` / `--shops` / `--limit` |
| `orders` | 订单查询（自动同步 + 查日期区间） | `--begin` / `--end` / `--days` / `--no-sync` / `--shops` / `--page-size` |
| `questions` | 买家未处理提问查询 + 回复（**自动关联中文名/标题/品牌/颜色/价格/描述/特征**） | `--shops` / `--reply` / `--question-id` / `--reply-all` / `--yes` / `--no-detail` |
| `questions-watch` | 买家提问实时监听（双模式：**front=前台AI** 打印提问/商品信息到控制台与日志、前台手动回复；**back=后台AI** 常驻轮询 + LLM 自动回复，DeepSeek/商汤等 OpenAI 兼容） | `--interval S` / `--mode front\|back`（默认 front）/ `--apply`（等价 back）/ `--shops` / `--once` |
| `cookies-update` | 从抓包 md 刷新凭证 | `<md文件>` |
| `daily` | 每日任务 | `morning\|check`（+ 透传参数） |
| `schedule` | 创建/删除 Windows 计划任务（⚠ 默认不创建，仅按需执行） | `--remove` |

## 三、示例

```bash
# 数据准备
python wb.py shops
python wb.py fetch                         # 同步+拉取 5 店（~1.5 分钟）
python wb.py fetch --no-sync               # 跳过同步快速拉取（~30s）
python wb.py fetch --shop-id 5272          # 单店
python wb.py mapping                       # 统一核对工作台
python wb.py review                        # 待审核工作台
python wb.py merge                         # 增量合并（无新审核）
python wb.py merge 统一审核.json            # 合并本次审核
python wb.py mapping-check --tol 3         # 核查工作台
python wb.py mismatch-check                 # 货不对板筛查工作台（看图勾选 → 导出 vc → trash --vc）
python wb.py mismatch-check --days 2        # 只审最近 2 天（昨天+今天）上架/创建的商品（--days 1=仅今天）
python wb.py mismatch-check --begin 2026-08-20 --end 2026-08-25  # 指定时间段（按映射表创建时间）

# 改价/折扣/库存/下架（dry-run 默认，加 --apply 执行；默认不自动同步/不写后验证，完成后仅打印提示；加 --sync 自动「同步在架商品并合并映射表」）
python wb.py price --name 充电宝 --apply --yes
python wb.py price --vc BCS-XXX-123 --price 130 --discount 20 --apply --sync
python wb.py price --vc BCS-XXX-123 --discount 30 --keep-price --apply --yes
python wb.py stock --prefix CYQX --amount 0 --apply --yes
python wb.py trash --vc BCS-XXX-123 --shops 5272 --apply --yes

# 跨店复制上架（部分覆盖的商品 → 缺失店铺；vendorCode 与源店一致、价格=源店现价）
# 默认不自动同步（用本地快照判断，可能滞后）；加 --sync 才前置同步全部店铺 + 上架后复核并合并映射表
python wb.py replicate                       # 预览全部部分覆盖商品（vc/源店/价格/目标店）
python wb.py replicate --prefix ZLTH --apply --sync  # 指定前缀码批量补齐（先行同步最新快照）
python wb.py replicate --vc BCS-XXX-123 --apply   # 单个 vc 补齐（目标店自动=缺失店）
python wb.py replicate --shops 5273 --apply  # 只补指定店
python wb.py replicate --cn-stock "感应灯:0,运动包:0" --apply  # 指定中文名上架库存（未指定用默认 999）

# 他人映射表导入上架（他人有我方无，按 WB原始nmId 匹配；新 vc 我方前缀优先、价格=floor(双倍售价)）
# 默认不自动同步（用本地快照判断，可能滞后）；加 --sync 才前置同步全部店铺 + 上架后复核并合并映射表
python wb.py import-shelve 他人映射表.xlsx          # 预览差集清单（含前缀来源标注）
python wb.py import-shelve 他人映射表.xlsx --cn 冲牙器   # 按他人表中文名过滤
python wb.py import-shelve 他人映射表.xlsx --shops 5273 --apply --sync  # 上架到指定店（先行同步）
python wb.py import-shelve 他人映射表.xlsx --cn-stock "充电线:100" --apply  # 指定中文名上架库存

# 促销/折扣/清理/价格审核/订单/提问
python wb.py promo-apply                   # 预览可报名活动
python wb.py promo-apply --apply           # 执行报名
python wb.py discount                      # 预览 >50% 商品
python wb.py discount --apply              # 执行 >50%→50%（默认不自动同步/不写后验证，完成后仅提示）
python wb.py discount --threshold -1 --target 10 --apply --sync   # 把所有在架商品折扣统一为 10%（含 0 折扣；阈值取负可选中全部商品；--sync 前置同步+提交后复核）
python wb.py discount-scan                  # ⚡ 快速改折扣（混合引擎，默认 >50%→50%）：预览（BCS批量/WB回退分类）
python wb.py discount-scan --apply          # ⚡ 执行：快照可定位价→BCS批量，缺失/无价→WB单条+提示
python wb.py price-review                  # ⚠ 报名/改折扣后必跑：预览隔离区待审商品
python wb.py price-review --apply          # ⚠ 应用新价格（改折扣也会触发审核，不应用则新折扣不生效）
python wb.py banned                        # 预览各店被阻止商品（WB 标记 banned）
python wb.py banned --apply --yes          # 执行：被阻止商品移到回收站（自动复核）
python wb.py banned --shops 5272 --apply --yes   # 只处理指定店
python wb.py clean --target all            # 预览
python wb.py clean --target all --apply    # 执行（先草稿后回收站；默认不自动同步/不合并，完成后仅提示）
python wb.py clean --target all --apply --sync  # 执行 + 清理前同步 + 清理后自动合并映射表
python wb.py orders                        # 同步+查询今天订单
python wb.py orders --begin 2026-08-17 --end 2026-08-20   # 指定日期区间
python wb.py orders --no-sync --days 3     # 跳过同步查缓存
python wb.py questions                     # 查询未处理提问（带中文名/标题/品牌/颜色/价格/描述/特征）
python wb.py questions --no-detail         # 跳过商品详情拉取（只中文名，快）
python wb.py questions --reply "..." --question-id <id>    # 回复单条
python wb.py questions-watch                # 前台AI：常驻监听，新提问打印到控制台与 data/logs/questions_front_*.log，前台手动回复（默认）
python wb.py questions-watch --once         # 前台AI跑一轮（展示，不提交）
python wb.py questions-watch --mode back --apply --interval 90   # 后台AI：常驻轮询 + LLM 自动提交（需配 ai.api_key）

# 运维
python wb.py cookies-update data/har/副号234_cookies.md
python wb.py daily morning                 # 手动跑一次"报名+改价"（等价于计划任务，无需计划任务）
python wb.py schedule                      # 【按需】创建 4 个计划任务（默认不创建，除非你要每日自动运行）
python wb.py schedule --remove             # 【按需】删除全部任务
```

## 四、merge 要点

- 审核文件是一次性输入：合并后固化进映射表，可归档。
- 消失即移除：vc 若 5 店任一都不在架 → 剔除并打印清单；某店数据缺失自动跳过（防误删）。
- 幂等：重复 merge 不翻倍。

## 五、ops 安全机制（必读）

1. **dry-run 默认**：不加 `--apply` 只打印清单。
2. **不可逆确认**：`trash` / 库存归零 需 `--yes`。
3. **价格下限校验**：目标价 ≤ 原价÷2 → 自动剔除并报告（先提价再分步调价可规避）。
4. **结果输出**：控制台 `✓/✗` + 汇总；明细追加写 `data/logs/ops_result.csv`（UTF-8-SIG）。
5. **0 值商品**：照常提交 + 执行后红色 `[0 值商品报告]`，复查仍 0 属 WB 官方原因。
6. **所有跳过显式提示**：`[跳过] 店X vc → 原因` + `[跳过汇总]`，杜绝隐式操作。
7. **replicate 防重复（四层）**：快照覆盖判断（vc 已在店 → 不进候选）→ 本地提交记录 `data/state/复制上架记录.json`（BCS 缓存滞后窗口内拦截，实测同步前 API 查不到刚提交的商品）→ 执行时 `vendorCodeMulti(filter=ALL)` 实时查重（同步后拦截）→ BCS 服务端 vendorCode 幂等（兜底）。上架后商品库存/价格以 BCS→WB 异步生效为准，明细 CSV 在 `data/logs/复制上架_*.csv`。
8. **replicate 成功判据 = vendorCode 出现**：上架后价格/库存等字段显示 None 属正常（WB 平台审核/回填延迟，短则数分钟、长则 1 小时+），**只要目标店能查到该 vendorCode 即上架成功**；核对价格库存数值需等 1 小时后再查。另：**多目标店当前必须逐店提交**——这是 BCS 平台 bug（2026-08-20 前后）：一次请求带多店返回 200 但静默不生效（4 店对照实验证实）。BCS 后续修正后可恢复多店一次提交提效，恢复前先小批量验证 vendorCode 确实创建。
9. **import-shelve 说明**：查重按 WB原始nmId（vc 末段数字，两代格式通吃）；新 vc 我方前缀优先（他人表中文名**精确命中**我商品价格表前缀码 → `BCS-{我的前缀}-{nmId}`，未命中沿用他人 vc）；防重复三层 = 我方快照 nm 集合 + 本地记录（`复制上架记录.json`，**nm 为键**）+ 执行时按新 vc 实时查重（新 vc 与他人不同时 BCS 服务端幂等失效，本地记录是必要防线）。仓库解析三层兜底：快照众数 → 仓库 API → `replicate.KNOWN_WAREHOUSES` 实测表。明细 CSV 在 `data/logs/导入上架_*.csv`。

## 六、Python 库调用（import 方式）

```python
import wb_ops.ops as ops
import wb_ops.mapping as mapping

# 映射表状态
state, excluded = mapping.load_mapping_state()   # {vc:{cn,dp}} + {vc:原因}
boss = mapping.load_boss()                       # 商品价格表商品列表

# 一键操作（两段式安全模式）
state, _, boss = ops.load_state()
shops = ops.get_shops()                          # [店id]
plans = ops.plan_price(['BCS-XXX-1'], shops, state, boss, manual=130, discount=20, club=None, keep_price=False)
ops.dry_run(plans, 'price')                      # 先预览
ops.run_apply(plans, 'price')                    # 执行（也可在 CLI 里 --apply）

# 促销/折扣/清理 模块都暴露 run(args) 入口，args 为 argparse.Namespace
```

> 每个模块内部函数签名在源码 docstring 中都有说明；CLI 是这些函数的薄封装。

## 七、失败排查速查

| 现象 | 原因 | 处理 |
|---|---|---|
| WB 接口 403 | cfidsw-wb 过期 | 重新抓包 → `wb.py cookies-update` |
| BCS 接口 401 | bcs.token 过期 | 更新 `data/credentials.json` 的 bcs.token |
| BCS 接口 405 | 缺 X-Limit-Key | 更新 bcs.limit_key |
| 改价查出 0 个但 WB 有高折扣 | BCS 缓存未同步 | 确认加了 `--sync`（或先 `wb.py fetch`），重跑 |
| 返回 200 但价格没变 | 触发价格下限 | ops 已拦截；确认是否手动提交过 |
| 删除失败"有库存" | 订单未完成 | 脚本已自动归零，订单完成后重跑 |
| `banned --apply` 部分失败 | 商品有库存/在途 | 先归零库存（`wb.py stock`）后重跑 `banned` |
| 报名"已存在跳过" | 活动已参加 | 正常幂等行为 |
