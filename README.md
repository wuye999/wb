# SellerOps 卖家运营工具

Wildberries、BCS、马帮、飞书和 AI 客服的命令行运营工具。需要 Python 3.10+，基础依赖为 requests 和 openpyxl。

## 从这里开始

| 读者或任务 | 阅读入口 |
| --- | --- |
| 第一次使用 | 本页安装与数据准备 → [配置](docs/CONFIGURATION.md) → [操作流程](docs/USAGE.md) |
| 日常查询与运营 | [操作流程](docs/USAGE.md)、[命令参考](docs/CLI.md) |
| 数据准备与备份 | [数据契约](docs/DATA.md) |
| 让 AI 协助操作 | [项目约束](AGENTS.md)、[AI 操作指南](docs/AI_GUIDE.md) |
| 开发与维护 | [开发指南](docs/DEVELOPMENT.md)，包含架构、代码入口、复用能力和开发准则 |
| 商品运营报告 | [报告说明](docs/PRODUCT_REPORT.md)：退货、差评、近7/14天出单和店铺链接缺口 |
| WB Open API 商品写入 | [备用上架与内容同步](docs/OPEN_API_LISTINGS.md)：两种商品来源、建卡后有限等待并上传图片/库存、断点续办、相同 VC 跨店更新；BCS 默认接口保留 |
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

当前 WB 凭证使用手动登录、抓包并写入的方式更新，**Playwright 尚未启动，浏览器自动续期未启用**；使用现有功能不要求启动它。受限浏览器的抓包步骤见 [配置](docs/CONFIGURATION.md#手动抓包更新凭证当前方式)。飞书功能另需已登录的配套 lark-cli。

## 更新已有环境

已有用户可继续使用原来的 `data/` 和 `.venv`，无需重建数据或重新填写凭证。更新前先停止本项目的运行进程，在项目根目录执行：

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
    价格映射表.xlsx
    products/
    state/
    shops/
```

样例凭证为空，不能直接连接平台。基础业务先准备所需平台凭证；商品匹配、价格计算和履约还依赖对应表格及库存 SKU。真实凭证、浏览器资料和业务数据由 Git 排除，更新代码时保留自己的 data，并单独备份。

默认 data 路径由项目位置确定，不随终端工作目录变化。只有需要单独存放数据时才使用 `--data-dir` 或 `SELLEROPS_DATA_DIR`；优先级为命令行、环境变量、项目根目录 data。全局参数放在子命令之前：

```powershell
python run.py --data-dir "D:\SellerData" shops
```

## 检查入口并开始使用

以下命令显示帮助或预览，不访问平台、不注册任务：

```powershell
python run.py price --help
python run.py daily morning
python run.py schedule
```

配置完成后可查询 BCS 店铺列表：

```powershell
python run.py shops
```

这只能检查 BCS 店铺查询，不能证明其他平台已登录可用。已有表格和快照时，按自己的店铺和商品生成操作预览：

```powershell
python run.py price --shops 1001 --name "示例商品"
```

店铺 ID 和商品名都是占位示例。核对范围、数量、价格及授权后，再对相同范围显式提交：

```powershell
python run.py price --shops 1001 --name "示例商品" --apply --yes
```

业务命令的 dry-run 可能读取平台并生成本地报告。首次需要初始化或更新商品快照时，按 [操作流程](docs/USAGE.md) 单独执行 fetch / merge。`fetch` 默认通过 BCS 保存的各店 apiKey 直接读取 WB 官方中国区商品、价格及莫斯科仓库存，全店默认4店并发（`--parallel` 可调整），不再等待 BCS 商品同步；旧来源须显式指定 `--source bcs`。不要把同步当作每次启动或写入后的固定步骤。

## 日常使用边界

- 凡涉及仓库的操作，**默认且仅限各店铺自己的莫斯科仓库 ID**。全店/全部商品不等于全仓；成都及其他地区仓不操作，也不作为找不到莫斯科时的备用仓。具体 ID 规则见 [配置](docs/CONFIGURATION.md#仓库范围仅允许莫斯科)。
- 普通用户及其运营 AI 使用已有命令，维护自己的 data；源码、文档、依赖声明和项目配置保持只读，临时脚本与产物放 `_scratch/`。维护者及其明确授权的开发任务按开发指南修改项目。
- WB/BCS 写入异步处理。提交并记录接口结果后结束，不进行写后查询验证，不附加同步、拉取或合并。需要更新数据时，由用户单独要求运行 fetch / merge。
- `publish-openapi` 包含建卡、有限等待新商品/规格标识、图片与莫斯科仓库存提交；该标识轮询是上架必要步骤，图片/库存受理后不回读验证。超时可用同一命令的 `--resume` 续办，不重复建卡。
- `--apply` 提交业务写入，部分操作还需 `--yes`；不同命令的确认机制不同。保留任务号与成功、失败、跳过明细，非零退出可能已有部分提交，不能盲目整批重试。
- Windows 任务记录注册时的解释器、代码和数据路径；移动项目或更换环境后需重新注册，并避免同一业务重复执行。

## 开发检查

```powershell
python scripts/check.py
python scripts/check_docs.py
```

离线检查验证源码、导入、依赖边界、命令入口和业务行为，不连接平台或读取真实凭证。改动 CLI 后先运行 `python scripts/generate_cli_docs.py` 更新 [命令参考](docs/CLI.md)。代码结构与扩展步骤见 [开发指南](docs/DEVELOPMENT.md)。
