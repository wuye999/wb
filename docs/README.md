# wb_ops · 文档索引

> Wildberries / BCS 卖家自动化库 —— 把原「检查价格」+「促销折扣」两套脚本整合为一个 Python 包。
> ⚠ **店铺 ID/名称、电脑文件路径、Python 路径、登录账号都是原作者环境的**，换账号/店铺/电脑时需替换成你自己的（见下方「环境适配」）。

## ⚠ 环境适配（换账号 / 换电脑必读）

本仓库内出现的以下值均为**原作者环境示例**，换人/换店/换机后请替换：

| 内容 | 原作者示例 | 说明 |
|---|---|---|
| 店铺 ID / 名称 | 5272(主号7) / 5273(主号8) / 5276(副号2) / 5280(副号3) / 5281(副号4) | 换成你自己的店铺；`wb.py shops` 可实时查出 |
| Python 路径 | `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe` | 换成你的 venv Python；下文命令统一用 `python` 表示 |
| 仓库位置 | 任意目录（脚本按包位置自动识别，相对路径） | 把本目录放到你想放的位置即可 |

> 脚本代码**全部用相对路径**（`config.py` 自动定位 `data/`、`daily/schedule` 用 `sys.executable` 推导解释器），**换环境无需改代码**，只需替换 `data/credentials.json`（凭证）与 `data/商品价格表.xlsx`（商品清单）。

## 这份系统是什么

| 术语 | 含义 |
|---|---|
| **商品价格表** | 唯一权威商品清单（`data/商品价格表.xlsx`，7 列，你只维护它） |
| **价格映射表** | 唯一状态源（`data/价格映射表.xlsx`，8 Sheet，merge 自动重建，勿手改） |
| **vendorCode** | `BCS-{4位前缀码}-{WB原始nmId}`，跨店唯一键，映射表主键 |
| **credentials.json** | 统一凭证（BCS 三件套 + WB 各店 cookie） |

**两大业务线**（现合并在一个入口 `wb.py` 下）：
1. **商品映射 / 改价 / 库存 / 下架 / 价格审核 / 货不对板筛查**（原「检查价格」）：`shops / fetch / mapping / mapping-check / mismatch-check / review / merge / price / stock / trash / price-review`
2. **促销报名 / 折扣改价 / 清理 / 订单 / 买家提问 / 每日自动化**（原「促销折扣」）：`promo-apply / discount / clean / orders / questions / cookies-update / daily / schedule`

## 文档索引（按需读）

| 文档 | 读它解决什么问题 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 模块怎么分层、每个文件干什么、数据怎么流转、核心业务规则 |
| [CLI.md](CLI.md) | 每个子命令的参数、示例、退出码；Python 库调用方式 |
| [USAGE.md](USAGE.md) | 按情景分步操作（初始化/刷新凭证/上新/改价/促销/折扣/清理/每日自动） |
| [CREDENTIALS.md](CREDENTIALS.md) | 两套鉴权、凭证结构、如何刷新、失效表现、安全 |

## 🤖 给 AI 的硬性约束（必读）

> 本项目通过 **git 仓库统一维护与更新**。为让后续 `git pull` 更新不冲突，AI 必须遵守：

1. **禁止修改项目代码与文档文件**：`wb_ops/`（所有 .py）、`wb.py`、`docs/`、`README.md`、`.gitignore`、`requirements.txt`、`data/README.md` 等一律**只读**，不得编辑、删除、新增。
2. **写脚本 / 处理文件 → 一律放 `_scratch/` 目录**：AI 要写临时脚本、处理文件、生成中间产物或结果文件时，统一放在仓库根目录的 `_scratch/`（该目录已 gitignore，不进公开仓库，用完可自行清理）。**不要**把临时脚本写进 `wb_ops/`（核心库）、`data/`（数据区）、`docs/` 或仓库根目录。
3. **只允许**：① 运行命令（`python wb.py ...`）；② 读写 `data/` 下你自己的数据文件（`credentials.json`、`商品价格表.xlsx`、日志等）；③ 在 `_scratch/` 里写临时脚本处理文件；④ 阅读文档与代码。
4. 如确需改代码，请先征得作者同意，并让作者在源仓库改好后通过 `git pull` 下发，**不要在本地直接改代码**。

> 原因：代码一旦在本地被改动，就会和源仓库分叉，导致之后 `git pull` 报冲突、更新失败。

## 🤖 AI 助手 30 秒上手

1. **确认环境**：用 venv 的 `python` 跑（勿用系统 python）：
   ```
   python wb.py shops
   ```
   应返回你的店铺列表（本文档作者环境为 5 家：主号7/主号8/副号2/副号3/副号4，你的是你自己的）。
2. **看状态**：`data/logs/daily_*.log` 最近一次结果；`data/credentials.json` 是否各店三件套齐全。
3. **查一下就能动手的命令**（都在**仓库根目录**下执行）：
   ```bash
   python wb.py shops                       # 店铺列表
   python wb.py discount                     # 折扣改价 dry-run（>50%→50%）
   python wb.py promo-apply                 # 促销报名 dry-run
   python wb.py clean --target all          # 清理草稿/回收站 dry-run
   ```
   > ⚠ 写操作（改价/折扣/库存/下架/清理/上架）**默认不自动 BCS 同步、不自动写后验证、不自动合并映射表**，执行完成仅打印提示；需要同步在架商品并合并映射表时加 **`--sync`**（或单独 `wb.py fetch && wb.py merge`），因 WB/BCS 异步回填，当场验证不一定准确。
4. **任何异常**：先看 `data/logs/`，别重复盲跑；遇到 401/403 去读 [CREDENTIALS.md](CREDENTIALS.md)。

> 完整命令参考与成功/失败判据见 [CLI.md](CLI.md) 和 [USAGE.md](USAGE.md)。
