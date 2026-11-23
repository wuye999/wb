# SellerOps 2.0 命令迁移

## 迁移旧商品数据

1. 关闭其他 SellerOps 命令，备份整个 `data/`。如有未完成的旧映射保存，先用上一版程序在副本上恢复。
2. 在备份副本目录试跑迁移，查看终端摘要中的每店行数、SKU 修复、待确认项和历史提交记录。确认结果后，再按同样方式迁移正式数据目录。

```powershell
python run.py --data-dir _scratch/legacy-copy migrate-legacy
```

迁移保留完整 VC 和每个店铺独立的商品及规格 ID。人工排除优先，人工纠偏可修复旧 SKU。已生效的前缀归属直接继承，卖家 SKU 为空时按全表唯一的产品中文名关联。归属冲突、无法确定唯一商品和原未归属商品留待确认。上架排除清单作为禁上架处理，已有的归属仍用于履约和报告。

迁移只读旧资料并写入同一目录的 `sellerops.db`。遇到损坏文件或快照标识冲突，整次导入回滚。已有内容的库拒绝被旧文件覆盖。导出 Excel 使用 `report mapping`。

首次配置依次运行 `sync` 和 `map workbench`，看图归属后用 `map batch-set --file` 或 `catalog merge` 导入导出的审核 JSON。旧单店审核请用新工作台检查并重新导出。已归属商品的映射异常和货不对板筛查统一使用 `map check-mismatch` 的大图画廊。

以下对照列出 1.0 命令的迁移入口。新命令前加 `python run.py`。

商品计划按命令要求指定店铺和商品。支持全店或全部商品的命令见各自帮助。原中文名包含筛选迁为 `--name-contains`。设置库存必须显式提供数量。BCS 通道仅由操作者显式选择。

| 原入口 | 2.0 入口 |
| --- | --- |
| `shops` | `shop list` |
| `fetch` | `catalog fetch` |
| `mapping` | `map workbench` 或 `catalog mapping` |
| `mapping-import` | `map batch-set --file 审核结果.json`（使用新工作台导出的文件） |
| `mapping-check` | `map check-mismatch` |
| `mismatch-check` | `map check-mismatch` 或 `catalog check-mismatch` |
| `review` | `map workbench` |
| `merge` | `catalog merge 审核结果.json` |
| `mapping-rename` | `catalog rename` |
| `shops-mapping` | `catalog shop-mappings` |
| `price` | `ops price --price P` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `price-bcs` | `ops price --channel bcs --price P` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `stock` | `ops stock --amount N` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `stock-bcs` | `ops stock --channel bcs --amount N` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `trash` | `ops trash` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `trash-bcs` | `ops trash --channel bcs` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `dimension` | `product dimensions` 预览，确认后加 `--apply` |
| `dims-check` | `product check-dimensions` |
| `banned` | `product banned` 查询。下架用 `ops trash` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `clean` | `product clean` 预览，确认后加 `--apply --yes` |
| `discount` | `ops discount --target D` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `discount-bcs` | `ops discount --channel bcs --target D --all-products` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `price-review` | `ops review-price` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `publish` | `ops publish` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `create-cards` | `ops create-cards` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `replicate` | `ops replicate` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `publish-openapi` | `ops publish-openapi` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `publish-openapi-finish` | 先迁移旧版阶段记录，再用 `ops publish-openapi --resume` 预览生成新单，确认后用 `apply --plan` 提交 |
| `normalize-vc-openapi` | `ops normalize-vc` 预览生成计划单，确认后用 `apply --plan` 提交 |
| `sync-cards-openapi` | `listing sync-cards` 预览，确认后加 `--apply` |
| `promo-apply` | `marketing enroll` |
| `promo-goods` | `marketing ads` |
| `promo-gap` | `marketing audit` |
| `orders` | `order list` |
| `mabang-orders` | `order match` |
| `mabang-forecast` | `order forecast` |
| `feishu-register` | `order register` |
| `feishu-register-dedup` | `order deduplicate` |
| `mabang-stock-register` | `order stock-register` |
| `mabang-stock-daily` | `order stock-days` |
| `fbs-group-ship-openapi` | `order group-ship` |
| `mabang-process` | `order process` |
| `product-report` | `report products` |
| `feishu-vc-stats` | `report vc-sales` |
| `fbs-delivery` | `report delivery` |
| `order-stats` | `report orders` |
| `ratings` | `report ratings` |
| `questions` | `support questions` |
| `questions-watch` | `support watch-questions` |
| `chat-watch` | `support watch-chats` |
| `chats` | `support chats` |
| `appeals` | `support appeals` |
| `ai-test` | `support ai-test` |
| `daily` | `task run`。原 check 模式改为 `adjust-discounts`，折扣步骤只生成待确认计划 |
| `schedule` | `task schedule` |
| `login` | `login` |
| `warehouse-clear` | 移除。成都仓永久删除不属于店铺运营范围。 |

## 更新与回退

1. 更新前列出现有 SellerOps_Morning、SellerOps_Check11、SellerOps_Check15、SellerOps_Check19 的解释器、时间和参数，核对自己的快捷方式、WorkBuddy 指令及正在运行的客服监听。记录原授权范围后暂停这些任务。
2. 备份自己的完整 data 目录。更新代码和现有环境，在项目根目录运行帮助及 `task schedule` 预览。逐项按上表更新旧命令。原折扣阈值规则显式传 `--threshold 50`，原日常任务已内置该参数。
3. 人工恢复原有授权任务。`task schedule --all-shops --apply` 注册原时间的四项全店任务。原先只操作特定店铺的任务用 `task schedule --shops 1001,1002 --apply` 按原范围恢复（ID 为示例）。
4. 回退时先停用新任务，恢复上一版程序、解释器和任务定义。保留最新的提交记录、客服去重和上架续办状态。已提交的运营操作不随程序回退撤销。

客服监听恢复时沿用原店铺和间隔。查看使用默认展示模式，起草使用 `--mode back`，发送使用 `--mode back --apply`。旧监听单独使用 `--apply` 也代表后台发送。前台展示与发送不能同时选择。

## WorkBuddy 商品命令迁移

| 旧用法 | 当前用法 |
| --- | --- |
| 手动运行 `product discount` 并依赖默认目标 50 | 用 `ops discount --target 50` 预览，或填写用户指定的 0 至 100。确认后用 `apply --plan` 提交 |
| 改价并设折扣 | 用 `ops price --price P --discount D` 预览，确认后用 `apply --plan` 提交 |

只改折扣可用 `ops price --keep-price --discount D` 或 `ops discount --target D` 预览，确认后用 `apply --plan` 提交。库存用 `--amount` 指定数量。每日折扣任务默认走 WB，支持 `--shops`，按目标折扣 50 生成待确认单。自动化使用 `ops`、`apply` 和 `report products` 的 `--json`，分别读取 `data.scope`、`data.receipt` 和 `data.artifacts`。旧命令末行摘要属于兼容输出。
