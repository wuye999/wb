# Wildberries / BCS 卖家自动化（wb_ops）

统一管理多店铺（店铺数量与 ID 由 data/credentials.json 决定，作者当前环境 3 家：袁州1/2/3）的商品**映射、改价、库存、下架、促销报名、折扣改价、清理**与**每日自动运营**。

## ⚠ 环境适配（换账号 / 换电脑必读）

本仓库里的以下内容是**原作者环境**的，换账号、换店铺、换电脑后**需替换成你自己的**（脚本代码无需改，只需换数据文件与凭证）：

| 项目 | 原作者环境示例 | 你需要改成 |
|---|---|---|
| 店铺 ID / 名称 | 作者环境示例：9352(袁州1)、9353(袁州2)、9356(袁州3) | 你自己的店铺 ID / 名称（wb.py shops 可查） |
| Python 路径 | `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe` | 你自己的 venv Python 路径 |
| 仓库位置 | 任意目录（本仓库根目录下即可） | 你自己的目录（相对路径，脚本自动识别） |

> 脚本内部**已全部使用相对路径**：`config.py` 按包所在位置自动定位 `data/`，`daily.py`/`schedule.py` 用 `sys.executable` 动态推导 Python 解释器路径，**不依赖任何绝对路径**。你只需准备 `data/credentials.json`（凭证）和 `data/商品价格表.xlsx`（商品清单）。

## 🤖 给 AI 的硬性约束（必读）

本项目通过 **git 仓库统一维护与更新**。为让后续 `git pull` 更新不冲突，AI 必须遵守：

- **禁止修改项目代码与文档文件**：`wb_ops/`、`wb.py`、`docs/`、`README.md`、`.gitignore`、`requirements.txt`、`data/README.md` 一律**只读**。
- **写脚本 / 处理文件 → 一律放 `_scratch/` 目录**：AI 要写临时脚本、处理文件、生成中间产物或结果文件时，统一放在仓库根目录的 `_scratch/`（该目录已 gitignore，不进公开仓库，用完可自行清理）。**不要**把临时脚本写进 `wb_ops/`（核心库）、`data/`（数据区）、`docs/` 或仓库根目录。
- **只允许**：运行命令（`python wb.py ...`）、读写 `data/` 下你自己的数据、在 `_scratch/` 里写临时脚本处理文件、阅读文档与代码。
- **严禁在写操作后擅自执行同步、合并或写后验证**：在执行改价（`price`）、改库存（`stock`）、改折扣（`discount`）、下架（`trash`）、清理（`clean`）、改尺寸（`dimension`）等写操作后，**严禁顺手执行 `fetch`、`merge` 或自写脚本查询接口进行写后验证**！
  - **核心原因**：写后验证必须依赖 BCS 全量同步，若不同步拉取的快照只是未修改前的旧数据；而触发 BCS 全量同步耗时长（~40-50s/店），频繁全量同步极易导致平台风控限流。平台侧写操作提交成功即生效。
  - **规范行为**：所有写操作执行完毕后立即结束。绝对不要自行补跑 `fetch` 或 `merge`，除非用户在指令中明确提出要同步/合并。
- **严禁执行全量测试，只测新增或修改的功能**：新增或者修改功能时，**无需进行全量测试**，只需要测新增或者修改的功能（使用 `python tests/run_tests.py --cmd <命令>` 或 `python tests/run_tests.py`）。全量测试会真连平台并耗时约 5 分钟，日常开发与修改功能严禁触发全量测试。
- 确需改代码时，请先征得作者同意，由作者改好后通过 `git pull` 下发，**不要在本地直接改代码**（否则会分叉、`git pull` 冲突）。

## 快速开始

