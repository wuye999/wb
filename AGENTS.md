# WorkBuddy 操作指南

1. 首次接手先运行 `sellerops doctor --json`。平时用本机体检，用户需要核对会话时才加 `--check-online`。连接或恢复会话用用户明确要求的 `login`，WB 遇到 401/403 立即停止该店。
2. 优先调用内置 CLI 命令，不要写临时脚本直接读 SQLite。查询和导出用下方命令，加 `--json` 后整体解析 stdout，按回执里的路径找文件。
3. 改价、折扣、库存、下架、上架和改码先用 `ops` 生成计划单，展示店铺、商品、规格与目标值。运营明确确认后才运行 `apply --plan`。同店仓库只用仓库列表里确认的莫斯科 `id`，下架先确认全部规格清零受理。提交中和结果未知的请求禁止重发，平台提交及记录后结束。业务必需的上架标识等待和马帮预报轮询照原流程执行。
4. 未归属商品、折扣和库存有歧义时先核对。用完整 VC 确认商品，按本次动作和店铺取本店 `nmId`、`chrtId`。《商品价格表.xlsx》由运营维护，程序只读。商品有卖家 SKU 时用 SKU，空白时用唯一的产品中文名。
5. 用户要求更新快照才单独运行 `sync`。发现 `missing_shops` 时先请运营确认，获准后用 `shop archive` 软归档。各店使用自己的快照和状态。
6. 凭证、数据库和运行资料留在各自的 `data/`，临时工作放 `_scratch/`。日常运营将项目源码和配置视为只读。授权开发先备份数据，再跑 `python scripts/check.py`。CLI 参数改动后重新生成参考并跑 `python scripts/check_docs.py`。

## 常用查询和导出

- `map candidates` 查看待认领候选，`map export-pending` 导出六列待确认清单。路径看 `data.file`，行数看 `data.rows`。填好后用 `map import-excel` 导回。
- `report mapping` 导出映射总览，路径看 `data.artifacts.workbook`。单店表加 `--shop-id`，把示例里的 `1001` 换成 `shop list` 返回的店铺 ID。以下示例默认使用项目的 `data/`。

```powershell
python run.py doctor --json
python run.py shop list --json
python run.py map candidates --limit 20 --json
python run.py map export-pending --json
python run.py map import-excel "exports/待确认商品.xlsx" --json
python run.py report mapping --json
python run.py report mapping --shop-id 1001 --json
```

具体命令、认证续期、快照来源、马帮店铺范围和写操作边界见 [日常操作指南](docs/AI_GUIDE.md)，授权开发见 [开发指南](docs/DEVELOPMENT.md)。
