# Wildberries / BCS 卖家自动化（wb_ops）

统一管理多店铺（本仓库默认 5 家：主号7/主号8/副号2/副号3/副号4）的商品**映射、改价、库存、下架、促销报名、折扣改价、清理**与**每日自动运营**。

## ⚠ 环境适配（换账号 / 换电脑必读）

本仓库里的以下内容是**原作者环境**的，换账号、换店铺、换电脑后**需替换成你自己的**（脚本代码无需改，只需换数据文件与凭证）：

| 项目 | 原作者环境示例 | 你需要改成 |
|---|---|---|
| 店铺 ID / 名称 | 5272(主号7)、5273(主号8)、5276(副号2)、5280(副号3)、5281(副号4) | 你自己的店铺 ID / 名称 |
| Python 路径 | `C:\Users\madokka\.workbuddy\binaries\python\envs\default\Scripts\python.exe` | 你自己的 venv Python 路径 |
| 仓库位置 | 任意目录（本仓库根目录下即可） | 你自己的目录（相对路径，脚本自动识别） |

> 脚本内部**已全部使用相对路径**：`config.py` 按包所在位置自动定位 `data/`，`daily.py`/`schedule.py` 用 `sys.executable` 动态推导 Python 解释器路径，**不依赖任何绝对路径**。你只需准备 `data/credentials.json`（凭证）和 `data/商品价格表.xlsx`（商品清单）。

## 🤖 给 AI 的硬性约束（必读）

本项目通过 **git 仓库统一维护与更新**。为让后续 `git pull` 更新不冲突，AI 必须遵守：

- **禁止修改项目代码与文档文件**：`wb_ops/`、`wb.py`、`docs/`、`README.md`、`.gitignore`、`requirements.txt`、`data/README.md` 一律**只读**。
- **写脚本 / 处理文件 → 一律放 `_scratch/` 目录**：AI 要写临时脚本、处理文件、生成中间产物或结果文件时，统一放在仓库根目录的 `_scratch/`（该目录已 gitignore，不进公开仓库，用完可自行清理）。**不要**把临时脚本写进 `wb_ops/`（核心库）、`data/`（数据区）、`docs/` 或仓库根目录。
- **只允许**：运行命令（`python wb.py ...`）、读写 `data/` 下你自己的数据、在 `_scratch/` 里写临时脚本处理文件、阅读文档与代码。
- 确需改代码时，请先征得作者同意，由作者改好后通过 `git pull` 下发，**不要在本地直接改代码**（否则会分叉、`git pull` 冲突）。

## 快速开始

```bash
# 用你自己的 venv Python 执行（勿用系统 python）；下文统一用 `python` 表示它
python wb.py shops                      # 打印你的店铺列表（验证鉴权）

# 常用
python wb.py fetch                      # 同步+拉取商品快照
python wb.py mapping                    # 核对工作台
python wb.py merge                      # 增量合并映射表
python wb.py price --name 充电宝 --apply --yes
python wb.py discount --apply           # >50% → 50%
python wb.py promo-apply --apply        # 促销报名
python wb.py clean --target all --apply
python wb.py price-review --apply       # 价格审核：应用新价格
python wb.py orders                     # 订单查询（同步+查询今天）
python wb.py questions                  # 买家未处理提问查询
```

## 文档

| 文档                                           | 内容                     |
| -------------------------------------------- | ---------------------- |
| [docs/README.md](docs/README.md)             | 文档索引 + AI 上手           |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构 / 模块职责 / 业务规则 / 数据流 |
| [docs/CLI.md](docs/CLI.md)                   | 命令参考 + Python 库调用      |
| [docs/USAGE.md](docs/USAGE.md)               | 日常情景使用流程               |
| [docs/CREDENTIALS.md](docs/CREDENTIALS.md)   | 鉴权与凭证                  |

## 目录

- `wb_ops/` 核心库 ｜ `wb.py` 统一入口 ｜ `data/` 数据与凭证（本地专属，不进 git） ｜ `docs/` 文档 ｜ `_scratch/` AI 临时工作区 ｜ `api/`、`_archive/` 本地参考（含账号信息，不随公开仓库分发）
