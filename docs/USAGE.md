# wb_ops · 日常情景使用流程（USAGE.md）

> 按情景给「确定命令 + 预期输出 + 成功/失败判据」。所有命令在**仓库根目录**下执行；`python` 指你的 venv Python（作者示例路径 `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，**换电脑请替换**，勿用系统 python）。
> ⚠ 文中店铺 ID（5272 等）、店铺名（主号7 等）均为**作者店铺示例**，换成你自己的（`wb.py shops` 可查）。

## 0. 环境（每次会话先确认）

```bash
python wb.py shops
```
- ✅ 成功：返回你的店铺列表（作者环境为 5 行：主号7/主号8/副号2/副号3/副号4）。
- ❌ 失败：报 401/405 → 去 [CREDENTIALS.md](CREDENTIALS.md) 更新凭证。

---

## 1. 一次性初始化

```bash
# 1) 确认凭证齐全（BCS 三件套 + WB 5 店三件套）
python wb.py shops          # BCS 通
python wb.py promo-apply    # WB 通（能拉出活动列表即正常，dry-run 无副作用）
```

> ⚠ **默认不建立任何计划任务**：除非你主动要求「每日自动运行」，否则**不要**运行 `wb.py schedule`，日常一律手动执行命令即可（见下文各情景）。

```bash
# 2) 【可选，仅当你明确需要"每日自动运行"时才执行】创建每日计划任务
python wb.py schedule       # 创建 4 个任务（9:00 morning / 11/15/19 check）
python wb.py schedule --remove   # 删除全部任务
```
- ✅ 若创建：`schtasks /Query` 能看到 WB_Daily_Morning 等 4 个任务，状态 OK。

## 2. 刷新鉴权凭证

```bash
# WB cookie 失效（403）：重新抓包 → 存 md → 更新
python wb.py cookies-update data/har/我的抓包.md

# BCS token 失效（401）：编辑 data/credentials.json 的 bcs.token / limit_key
```
- ✅ 成功：`cookies-update` 打印 `✓ 主号7 ... 已更新`；`wb.py shops` 恢复正常。
- 📌 cookie 保鲜实测 ≥46 小时，建议每周刷新一次。

## 3. 新商品上架 → 进映射表（免人工核对）

1. 商品价格表该商品行填「vendorCode前缀码」：**4 位大写、全局唯一**（如 充电宝-绿 → `CDBL`）。
2. 上架时 vendorCode 用 `BCS-{前缀}-{WB原始nmId}` 格式。
3. 之后跑：
```bash
python wb.py fetch          # 同步+拉取
python wb.py merge          # 前缀命中 → 自动补录进映射表
```
- ✅ 成功：merge 打印 `[前缀自动补录] N 个`，映射表新增对应 vc。

## 4. 同步 / 新增商品（含人工审核）

```bash
python wb.py fetch                         # 同步+拉取 5 店
python wb.py mapping                       # 统一核对工作台（一页两区）
# → 打开 data/workbench/价格映射核对工作台.html
#   上半区勾选商品价格表商品→候选；下半区给未归属 vc 选归属/排除
# → 点「导出核对结果 JSON」→ 保存为 统一审核.json
python wb.py merge 统一审核.json            # 增量合并 → 映射表
```
- ✅ 成功：merge 打印 `合并完成`，映射表更新。

## 5. 货不对板筛查（看图找「上架商品图片与中文名不符」的）

```bash
python wb.py mismatch-check             # 生成货不对板筛查工作台
# → 打开 data/workbench/货不对板筛查工作台.html
#   顶部「中文名筛选」可多选只看某几个中文名（Ctrl/Cmd 点选，不选=全部）
#   逐个商品看缩略图（点击弹高清大图）与俄文标题是否和中文名一致
#   点击标题（或左侧复选框）勾选「货不对板」（勾选状态浏览器本地保存，刷新不丢）
# → 点「导出勾选 vc」下载 货不对板vc.json，或「复制 vc」复制逗号分隔列表
python wb.py trash --vc <vc列表> --apply --yes   # 清空库存并移至回收站
```
- ✅ 成功：工作台「共 N 条 · M 个中文名商品」；导出 vc 后 trash 打印 `成功 N · 失败 0`。
- 📌 下架前先去 `--apply` 跑 dry-run 预览清单；`trash` 会自动先清库存再移回收站。

## 6. 改价 / 设折扣 / 库存 / 下架（两段式）

```bash
# 改价（默认 floor 双倍售价；--price 手动；--discount 带折扣）
python wb.py price --name 充电宝 --apply --yes
python wb.py price --vc BCS-XXX --price 130 --discount 20 --apply --yes
python wb.py price --vc BCS-XXX --discount 30 --keep-price --apply --yes   # 只改折扣不动价

