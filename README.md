# SellerOps 卖家运营工具

Wildberries、BCS、马帮、飞书和 AI 客服的命令行运营工具。需要 Python 3.10+，基础依赖为 requests 和 openpyxl。

## 用中文交代运营任务

电商运营可以用中文向 WorkBuddy 交代任务，例如“生成 A 店近 14 天的商品运营报告”，也可以使用其他支持调用本地命令行的 AI 客户端。零代码上手步骤和日常话术见 [用户使用手册](user-guide/README.md)。

## 从这里开始

| 读者或任务 | 阅读入口 |
| --- | --- |
| 电商运营与团队管理 | [用户使用手册](user-guide/README.md)，普通运营唯一日常入口，涵盖零代码上手和日常话术 |
| 让 AI 协助操作 | [项目约束](AGENTS.md)、[AI 操作指南](docs/AI_GUIDE.md) |
| 命令行用法与参数参考 | [按业务模块查命令](docs/USAGE.md)、[完整参数参考](docs/CLI.md) |
| 安装与环境配置 | [安装并启动](#安装并启动)、[准备自己的数据](#准备自己的数据)、[配置指南](docs/CONFIGURATION.md) |
| 连接或更新平台凭证 | [WorkBuddy 平台连接](docs/CONFIGURATION.md#通过-workbuddy-连接平台) |
| 数据准备与备份 | [数据契约](docs/DATA.md) |
| 开发与维护 | [开发指南](docs/DEVELOPMENT.md)，包含架构、代码入口、复用能力和开发准则 |
| 商品运营报告 | [报告说明](docs/PRODUCT_REPORT.md)：退货、差评、近7/14天出单和店铺链接缺口 |
| WB Open API 商品写入 | [上架、内容更新与 VC 规范化](docs/OPEN_API_LISTINGS.md)：两种商品来源、有限标识等待、断点续办、相同 VC 跨店更新、按明确归属规范历史 VC 前缀；BCS 默认接口保留 |
| 平台接口与抓包参考 | [开发指南](docs/DEVELOPMENT.md#平台接口资料)；维护者私有资料保存在根目录 `api/`，不随 Git 分发 |
| 启动或业务报错 | [故障排查](docs/TROUBLESHOOTING.md) |

## 安装并启动

在项目根目录创建独立环境，始终使用同一个解释器安装和运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe run.py --help
```

下文的 `python` 应替换为实际使用的解释器，或先激活环境。也可以使用已装基础依赖的 Python 执行 `python run.py --help`。

在 WorkBuddy 里说「连接 WB」「连接 BCS」或「连接马帮」即可进入对应的 `login` 流程。BCS、马帮优先使用本机账号密码，平台要求验证时在官网操作；WB 从专用浏览器恢复会话。首次安装和登录步骤见 [平台连接配置](docs/CONFIGURATION.md#通过-workbuddy-连接平台)。飞书功能另需已登录的配套 lark-cli。

## 更新已有环境

从 1.0 升级时先看 [旧命令迁移对照及回退步骤](docs/MIGRATION.md)。

已有用户可沿用原来的 `data/`、`.venv` 和有效的平台会话。更新后请按下文核对仓库和交运渠道配置。更新前先停止本项目的运行进程，在项目根目录执行：

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe run.py --help
```

以上安装命令会按 `pyproject.toml` 检查基础依赖，并安装 `sellerops` 包入口；不会读取或修改生产数据。原环境若已满足 Python 3.10+、requests 与 openpyxl 的版本要求，也可直接用该环境执行 `python run.py ...`。

当前源码入口是 `run.py`，旧的 `python wb.py ...` 调用需改为 `python run.py ...`。原脚本、快捷方式及计划任务若引用旧入口或目录，应同步更新；安装包后也可使用 `python -m sellerops ...`。持续运行的任务在更新后按原授权范围手动恢复，更新代码不会自动启动任务、同步平台或执行业务写入。

Git 只分发源码、说明和空值样例，自己的凭证、商业表格、快照、状态、日志、运行环境及本地归档不上传。更新代码保留这些用户数据；飞书 CLI 路径、登录状态及平台凭证继续由各用户自己的环境维护。

## 准备自己的数据

数据放在与 `run.py` 同级的 `data/`。已有数据时可将自己的整个 data 文件夹放入；首次配置按 [配置样例](examples/credentials.example.json) 和 [数据契约](docs/DATA.md) 准备凭证及表格：

```text
wb/
  run.py
  src/
  data/
    credentials.json
    商品价格表.xlsx
    sellerops.db
    state/
```

首次配置时，在 `data/credentials.json` 中填写 `wb.warehouse_keyword`（例如「莫斯科」或具体仓库全名）和 `mabang.handover_keyword`（例如「七库海外仓」）。请根据本店仓库列表和马帮账户的实际渠道名称填写。

已有旧快照或映射表的用户，先按 [旧数据迁移说明](docs/MIGRATION.md#迁移旧商品数据) 将商品资料搬入 SQLite。样例凭证为空，不能直接连接平台。基础业务先准备所需平台凭证。真实凭证、浏览器资料和业务数据由 Git 排除，更新代码时保留自己的 data，并单独备份。

在商品价格表的「发货方式」列填写「马帮发货」「WB发货」或「不处理」。「马帮发货」须填写「马帮库存SKU」，其余两种方式让该列留空。详见 [商品价格表](docs/DATA.md#商品价格表)。

默认 data 路径由项目位置确定，不随终端工作目录变化。只有需要单独存放数据时才使用 `--data-dir` 或 `SELLEROPS_DATA_DIR`。优先级为命令行、环境变量、项目根目录 data。`--data-dir` 可放在子命令前后。

```powershell
python run.py --data-dir "D:\SellerData" shop list
```

要同时处理两份运营数据，可在两个终端分别启动命令，各用自己的 `--data-dir`。凭证和数据库跟随各自的数据目录。

## 检查入口并开始使用

以下命令显示帮助或预览，不访问平台、不注册任务：

```powershell
python run.py ops price --help
python run.py task run morning --all-shops
python run.py task schedule
```

`doctor` 检查本机环境与配置状态，只有加 `--check-online` 才联网探测会话。`shop list` 查询本地库中的店铺和快照状态。

```powershell
python run.py doctor --json
python run.py shop list --json
```

已有价格表和快照时，按自己的店铺和商品生成操作预览。

```powershell
python run.py ops price --shops 1001 --name "示例商品" --price 55 --json
```

店铺 ID、商品名和计划单号均为占位示例。核对范围、数量和价格，经运营确认后，用本次预览的单号提交。

```powershell
python run.py apply --plan P018
```

用户要求更新快照时单独运行 `sync`。默认通过 BCS 先同步 WB → BCS 再读缓存，已在后台同步时可加 `--no-sync`。显式 `--source wb-openapi` 直读 WB 中国区接口。

## 日常使用边界

- 商品计划类操作先预览，经运营确认后提交，执行结果以本次回执为准。写入、续办和汇报细则见 [AI 操作指南](docs/AI_GUIDE.md)。
- 涉及 WB 仓库时只使用本店已确认的默认业务仓。配置与核对方法见 [仓库范围](docs/CONFIGURATION.md#仓库范围仅允许配置的本店默认业务仓)。
- 日常运营使用自己的 `data/`，临时工作放 `_scratch/`，项目文件保持只读。维护者授权的开发工作见 [开发指南](docs/DEVELOPMENT.md)。

## 开发检查

```powershell
python scripts/check.py
python scripts/check_docs.py
```

离线检查验证源码、导入、依赖边界、命令入口和业务行为，不连接平台或读取真实凭证。改动 CLI 后先运行 `python scripts/generate_cli_docs.py` 更新 [命令参考](docs/CLI.md)。代码结构与扩展步骤见 [开发指南](docs/DEVELOPMENT.md)。
