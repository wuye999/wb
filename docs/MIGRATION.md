# SellerOps 2.0 命令迁移

## 迁移旧商品数据

1. 关闭其他 SellerOps 命令，备份整个 `data/`。如有未完成的旧映射保存，先用上一版程序在副本上恢复。
2. 在备份副本目录试跑迁移，查看终端摘要中的每店行数、SKU 修复、待确认项和历史提交记录。确认结果后，再按同样方式迁移正式数据目录。

```powershell
python run.py --data-dir _scratch/legacy-copy migrate-legacy
```

迁移保留完整 VC 和每个店铺独立的商品及规格 ID。人工排除优先，人工纠偏可修复旧 SKU。已生效的前缀归属直接继承，冲突、缺少唯一 SKU 和未归属商品留待确认。上架排除清单作为禁上架处理，已有的归属仍用于履约和报告。

迁移只读旧资料并写入同一目录的 `sellerops.db`。遇到损坏文件或快照标识冲突，整次导入回滚。已有内容的库拒绝被旧文件覆盖。导出 Excel 使用 `report mapping`。

以下对照覆盖 1.0 的全部 57 个入口。新命令前加 `python run.py`。

商品写入需要指定店铺和商品。全店使用 `--all-shops`，全部商品使用 `--all-products`。原中文名包含筛选迁为 `--name-contains`。设置库存必须显式提供数量。BCS 通道仅由操作者显式选择。

| 原入口 | 2.0 入口 |
| --- | --- |
| `shops` | `shop list` |
| `fetch` | `catalog fetch` |
| `mapping` | `catalog mapping` |
| `mapping-import` | `catalog import-mapping` |
| `mapping-check` | `catalog check-mapping` |
| `mismatch-check` | `catalog check-mismatch` |
| `review` | `catalog review` |
| `merge` | `catalog merge` |
| `mapping-rename` | `catalog rename` |
| `shops-mapping` | `catalog shop-mappings` |
| `price` | `product price` |
| `price-bcs` | `product price --channel bcs` |
| `stock` | `product stock` |
| `stock-bcs` | `product stock --channel bcs` |
| `trash` | `product trash` |
| `trash-bcs` | `product trash --channel bcs` |
| `dimension` | `product dimensions` |
| `dims-check` | `product check-dimensions` |
| `banned` | `product banned` |
| `clean` | `product clean` |
| `discount` | `product discount` |
| `discount-bcs` | `product discount --channel bcs` |
| `price-review` | `product review-price` |
| `publish` | `listing publish` |
| `create-cards` | `listing create-cards` |
| `replicate` | `listing replicate` |
| `import-publish` | `listing import` |
| `publish-openapi` | `listing publish-openapi` |
| `normalize-vc-openapi` | `listing normalize-vc` |
| `sync-cards-openapi` | `listing sync-cards` |
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
| `daily` | `task run`；原 check 模式改为 adjust-discounts |
| `schedule` | `task schedule` |
| `login` | `login` |
| `warehouse-clear` | 移除。成都仓永久删除不属于店铺运营范围。 |

## 更新与回退

1. 更新前列出现有 SellerOps_Morning、SellerOps_Check11、SellerOps_Check15、SellerOps_Check19 的解释器、时间和参数，核对自己的快捷方式、WorkBuddy 指令及正在运行的客服监听。记录原授权范围后暂停这些任务。
2. 备份自己的完整 data 目录。更新代码和现有环境，在项目根目录运行帮助及 `task schedule` 预览。逐项按上表更新旧命令。原折扣阈值规则显式传 `--threshold 50`，原日常任务已内置该参数。
3. 人工恢复原有授权任务。`task schedule --all-shops --apply` 注册原时间的四项全店任务。原先只操作特定店铺的任务用 `task schedule --shops 1001,1002 --apply` 按原范围恢复（ID 为示例）。
4. 回退时先停用新任务，恢复上一版程序、解释器和任务定义。保留最新的提交记录、客服去重和上架续办状态。已提交的运营操作不随程序回退撤销。

外部表上架旧参数 `--cn` 按包含匹配。迁移到 `listing import --name-contains` 可沿用原筛选；`--name` 只匹配完整名称。

客服监听恢复时沿用原店铺和间隔。查看使用默认展示模式，起草使用 `--mode back`，发送使用 `--mode back --apply`。旧监听单独使用 `--apply` 也代表后台发送。前台展示与发送不能同时选择。

## WorkBuddy 商品命令迁移

| 旧用法 | 当前用法 |
| --- | --- |
| 手动运行 `product discount` 并依赖默认目标 50 | 显式指定 `product discount --target 50`，或填写用户指定的 0 至 100 |
| `product price --discount D` 同时按价格表改价 | 显式指定 `product price --from-price-book --discount D`，只改折扣用 `product discount --target D` |

原有 `product price --keep-price --discount D` 可继续使用。三项日常命令默认 WB，计划任务支持 `--shops` 并继续显式传入目标折扣 50。库存用 `--amount` 指定数量。自动化可从三项商品命令及 `report products` 的标准输出末行读取 `schema_version=1` JSON，既有数据目录可直接沿用。