# 库存
python wb.py stock --name 冲牙器 --amount 0 --apply --yes
python wb.py stock --name 冲牙器 --amount 999 --apply --yes

# 下架（不可逆，自动先清库存）
python wb.py trash --name 某商品 --apply --yes

# ⚠ 写后必做：改价/库存写 WB 侧，需再同步才在 BCS 可见
python wb.py fetch && python wb.py merge
```
- ✅ 成功：`结果: 成功 N · 失败 0`，明细在 `data/logs/ops_result.csv`。
- 📌 每个命令先去 `--apply` 跑 dry-run 预览清单。

## 6b. 跨店复制上架（replicate）——补齐只覆盖部分店铺的商品

场景：某商品只在 1-4 家店在架，其余店没有 → 自动把它上架到缺失的店铺（vendorCode 与源店一致、价格=源店现价、库存 999、直上）。

> ⚠ **自动同步**：命令启动时会**自动先同步+拉取全部店铺**（WB→BCS，约 2 分钟），保证覆盖判断基于最新数据，无需手动 fetch。加 `--no-sync` 可跳过（用本地快照，覆盖判断可能滞后，不推荐）。

```bash
python wb.py replicate                    # 自动同步后预览全部部分覆盖商品（vc/中文名/源店/价格/目标店）
python wb.py replicate --prefix ZLTH --apply   # 按前缀码批量补齐
python wb.py replicate --name 冲牙器 --apply    # 按映射表中文名补齐
python wb.py replicate --vc BCS-XXX-123 --apply # 单个 vc（目标店自动=缺失店）
python wb.py replicate --shops 5273 --apply     # 只补指定店
python wb.py replicate --no-sync          # 跳过自动同步（用本地快照，不推荐）
```
- ✅ 成功判据：**目标店能查到该 vendorCode 即成功**；上架后价格/库存显示 None 属正常（WB 平台审核/回填延迟，等 1 小时甚至更久再核对数值）。
- 📌 防重复四层（快照覆盖 → 本地记录 → 实时查重 → BCS 服务端幂等），重复执行安全，已存在的店自动跳过。
- 📌 多目标店自动逐店提交（BCS 平台 bug：多店一次提交返回 200 但静默不生效）。
- 明细：`data/logs/复制上架_*.csv`；本地提交记录 `data/state/复制上架记录.json`。
- 📌 上架后跑 `python wb.py fetch && python wb.py merge`，新店商品自动进映射表（vc 已知，店铺覆盖矩阵自动更新）。

## 6c. 他人映射表导入上架（import-shelve）——别人有、我没有的商品上架到我的店

场景：拿到他人（同项目格式）映射表 → 按 **WB原始nmId**（vendorCode 末段数字，两代格式 `BCS-{随机4位}-{nm}` / `BCS-{前缀码}-{nm}` 通吃）比对，他人有、我方 5 店全无的商品上架到我的店铺。

> ⚠ **自动同步**：命令启动时会**自动先同步+拉取全部店铺**（WB→BCS，约 2 分钟），保证差集判断基于最新数据（漏判会重复上架他人已有的商品），无需手动 fetch。加 `--no-sync` 可跳过（不推荐）。

```bash
python wb.py import-shelve 他人映射表.xlsx         # 自动同步后预览差集清单（含前缀来源标注）
python wb.py import-shelve 他人映射表.xlsx --cn 冲牙器   # 按他人表中文名过滤
python wb.py import-shelve 他人映射表.xlsx --shops 5273 --apply   # 上架到指定店
python wb.py import-shelve 他人映射表.xlsx --apply              # 全量上架到全部店
python wb.py import-shelve 他人映射表.xlsx --no-sync            # 跳过自动同步（不推荐）
```
- ✅ 成功判据：同 6b——目标店查到新 vendorCode 即成功（数值回填延迟同上）。
- 📌 **新 vendorCode 规则**：他人表中文名**精确命中**我商品价格表前缀码 → `BCS-{我的前缀}-{nmId}`（预览清单标注"我方"）；未命中 → 沿用他人 vc（标注"他人"）。
- 📌 **价格** = `floor(他人表双倍售价)`（我方标准定价规则）；双倍售价缺失的商品 dry-run 显示 `?`、执行时跳过。
- 📌 防重复三层（我方快照 nm 集合 → 本地记录（nm 为键）→ 按新 vc 实时查重），重复执行安全。
- 明细：`data/logs/导入上架_*.csv`。
- ⚠ 他人表须为同项目生成的映射表格式（含「映射总表」Sheet）；中文名命中我价格表 → 上架后 merge 可前缀自动归属，未命中的沿用他人 vc（后续按普通新商品走核对流程）。

## 7. 促销报名（每日 9:00 自动）

```bash
python wb.py promo-apply              # 预览可报名活动
python wb.py promo-apply --apply      # 执行（5 店全量报名）
python wb.py promo-apply --shops 5272 --apply   # 只报主号7
```
- ✅ 成功：`[汇总] 可报名 N | 成功 M`；幂等（重复报名返回"已存在跳过"）。
- 明细：`data/logs/报名结果_*.csv`。
- ⚠ **报名后必跑价格审核**：参加活动会同步改折扣，改折扣降幅落 30-49.9% 的商品进 WB 隔离区（见第 10 节）→ 报名后执行 `python wb.py price-review`（dry-run 预览 → `--apply`），确保折扣生效。

## 8. 折扣检查（>50% → 50%）

```bash
python wb.py discount                # 预览 >50% 商品
python wb.py discount --apply        # 执行
python wb.py discount --threshold 47 --target 46 --apply   # 自定义
```
- ✅ 成功：`[汇总] 共 N 条待改`；日志 `data/logs/折扣修改_*.csv`。
- ⚠ 默认先同步 BCS 缓存（~50s），**不同步会漏查**（实测教训），勿用 `--no-sync`。
- ⚠ **改折扣也会触发价格审核**：`discount --apply` 之后**必跑 `python wb.py price-review`**（先 dry-run 预览、有货再 `--apply`）——降幅落 30-49.9% 区间的商品会进 WB 隔离区，不「应用新价格」则新价不生效（详见第 10 节）。

## 9. 清理草稿箱 / 回收站

```bash
python wb.py clean --target all            # 预览
python wb.py clean --target all --apply    # 执行：先草稿箱，后回收站
```
- 回收站规则：**一键清空**（`deleteAllSize`）——先对回收站里有库存的商品归零，再一键清空整店回收站；草稿箱按 UUID 逐条删除。
- ⚠ **`deleteAllSize` 对有库存商品会失败**（返回 `error:true` + `StockCount>0`，报 `content.api.errors.source.whileDeleting`），需等库存清零后再重试。
- ✅ 成功：`[汇总-回收站] 共 N | 已删除 M | 已归零 Z`。

## 10. 价格审核（降价 / 改折扣 30-49.9% 进审查的商品）

> ⚠ **触发源不止改价，改折扣（discount）同样触发**（实测 2026-08-20）：WB 价格审查按「**新价相对原价降幅**」判定，折扣降幅落入 30–49.9% 区间的商品也会进隔离区，必须「应用新价格」才生效。

```bash
python wb.py price-review              # 预览隔离区待审商品
python wb.py price-review --apply      # 应用新价格（审核通过）
python wb.py price-review --shops 5272 --apply   # 只审主号7
```
- 背景：改价**或改折扣**使新价较原价下降 **30–49.9%** 会进入 WB 价格审查（隔离区），**必须「应用新价格」才生效**；降价 **>50%** 会被 WB 直接拒绝。
- ✅ 成功：`[汇总] 待审 N | 已应用新价格 M`；日志 `data/logs/价格审核_*.csv`。
- 改价时自动审核：`python wb.py price --name 充电宝 --apply --yes --auto-review`（改价成功后自动匹配降价 30-49.9% 的商品并应用新价格，不误审历史遗留待审商品）。
- ⚠ **日常必备动作**：每次 `promo-apply`（报名）或 `discount --apply`（改折扣）**之后，必跑一次 `price-review`**（先 dry-run 预览、有货再 `--apply`），否则本次降折扣触发的隔离区商品价格不生效。

## 11. 订单查询

```bash
python wb.py orders                                   # 同步+查询今天订单
python wb.py orders --begin 2026-08-17 --end 2026-08-20   # 指定日期区间
python wb.py orders --days 7                          # 最近 7 天
python wb.py orders --no-sync                         # 跳过同步，直接查缓存
```
- 流程：自动触发订单同步（`shopIds:[]`=全部店铺，异步任务约数秒）→ 轮询完成 → 查询订单列表 + 状态分类计数。
- ✅ 成功：`[汇总] 订单 N 条`；日志 `data/logs/订单查询_*.csv`。
- `--shops` 可限定同步范围与结果；`--page-size` 控制分页（默认 50）。

## 12. 买家提问查询 + AI 一键回复

```bash
python wb.py questions                              # 查询未处理提问
python wb.py questions --reply "回复内容" --question-id <id>   # 回复单条
python wb.py questions --reply "回复内容" --reply-all --yes    # 回复全部（危险）
```
- ✅ 成功：`[汇总] 未处理提问 N`；日志 `data/logs/买家提问_*.csv`。

**🤖 AI 一键查询新提问并回复的流程**（拿给 AI 就能照做）：
1. `python wb.py questions` → 列出各店未处理提问（含提问ID、商品、买家问题、回复截止时间）。
2. 逐条阅读买家问题（俄语），理解买家在问什么。
3. 为每条生成俄语回复（礼貌、简洁、针对问题作答；不确定的不要乱答，涉及物流/售后的可引导联系客服）。
4. 逐条回复：`python wb.py questions --reply "<俄语回复>" --question-id <提问ID>`（一条一条回，回完核对 `已回复`）。
5. 复核：再跑一次 `python wb.py questions`，确认 `未处理提问 0`。
- ⚠ 回复是**对买家公开发言**，务必措辞得当；批量自动回复建议先人工抽查 1-2 条再放开。

## 13. 每日自动运行（仅当已主动创建计划任务）

> ⚠ **默认不创建计划任务**。本节仅在你自己主动运行过 `wb.py schedule`（见第 1 节）之后才生效；未创建则一切手动执行。

| 时间 | 自动动作 |
|---|---|
| 09:00 | `wb.py daily morning` = 报名 + 改价（含价格审核） |
| 11/15/19 点 | `wb.py daily check` = 只改价（含价格审核） |

> ⚠ **价格审核是改折扣后的必备环节**：无论计划任务还是手动跑 `daily morning/check`，`discount --apply` 之后都应补跑一次 `python wb.py price-review --apply`（隔离区商品不「应用新价格」则新折扣不生效，见第 10 节）。

- 查看结果：`data/logs/daily_YYYYMMDD.log`（主日志）+ 各 CSV 明细。
- 手动触发（等价于计划任务）：`python wb.py daily morning` 或 `python wb.py daily check`。
- 已建任务时手动触发：`schtasks /Run /TN WB_Daily_Morning`。

## 14. 维护原则

- **只维护**：`data/商品价格表.xlsx`（映射表由 merge 自动更新）。
- **不要手动改**：`data/价格映射表.xlsx`（永远用 merge 重建）。
- **审核文件用完即弃**：合并后归档。
- **fetch 必须完整**：某店拉取失败时 merge 会跳过「消失即移除」，先补拉再 merge。
- **加列/改结构用 Excel**，避免 openpyxl 重存破坏商品价格表公式。

## 15. AI 执行手册（拿给 AI 就能照做）

> 若你是 AI 被要求操作本系统，按下面顺序执行，每步验证输出再继续：

1. `wb.py shops` → 5 店正常。
2. `ls data/logs/daily_*.log` → 看最近一次结果。
3. 要做什么就查什么（改价→`price`、折扣→`discount`、报名→`promo-apply`、清理→`clean`），**一律先 dry-run**（不加 `--apply`），确认清单再 `--apply`。
4. 写操作后 `wb.py fetch && wb.py merge` 验证。
5. 遇 401/403 → 读 [CREDENTIALS.md](CREDENTIALS.md)；遇其他异常 → 看 `data/logs/`，不重复盲跑。
