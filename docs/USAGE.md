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

## 5. 改价 / 设折扣 / 库存 / 下架（两段式）

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

## 6. 促销报名（每日 9:00 自动）

```bash
python wb.py promo-apply              # 预览可报名活动
python wb.py promo-apply --apply      # 执行（5 店全量报名）
python wb.py promo-apply --shops 5272 --apply   # 只报主号7
```
- ✅ 成功：`[汇总] 可报名 N | 成功 M`；幂等（重复报名返回"已存在跳过"）。
- 明细：`data/logs/报名结果_*.csv`。

## 7. 折扣检查（>50% → 50%）

```bash
python wb.py discount                # 预览 >50% 商品
python wb.py discount --apply        # 执行
python wb.py discount --threshold 47 --target 46 --apply   # 自定义
```
- ✅ 成功：`[汇总] 共 N 条待改`；日志 `data/logs/折扣修改_*.csv`。
- ⚠ 默认先同步 BCS 缓存（~50s），**不同步会漏查**（实测教训），勿用 `--no-sync`。

## 8. 清理草稿箱 / 回收站

```bash
python wb.py clean --target all            # 预览
python wb.py clean --target all --apply    # 执行：先草稿箱，后回收站
```
- 回收站规则：能删先删；删除失败（有未完成订单占库存）→ 只归零库存，不重删。
- ✅ 成功：`[汇总-回收站] 共 N | 已删除 M | 失败 F | 已归零 Z`。

## 9. 每日自动运行（仅当已主动创建计划任务）

> ⚠ **默认不创建计划任务**。本节仅在你自己主动运行过 `wb.py schedule`（见第 1 节）之后才生效；未创建则一切手动执行。

| 时间 | 自动动作 |
|---|---|
| 09:00 | `wb.py daily morning` = 报名 + 改价 |
| 11/15/19 点 | `wb.py daily check` = 只改价 |

- 查看结果：`data/logs/daily_YYYYMMDD.log`（主日志）+ 各 CSV 明细。
- 手动触发（等价于计划任务）：`python wb.py daily morning` 或 `python wb.py daily check`。
- 已建任务时手动触发：`schtasks /Run /TN WB_Daily_Morning`。

## 10. 维护原则

- **只维护**：`data/商品价格表.xlsx`（映射表由 merge 自动更新）。
- **不要手动改**：`data/价格映射表.xlsx`（永远用 merge 重建）。
- **审核文件用完即弃**：合并后归档。
- **fetch 必须完整**：某店拉取失败时 merge 会跳过「消失即移除」，先补拉再 merge。
- **加列/改结构用 Excel**，避免 openpyxl 重存破坏商品价格表公式。

## 11. AI 执行手册（拿给 AI 就能照做）

> 若你是 AI 被要求操作本系统，按下面顺序执行，每步验证输出再继续：

1. `wb.py shops` → 5 店正常。
2. `ls data/logs/daily_*.log` → 看最近一次结果。
3. 要做什么就查什么（改价→`price`、折扣→`discount`、报名→`promo-apply`、清理→`clean`），**一律先 dry-run**（不加 `--apply`），确认清单再 `--apply`。
4. 写操作后 `wb.py fetch && wb.py merge` 验证。
5. 遇 401/403 → 读 [CREDENTIALS.md](CREDENTIALS.md)；遇其他异常 → 看 `data/logs/`，不重复盲跑。
