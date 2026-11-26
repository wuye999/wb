# WorkBuddy 操作指南

1. 首次接手先运行 `sellerops doctor --json`，入口未安装时用 `python run.py doctor --json`。平时用本机体检，用户需要核对会话时才加 `--check-online`。连接或恢复会话用用户明确要求的 `login`，WB 遇到 401/403 立即停止该店。
2. 只读查询优先用内置 CLI，如 `shop list --json` 和 `catalog product-summary`。CLI 未覆盖的分析可只读本地数据，按需过滤归档店、回收站和非货盘，并区分本店默认业务仓库存、原价和折后价。支持 `--json` 的查询和导出整体解析 stdout，按回执里的路径找文件。标准商品表的导出路径见终端输出。
3. 改价、价格审核、折扣、库存、下架和改码的店铺、商品、目标值已明确时，直接用 `ops` 生成预览，展示店铺、商品、规格与目标值。运营确认一次后用 `apply --plan` 整单提交。`--price` 设置原价（吊牌价）。WB 折扣越大，折后价越低，反之越高。WB 各店只用按 `wb.warehouse_keyword` 唯一匹配并核实的默认业务仓 `id`，否则中止该店。马帮库存匹配使用本账号 `mabang.warehouse_keyword` 唯一命中的启用仓，也可沿用已核实的 `mabang.warehouse_id`。两项都填时须一致，严禁与 WB 仓库 ID 混用。下架先确认全部规格清零受理。提交中和结果未知的请求禁止重发，平台提交及记录后结束。业务必需的上架标识等待和马帮预报轮询照原流程执行。
4. 未归属商品、折扣和库存有歧义时先核对。用完整 VC 确认商品，按本次动作和店铺取本店 `nmId`、`chrtId`。运营明确要求时可编辑《商品价格表.xlsx》。先请用户保存并关闭 Excel、备份原表，使用可保留公式和嵌入图片的编辑方式。商品有卖家 SKU 时用 SKU，空白时用唯一的产品中文名。
5. 用户要求更新快照才单独运行 `sync`。首次成功保存快照后，主动引导运营使用 `map workbench` 看图归属。发现 `missing_shops` 时先请运营确认，获准后用 `shop archive` 软归档。各店使用自己的快照和状态。
6. 凭证、数据库和运行资料留在各自的 `data/`，其他临时工作放 `_scratch/`。修改 `credentials.json` 时仅在 `data/` 内备份或原子替换。密码只报「已填/未填」，不报字符位数。日常运营将项目源码和配置视为只读。授权开发先备份数据，再跑 `python scripts/check.py`。CLI 帮助或参数改动后重新生成参考并跑 `python scripts/check_docs.py`。
7. 按本次回执的路径查找导出和日志。默认导出在 `exports/`，运行日志和回执在 `data/logs/YYYY-MM-DD/`。定期清理先用 `task clean --json` 预览，运营明确要求清理时加 `--apply`。默认保留最近 20 天，保护 `exports/workbench/` 和 `_scratch/README.md`。

`sync` 和 `catalog fetch` 按当前价格表唯一四位 ASCII 字母前缀，自动归属本次成功保存、在架且无禁上架限制的待认领 VC，来源为 `prefix`。按 `data.fetched[].prefix_mapped` 和 `data.prefix_mapping` 汇报，并用 `map check-mismatch` 复核。前缀未命中、冲突或带禁上架限制的待认领 VC，以及人工纠偏和排除，按完整 VC 用 `map batch-set --file`、`catalog merge` 或 `catalog rename` 确认。严禁直接修改 SQLite `vc_map`。价格表删行后仍可按完整 VC 查历史中文名，选品、履约、报价及价格表依赖的上架和改码只用现行资料。

逐店用同币种核对最低售价基准、原价倍率、折扣百分比及折后价。常规折后价按 `原价 × (1 − 折扣百分比 / 100)`，平台额外优惠和实取值注明来源。`SellerOps_Morning` 和 `SellerOps_Check11/15/19` 是本机计划任务，早间任务提交促销报名，折扣步骤均生成待确认计划。临时折扣规则只用于本次计划，运营明确要求调整计划任务时才走 `task schedule`。

**上架选型规则。日常上架统一走 BCS 建卡通道。** CLI 用 `ops create-cards`、`ops replicate` 或 `listing import-publish`，不要主动选用 `ops publish` 或 `ops publish-openapi`。WorkBuddy 自定义脚本批量上架统一构造字典列表，一次传入 `sellerops.features.publishing.card_publish.publish_cards_simple`。先展示预览中的店铺、完整目标 VC、售价、包装规格与库存。运营明确确认后，`ops` 用 `apply --plan`，标准表导入用 `--apply`，自定义脚本用 `apply=True`。

处理新增订单使用 `order process`，马帮发货进入匹配、预报和交运。WB 发货用 `order group-ship`。配送受理后，用 `order mabang-void-wb` 预览仍滞留在马帮的待处理订单，经确认再加 `--apply`。需要飞书登记时，先运行 `order register` 或 `order process` 完成登记。具体分流和用法见 [订单履约与飞书](docs/USAGE.md#订单履约与飞书)。

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