```bash
# 用你自己的 venv Python 执行（勿用系统 python）；下文统一用 `python` 表示它
python wb.py shops                      # 打印你的店铺列表（验证鉴权）

# 常用
python wb.py fetch                      # 同步+拉取商品快照（仅当需要 BCS 缓存反映最新结果时才跑，日常写操作默认不用）
python wb.py mapping                    # 核对工作台
python wb.py merge                      # 增量合并映射表（同步各店单表并聚合总表；拿到新审核/新上架需入库时跑）
python wb.py shops-mapping              # 刷新各店铺独立映射表（data/shops/shop_*.xlsx）
python wb.py mapping-rename --vc BCS-XXX-123 --cn "新中文名" # 货不对板纠偏/改名：全店单表与总表一键同步
python wb.py price --name 充电宝 --apply --yes
python wb.py dimension                        # 按价格表「尺寸」批量改全部店铺商品尺寸（dry-run 默认；也可 --dims "长*宽*高/毛重" 自定义，须配 --vc/--name/--prefix 圈定）
python wb.py discount --apply           # >50% → 50%（BCS 批量；默认不自动同步/不写后验证，仅提示；加 --sync 自动同步并合并映射表）
python wb.py discount-wb --apply        # WB 原生批量改折扣（从高到低查询 >50% 并原生批量修改；默认不写后验证）
python wb.py promo-apply --apply        # 促销报名
python wb.py promo-goods                 # 只读：广告推广中被推广的商品（WB商品码/供应商代码/中文名；明细 CSV）
python wb.py price-review --apply       # ⚠ 报名/改折扣后必跑：应用新价格（改折扣也会触发价格审核）
python wb.py dims-check --name 视黄醇面霜  # 只读：列出尺寸偏差待验证商品（--type weight/all 可看重量/合并）
python wb.py clean --target all --apply # 清理（默认不自动同步，仅提示；加 --sync 自动同步并合并映射表）
python wb.py replicate                  # 跨店复制上架（部分覆盖→补齐缺失店铺）
python wb.py import-shelve 他人表.xlsx  # 他人映射表导入上架（他人有我方无）
python wb.py shelve <nmId> --price 59   # 新版批量上架（输入 WB 商品码直接上架）
python wb.py shelve-old <nmId> --price 59 # 旧版上品建卡（支持自定义完整 VC）
python wb.py price-review --apply       # 价格审核：应用新价格
python wb.py orders                     # 订单查询（同步+查询今天）
# ▸ 写操作（改价/库存/下架/折扣/清理/上架/改尺寸）默认都不同步、不写后验证、不合并映射表；执行完成即结束，严禁擅自补跑 fetch+merge，仅在极低频全局盘点或用户显式要求时才跑。
python wb.py questions                  # 买家未处理提问查询
python wb.py appeals --days 5           # WB 平台投诉单：未处理(等待回复) + 剩余天数=5 → 明细 + 去重商品编号行 + CSV
python wb.py feishu-vc-stats             # 只读：飞书「订单登记」近7天按供应商代码(BCS编号)统计单数降序（跨店合并；--days/--date/--begin/--end/--shops/--by-prefix）

# ▸ 每日新订单处理（2026-09-10 拆分为两个独立脚本；先 A 后 B 保证库存SKU 正确）
python wb.py mabang-process --apply                     # A 马帮处理一体：匹配商品→预报单→上传（自动发货）→物流交运（零飞书依赖）
python wb.py feishu-register                            # B 飞书登记：orderalllist 最近500条 + 待处理订单（两路合并）去重只登新增；只匹配未进预报/上传/交运的单也登记，库存SKU 取本地价格表（查不到留空）
python wb.py feishu-register --scope all --date 2026-09-05 --apply   # 补录历史订单（指定日期/区间）
python wb.py mabang-forecast --check                    # 上传 5-10 分钟后查预报结果
python wb.py mabang-stock-daily --apply                 # 「马帮库存登记表」日期列管理：默认只建今天列+更新全部已有日期列+总新增订单量（--begin/--date 显式时删旧列，--end 需同用）
python wb.py mabang-stock-register --apply              # 全量重建「马帮库存登记表」（马帮全部库存SKU 库存/状态/附件列「图」；会清空各日列与总列）
```

## 文档

| 文档                                           | 内容                     |
| -------------------------------------------- | ---------------------- |
| [docs/README.md](docs/README.md)             | 文档索引 + AI 上手           |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构 / 模块职责 / 解耦实践 / 业务规则 / 数据流 |
| [docs/DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md) | ★ 开发要求与代码格式规范（分层依赖/拆分标准/扩展流程/按需测试门禁） |
| [docs/REUSE_GUIDE.md](docs/REUSE_GUIDE.md)   | ★ 开发复用指南：能做 X 用哪个模块/函数 + 代码模板（写新功能前先读） |
| [docs/CLI.md](docs/CLI.md)                   | 44 个命令全集参考 + Python 库调用 |
| [docs/USAGE.md](docs/USAGE.md)               | 日常情景使用流程               |
| [docs/CREDENTIALS.md](docs/CREDENTIALS.md)   | 鉴权与凭证                  |

## 目录

- `wb_ops/` 核心库（5 层分层整洁架构） ｜ `wb.py` 统一入口 ｜ `tests/` 自动化测试套件（`test_all_commands.py` 全量 + `run_tests.py` 按需选择器） ｜ `data/` 数据与凭证（本地专属，不进 git） ｜ `docs/` 文档 ｜ `_scratch/` AI 临时工作区 ｜ `api/`、`_archive/` 本地参考（含账号信息，不随公开仓库分发）
