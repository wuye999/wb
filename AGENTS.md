# WorkBuddy 操作指南

1. 首次接手先运行 `sellerops doctor --json`。平时用本机体检，用户需要核对会话时才加 `--check-online`。连接或恢复会话用用户明确要求的 `login`，WB 遇到 401/403 立即停止该店。
2. 优先调用内置 CLI 命令，不要写临时脚本直接读 SQLite。支持 `--json` 的查询和导出整体解析 stdout，按回执里的路径找文件。标准商品表的导出路径见终端输出。
3. 改价、价格审核、折扣、库存、下架和改码先用 `ops` 生成计划单，展示店铺、商品、规格与目标值。运营明确确认后才运行 `apply --plan`。WB 仓库只用各店按 `data/credentials.json` 中 `wb.warehouse_keyword` 唯一匹配并核实的本店默认业务仓 `id`（如莫斯科仓或运营配置的其他业务仓）。未配置、匹配不到或匹配多个时中止该店操作，不猜测、不跨店复用 ID、不回退未配置的仓库。下架先确认全部规格清零受理。提交中和结果未知的请求禁止重发，平台提交及记录后结束。业务必需的上架标识等待和马帮预报轮询照原流程执行。
4. 未归属商品、折扣和库存有歧义时先核对。用完整 VC 确认商品，按本次动作和店铺取本店 `nmId`、`chrtId`。《商品价格表.xlsx》由运营维护，程序只读。商品有卖家 SKU 时用 SKU，空白时用唯一的产品中文名。
5. 用户要求更新快照才单独运行 `sync`。发现 `missing_shops` 时先请运营确认，获准后用 `shop archive` 软归档。各店使用自己的快照和状态。
6. 凭证、数据库和运行资料留在各自的 `data/`，临时工作放 `_scratch/`。日常运营将项目源码和配置视为只读。授权开发先备份数据，再跑 `python scripts/check.py`。CLI 帮助或参数改动后重新生成参考并跑 `python scripts/check_docs.py`。
7. 按本次回执的路径查找导出和日志。默认导出在 `exports/`，运行日志和回执在 `data/logs/YYYY-MM-DD/`。定期清理先用 `task clean --json` 预览，运营明确要求清理时加 `--apply`。默认保留最近 20 天，保护 `exports/workbench/` 和 `_scratch/README.md`。

**上架选型规则。日常上架统一走 BCS 建卡通道。** CLI 用 `ops create-cards`、`ops replicate` 或 `listing import-publish`，不要主动选用 `ops publish` 或 `ops publish-openapi`。WorkBuddy 自定义脚本批量上架统一构造字典列表，一次传入 `sellerops.features.publishing.card_publish.publish_cards_simple`。先展示预览中的店铺、完整目标 VC、售价、包装规格与库存。运营明确确认后，`ops` 用 `apply --plan`，标准表导入用 `--apply`，自定义脚本用 `apply=True`。

## 常用查询和导出

- `map workbench` 在 `exports/workbench/` 生成带图待认领工作台，路径见 `data.artifacts.html`。`map candidates` 查询分组和参考候选，用 `map batch-set --file` 或 `catalog merge` 按完整 VC 导入运营确认的审核 JSON。
- `report mapping` 导出映射总览，路径看 `data.artifacts.workbook`。单店表加 `--shop-id`，把示例里的 `1001` 换成 `shop list` 返回的店铺 ID。以下示例默认使用项目的 `data/`。

```powershell
python run.py doctor --json
python run.py shop list --json
python run.py map workbench --json
python run.py map candidates --limit 20 --json
python run.py report mapping --json
python run.py report mapping --shop-id 1001 --json
python run.py catalog export-products --all-shops
python run.py listing import-publish --file exports/shops_products.xlsx --shops 1002 --prefix ABCD --limit 50
```

- 详细 AI 操作规范与命令选型见 [AI 日常操作指南](docs/AI_GUIDE.md)。
- 按模块查阅 CLI 命令示例见 [CLI 用法](docs/USAGE.md)，完整参数见 [命令参考](docs/CLI.md)。
- 授权开发见 [开发指南](docs/DEVELOPMENT.md)。
