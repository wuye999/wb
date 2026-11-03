# 命令参考

由 `python scripts/generate_cli_docs.py` 从实际参数定义生成。

使用方法见 [操作流程](USAGE.md)，配置见 [CONFIGURATION](CONFIGURATION.md)，AI 边界见 [AI_GUIDE](AI_GUIDE.md)。

源码运行时将下方 usage 的 `sellerops` 换成 `python run.py`。全局 `--data-dir` 放在子命令之前。

远端业务写入显式加 `--apply`；部分命令另需 `--yes`，具体以实际参数及操作流程为准。
本地映射、凭证导入等命令会直接写本地数据。dry-run 不等于离线；写命令结束后不追加验证或同步。

当前凡涉及仓库的操作默认且仅限各店莫斯科仓库 ID，不回退其他地区仓；全店/全部商品不扩大到全仓。`warehouse-clear` 面向成都仓，当前不使用。
当前 WB 凭证使用人工登录、手动抓包并填写/导入；Playwright 尚未启动，`cookie-refresh-wb` 保留但未启用。受限浏览器步骤见 [手动抓包说明](CONFIGURATION.md#手动抓包更新凭证当前方式)。

共 57 个命令。处理器链接用于维护者定位实现，不会在生成文档时加载处理器。

| 命令 | 用途 | 实现入口 |
| --- | --- | --- |
| `shops` | 账号店铺列表 | [sellerops.integrations.bcs.client:print_shops](../src/sellerops/integrations/bcs/client.py) |
| `product-report` | 只读：默认筛选低评分、高退货率及近期出单补货候选 → Excel | [sellerops.features.catalog.product_report:run](../src/sellerops/features/catalog/product_report.py) |
| `fetch` | 拉取商品快照（默认 WB 官方 API，仅从 BCS 读取店铺密钥） | [sellerops.cli.catalog_handlers:run_fetch](../src/sellerops/cli/catalog_handlers.py) |
| `mapping` | 生成统一核对工作台（全部活跃店铺并集，一页两区） | [sellerops.cli.catalog_handlers:run_mapping](../src/sellerops/cli/catalog_handlers.py) |
| `mapping-import` | 导入核对结果 → 生成映射表（旧格式） | [sellerops.cli.catalog_handlers:run_mapping_import](../src/sellerops/cli/catalog_handlers.py) |
| `mapping-check` | 映射表核查工作台（带图，核对匹配是否有误） | [sellerops.cli.catalog_handlers:run_mapping_check](../src/sellerops/cli/catalog_handlers.py) |
| `mismatch-check` | 货不对板筛查工作台（看图勾选，导出 vc 下架） | [sellerops.cli.catalog_handlers:run_mismatch_check](../src/sellerops/cli/catalog_handlers.py) |
| `review` | 多店铺待审核工作台 | [sellerops.cli.catalog_handlers:run_review](../src/sellerops/cli/catalog_handlers.py) |
| `merge` | 增量合并审核 → 映射表（file 可选） | [sellerops.cli.catalog_handlers:run_merge](../src/sellerops/cli/catalog_handlers.py) |
| `mapping-rename` | 纠偏/修改商品中文名（自动级联更新全部店铺单表与聚合总表） | [sellerops.cli.catalog_handlers:run_mapping_rename](../src/sellerops/cli/catalog_handlers.py) |
| `shops-mapping` | 刷新/生成各店铺独立映射表（data/shops/shop_*.xlsx） | [sellerops.cli.catalog_handlers:run_shops_mapping](../src/sellerops/cli/catalog_handlers.py) |
| `price` | 改价/改折扣（默认 WB 原生 dp-api 批量，自动确认降价/隔离区弹窗；dry-run 默认） | [sellerops.features.listings.prices:run](../src/sellerops/features/listings/prices.py) |
| `price-bcs` | [备选] 改价走 BCS 接口（price/batch，WB 原生不可用时的兜底） | [sellerops.features.listings.operations:run (price)](../src/sellerops/features/listings/operations.py) |
| `stock` | 改库存（默认 WB 原生在线接口；dry-run 默认，归零须 --yes） | [sellerops.features.listings.stocks:run](../src/sellerops/features/listings/stocks.py) |
| `stock-bcs` | [备选] 改库存走 BCS 接口（stock/batchSetByChrtIdsBatch，WB 原生不可用时的兜底） | [sellerops.features.listings.operations:run (stock)](../src/sellerops/features/listings/operations.py) |
| `trash` | 下架（默认 WB 原生 moveNmsToTrash 移回收站；先清库存；不可逆） | [sellerops.features.listings.operations:run (trash, wb)](../src/sellerops/features/listings/operations.py) |
| `trash-bcs` | [旧通道] 移回收站走 BCS 接口（clean/removeToTrash）；仅供排查，**不作兜底**（BCS 不可靠） | [sellerops.features.listings.operations:run (trash, bcs)](../src/sellerops/features/listings/operations.py) |
| `replicate` | 跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认） | [sellerops.features.listings.replicate:run](../src/sellerops/features/listings/replicate.py) |
| `import-publish` | 他人映射表导入上架：他人有我方无的商品（按真实 WB商品码 匹配）上架到我的店铺 | [sellerops.features.listings.import_publish:run](../src/sellerops/features/listings/import_publish.py) |
| `dimension` | 批量修改尺寸（按价格表或 --dims 自定义，dry-run 默认，--apply 执行） | [sellerops.features.listings.dimension:run](../src/sellerops/features/listings/dimension.py) |
| `dims-check` | 查询 WB 包装尺寸/重量偏差待验证商品（只读） | [sellerops.features.listings.dims_check:run](../src/sellerops/features/listings/dims_check.py) |
| `banned` | 查询并删除被阻止的商品（dry-run 默认，--apply 移到回收站） | [sellerops.features.listings.banned:run](../src/sellerops/features/listings/banned.py) |
| `clean` | 清理草稿箱/回收站 | [sellerops.features.listings.clean:run](../src/sellerops/features/listings/clean.py) |
| `warehouse-clear` | 成都仓库商品永久删除（当前仅操作莫斯科，不使用此命令） | [sellerops.features.listings.warehouse_clear:run](../src/sellerops/features/listings/warehouse_clear.py) |
| `publish` | 批量发布上架：输入 WB 商品码推送到店铺（支持指定前缀/完整VC/价格/尺寸/店铺） | [sellerops.features.listings.bulk_publish:run](../src/sellerops/features/listings/bulk_publish.py) |
| `create-cards` | 建卡发布上架：输入 WB 商品码推送到店铺（支持自定义完整VC/俄文详情与主图建卡） | [sellerops.features.listings.card_publish:run](../src/sellerops/features/listings/card_publish.py) |
| `publish-openapi` | [备用] WB Open API 上架：建卡、有限等待标识、上传图片和莫斯科库存；支持续办 | [sellerops.features.listings.open_api_publish:run](../src/sellerops/features/listings/open_api_publish.py) |
| `normalize-vc-openapi` | WB Open API：纠正历史 VC 前缀，保留原尾部，冲突时追加跨店一致序号 | [sellerops.features.listings.vc_normalization:run](../src/sellerops/features/listings/vc_normalization.py) |
| `sync-cards-openapi` | WB Open API：按相同完整 VC 将源店商品内容同步到其他店 | [sellerops.features.listings.open_api_sync:run](../src/sellerops/features/listings/open_api_sync.py) |
| `promo-apply` | 促销报名（cookie 会话，applyAll） | [sellerops.features.pricing.promo:run](../src/sellerops/features/pricing/promo.py) |
| `promo-goods` | 查询 WB 广告推广中被推广的商品（中文名/供应商代码/WB商品码，只读） | [sellerops.features.pricing.adverts:run](../src/sellerops/features/pricing/adverts.py) |
| `promo-gap` | 只读：推广 × 销量双向错配审计（gap=该推没推 / waste=在推但没销量该关） | [sellerops.features.pricing.promo_gap:run](../src/sellerops/features/pricing/promo_gap.py) |
| `discount` | 折扣改价（WB 原生批量，从高到低查询，支持 --name/--vc/--shops 灵活筛选，默认不做写后验证） | [sellerops.features.pricing.discounts:run_cli](../src/sellerops/features/pricing/discounts.py) |
| `discount-bcs` | BCS 通道全量改折扣（较慢，按需手动调用） | [sellerops.features.pricing.discount_bcs:run](../src/sellerops/features/pricing/discount_bcs.py) |
| `price-review` | 价格审核：查看并应用新价格（降价 30-49.9%% 进审查的商品） | [sellerops.features.pricing.price_review:run](../src/sellerops/features/pricing/price_review.py) |
| `orders` | 订单查询（自动同步 + 查询指定日期区间） | [sellerops.features.orders.orders:run](../src/sellerops/features/orders/orders.py) |
| `mabang-orders` | 马帮待处理订单 SKU 匹配核对/更换（VC→映射表中文名→价格表库存SKU） | [sellerops.features.orders.matching:run](../src/sellerops/features/orders/matching.py) |
| `mabang-forecast` | 马帮预报批次：已匹配商品订单生成预报批次并上传（dry-run 默认；--check 查批次状态） | [sellerops.features.orders.forecast:run_forecast](../src/sellerops/features/orders/forecast.py) |
| `feishu-register` | 马帮订单登记到飞书多维表格（按订单编号去重，dry-run 默认） | [sellerops.features.orders.feishu_register:run](../src/sellerops/features/orders/feishu_register.py) |
| `feishu-register-dedup` | 存量修复：「订单登记」表内重复订单编号清理（按订单编号分组，每组保留表内最早一条、其余删除；dry-run 默认） | [sellerops.features.orders.feishu_register_dedup:run](../src/sellerops/features/orders/feishu_register_dedup.py) |
| `mabang-stock-register` | 拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」（含附件列「图」） | [sellerops.features.orders.mabang_stock:run](../src/sellerops/features/orders/mabang_stock.py) |
| `mabang-stock-daily` | 「马帮库存登记表」日期列管理：默认只建今天列+更新全部已有日期列；--begin/--date 任一显式给出即进入区间模式（删早于该日的旧列+补建区间缺列，--end 需同用） | [sellerops.features.orders.mabang_stock_daily:run_daily](../src/sellerops/features/orders/mabang_stock_daily.py) |
| `feishu-vc-stats` | 只读：飞书「订单登记」按供应商代码(BCS编号)统计单数并降序（跨店合并；支持店铺/日期过滤） | [sellerops.features.orders.feishu_vc_stats:run](../src/sellerops/features/orders/feishu_vc_stats.py) |
| `fbs-delivery` | 只读：WB FBS「配送中」清单及其全部商品采集+统计（按清单 createdAt 时段、商品状态默认 shipment=待发货；控制台汇总 + CSV 明细） | [sellerops.features.orders.fbs_delivery:run](../src/sellerops/features/orders/fbs_delivery.py) |
| `order-stats` | 只读：WB「订单动态」近一月订单按商品中文名统计（订单数/退货数/退货率/完单口径退货率/未完结）→ xlsx 多 Sheet（汇总 + 按供应商代码 + 每店 + 订单明细） | [sellerops.features.orders.order_stats:run](../src/sellerops/features/orders/order_stats.py) |
| `mabang-process` | 马帮订单处理一体：匹配商品→预报单→上传（自动发货）→物流交运→飞书登记（登记含只匹配未进预报流程的订单） | [sellerops.features.orders.mabang_process:run](../src/sellerops/features/orders/mabang_process.py) |
| `questions` | 买家未处理提问查询与回复 | [sellerops.features.support.questions:run](../src/sellerops/features/support/questions.py) |
| `questions-watch` | 买家提问实时监听（front=前台AI打印提问/商品信息+手动回复，back=后台AI LLM 自动回复） | [sellerops.features.support.questions_watch:run](../src/sellerops/features/support/questions_watch.py) |
| `chat-watch` | AI 售后客服：买家聊天实时监听（front=前台AI展示/手动，back=后台AI自动回复；默认只展示） | [sellerops.features.support.chat_watch:run](../src/sellerops/features/support/chat_watch.py) |
| `chats` | 查询 WB 买家聊天会话与消息历史（只读，用于验证/排障） | [sellerops.features.support.chat_inspect:run](../src/sellerops/features/support/chat_inspect.py) |
| `appeals` | 查询 WB 平台投诉单（只读；未处理/剩余天数筛选，输出去重商品编号与供应商代码） | [sellerops.features.support.complaints:run](../src/sellerops/features/support/complaints.py) |
| `ratings` | 查询商品链接评分与评价情况（只读；支持按评分/店铺/1星评论筛选，导出包含价格表数据的报表） | [sellerops.features.support.ratings:run](../src/sellerops/features/support/ratings.py) |
| `ai-test` | 使用本地问答集调用 LLM 对照生成回复（会联网） | [sellerops.features.support.ai_reply_test:run](../src/sellerops/features/support/ai_reply_test.py) |
| `cookies-update` | 从抓包 md 刷新凭证 | [sellerops.integrations.sessions.capture:run_cookies_update](../src/sellerops/integrations/sessions/capture.py) |
| `cookie-refresh-wb` | Playwright 刷新 WB 会话（当前未启用，凭证手动抓包更新） | [sellerops.integrations.sessions.browser:run_cookie_refresh_wb](../src/sellerops/integrations/sessions/browser.py) |
| `daily` | 每日任务（morning=报名+改价 / check=只改价） | [sellerops.automation.daily:run_daily_cmd](../src/sellerops/automation/daily.py) |
| `schedule` | 创建/删除 Windows 计划任务（默认预览，--apply 执行） | [sellerops.automation.schedule:run](../src/sellerops/automation/schedule.py) |

## shops

```text
usage: sellerops shops [-h]

options:
  -h, --help  show this help message and exit
```

## product-report

```text
usage: sellerops product-report [-h] [--details] [--shops SHOPS]
                                [--extra-bcs-stdin] [--order-days ORDER_DAYS]
                                [--feedback-days FEEDBACK_DAYS]
                                [--wb-region {ru,cn}] [--url URL]
                                [--table TABLE] [--out OUT]

options:
  -h, --help            show this help message and exit
  --details             追加原始汇总、订单、评价和链接证据表（默认仅简洁筛选与覆盖说明）
  --shops SHOPS         本机目标店铺ID，逗号分隔（默认本机BCS账号全部店铺；不筛选临时账号或飞书来源）
  --extra-bcs-stdin     从标准输入读取临时BCS账号的一个或多个fetch抓包，仅补充统计，凭证不落盘
  --order-days ORDER_DAYS
                        退货统计下单窗口，含今天（1–31天，默认30）
  --feedback-days FEEDBACK_DAYS
                        评价日期窗口，含今天（默认0：接口可访问的全部历史）
  --wb-region {ru,cn}   WB官方主机区域（默认ru；不自动跨域重试）
  --url URL             飞书多维表格地址（默认feishu.base_url）
  --table TABLE         飞书来源表（默认订单登记）
  --out OUT             输出.xlsx路径（默认data/logs/商品运营报告_时间.xlsx）
```

## fetch

```text
usage: sellerops fetch [-h] [--shop-id SHOP_ID] [--source {wb-openapi,bcs}]
                       [--parallel PARALLEL] [--no-sync]

options:
  -h, --help            show this help message and exit
  --shop-id SHOP_ID     店铺ID（默认全部）
  --source {wb-openapi,bcs}
                        快照来源（默认wb-openapi；bcs显式选择旧缓存流程，不自动回退）
  --parallel PARALLEL   Open API并发店铺数（正整数，默认4；1为串行，单店及BCS来源不受影响）
  --no-sync             BCS来源：跳过同步；Open API来源无需同步
```

## mapping

```text
usage: sellerops mapping [-h] [--main-shop-only]

options:
  -h, --help        show this help message and exit
  --main-shop-only  只使用主店候选商品
```

## mapping-import

```text
usage: sellerops mapping-import [-h] file

positional arguments:
  file        核对结果 JSON

options:
  -h, --help  show this help message and exit
```

## mapping-check

```text
usage: sellerops mapping-check [-h] [--tol TOL]

options:
  -h, --help  show this help message and exit
  --tol TOL   价格偏差阈值（元，默认 5）
```

## mismatch-check

```text
usage: sellerops mismatch-check [-h] [--cn CN] [--begin BEGIN] [--end END]
                                [--days DAYS]

options:
  -h, --help     show this help message and exit
  --cn CN        只渲染指定中文名（精确匹配，如『牙膏-紫色』）
  --begin BEGIN  开始日期 YYYY-MM-DD（按映射表创建时间/上架时间筛选）
  --end END      结束日期 YYYY-MM-DD（缺省=今天）
  --days DAYS    最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）
```

## review

```text
usage: sellerops review [-h]

options:
  -h, --help  show this help message and exit
```

## merge

```text
usage: sellerops merge [-h] [file]

positional arguments:
  file        本次审核结果 JSON（可选）

options:
  -h, --help  show this help message and exit
```

## mapping-rename

```text
usage: sellerops mapping-rename [-h] [--vc VC] [--cn CN] [--reason REASON]
                                [--file FILE]

options:
  -h, --help       show this help message and exit
  --vc VC          要改名的 vendorCode
  --cn CN          新产品中文名
  --reason REASON  改名原因说明
  --file FILE      批量改名 JSON 文件路径（含 [{'vc': ..., 'cn': ...}]）
```

## shops-mapping

```text
usage: sellerops shops-mapping [-h] [--shop-id SHOP_ID] [--force]

options:
  -h, --help         show this help message and exit
  --shop-id SHOP_ID  指定店铺ID（默认全部活跃店铺）
  --force            强制全量重新构建
```

## price

```text
usage: sellerops price [-h] [--sku SKU | --name NAME | --prefix PREFIX |
                       --vc VC | --all] [--shops SHOPS] [--apply] [--yes]
                       [--price PRICE] [--discount DISCOUNT]
                       [--club-discount CLUB_DISCOUNT] [--keep-price]
                       [--auto-review] [--chunk CHUNK] [--interval INTERVAL]

options:
  -h, --help            show this help message and exit
  --sku SKU             商品价格表卖家SKU
  --name NAME           商品价格表产品中文名包含
  --prefix PREFIX       商品价格表 vendorCode 前缀码
  --vc VC               vendorCode 列表（逗号分隔）
  --all                 全部映射商品（默认）
  --shops SHOPS         限定店铺ID（逗号分隔，默认全部已 fetch 店铺）
  --apply               真正执行（默认 dry-run）
  --yes                 跳过不可逆操作确认
  --price PRICE         目标价（默认 floor(商品价格表实际售价)）
  --discount DISCOUNT   折扣 0-100（不传=不改）
  --club-discount CLUB_DISCOUNT
                        club折扣 0-100（不传=不改）
  --keep-price          价格保持当前值（只改折扣/俱乐部折扣）
  --auto-review         改价后自动「应用新价格」（降价 30-49.9% 进审查时，精确匹配刚改价商品）
  --chunk CHUNK         每批提交条数（≤300，对齐 discount 实践，默认 100）
  --interval INTERVAL   批/店间请求间隔秒
```

## price-bcs

```text
usage: sellerops price-bcs [-h] [--sku SKU | --name NAME | --prefix PREFIX |
                           --vc VC | --all] [--shops SHOPS] [--apply] [--yes]
                           [--price PRICE] [--discount DISCOUNT]
                           [--club-discount CLUB_DISCOUNT] [--keep-price]
                           [--auto-review]

options:
  -h, --help            show this help message and exit
  --sku SKU             商品价格表卖家SKU
  --name NAME           商品价格表产品中文名包含
  --prefix PREFIX       商品价格表 vendorCode 前缀码
  --vc VC               vendorCode 列表（逗号分隔）
  --all                 全部映射商品（默认）
  --shops SHOPS         限定店铺ID（逗号分隔，默认全部已 fetch 店铺）
  --apply               真正执行（默认 dry-run）
  --yes                 跳过不可逆操作确认
  --price PRICE         目标价（默认 floor(商品价格表实际售价)）
  --discount DISCOUNT   折扣 0-100（不传=不改）
  --club-discount CLUB_DISCOUNT
                        club折扣 0-100（不传=不改）
  --keep-price          价格保持当前值（只改折扣/俱乐部折扣）
  --auto-review         改价后自动「应用新价格」（降价 30-49.9% 进审查时，精确匹配刚改价商品）
```

## stock

```text
usage: sellerops stock [-h] [--sku SKU | --name NAME | --prefix PREFIX |
                       --vc VC | --all] [--shops SHOPS] [--apply] [--yes]
                       [--amount AMOUNT] [--chunk CHUNK] [--interval INTERVAL]
                       [--max-pages MAX_PAGES] [--resolve {snapshot,live}]

options:
  -h, --help            show this help message and exit
  --sku SKU             商品价格表卖家SKU
  --name NAME           商品价格表产品中文名包含
  --prefix PREFIX       商品价格表 vendorCode 前缀码
  --vc VC               vendorCode 列表（逗号分隔）
  --all                 全部映射商品（默认）
  --shops SHOPS         限定店铺ID（逗号分隔，默认全部已 fetch 店铺）
  --apply               真正执行（默认 dry-run）
  --yes                 跳过不可逆操作确认
  --amount AMOUNT       目标库存（默认 0）
  --chunk CHUNK         每批提交条数（≤1000，探针实测 1000 可行，默认 500）
  --interval INTERVAL   批/店间请求间隔秒
  --max-pages MAX_PAGES
                        WB 游标分页安全上限（--resolve live 时使用）
  --resolve {snapshot,live}
                        snapshot=本地完整规格（默认，缺标识时官方API定向补齐）；live=另读莫斯科当前库存，缺库存记录
                        不减少规格
```

## stock-bcs

```text
usage: sellerops stock-bcs [-h] [--sku SKU | --name NAME | --prefix PREFIX |
                           --vc VC | --all] [--shops SHOPS] [--apply] [--yes]
                           [--amount AMOUNT]

options:
  -h, --help       show this help message and exit
  --sku SKU        商品价格表卖家SKU
  --name NAME      商品价格表产品中文名包含
  --prefix PREFIX  商品价格表 vendorCode 前缀码
  --vc VC          vendorCode 列表（逗号分隔）
  --all            全部映射商品（默认）
  --shops SHOPS    限定店铺ID（逗号分隔，默认全部已 fetch 店铺）
  --apply          真正执行（默认 dry-run）
  --yes            跳过不可逆操作确认
  --amount AMOUNT  目标库存（默认 0）
```

## trash

```text
usage: sellerops trash [-h] [--sku SKU | --name NAME | --prefix PREFIX |
                       --vc VC | --all] [--shops SHOPS] [--apply] [--yes]
                       [--chunk CHUNK] [--interval INTERVAL]

options:
  -h, --help           show this help message and exit
  --sku SKU            商品价格表卖家SKU
  --name NAME          商品价格表产品中文名包含
  --prefix PREFIX      商品价格表 vendorCode 前缀码
  --vc VC              vendorCode 列表（逗号分隔）
  --all                全部映射商品（默认）
  --shops SHOPS        限定店铺ID（逗号分隔，默认全部已 fetch 店铺）
  --apply              真正执行（默认 dry-run）
  --yes                跳过不可逆操作确认
  --chunk CHUNK        每批移回收站条数（≤300，默认 300）
  --interval INTERVAL  批间间隔秒
```

## trash-bcs

```text
usage: sellerops trash-bcs [-h] [--sku SKU | --name NAME | --prefix PREFIX |
                           --vc VC | --all] [--shops SHOPS] [--apply] [--yes]
                           [--chunk CHUNK] [--interval INTERVAL]

options:
  -h, --help           show this help message and exit
  --sku SKU            商品价格表卖家SKU
  --name NAME          商品价格表产品中文名包含
  --prefix PREFIX      商品价格表 vendorCode 前缀码
  --vc VC              vendorCode 列表（逗号分隔）
  --all                全部映射商品（默认）
  --shops SHOPS        限定店铺ID（逗号分隔，默认全部已 fetch 店铺）
  --apply              真正执行（默认 dry-run）
  --yes                跳过不可逆操作确认
  --chunk CHUNK        每批条数（≤300，默认 300）
  --interval INTERVAL  批间间隔秒（BCS 有 19s 限流窗口，连续多批建议 21）
```

## replicate

```text
usage: sellerops replicate [-h] [--vc VC] [--prefix PREFIX] [--name NAME]
                           [--shops SHOPS] [--source-shops SOURCE_SHOPS]
                           [--price-source {snapshot,minimum}]
                           [--price-multiplier PRICE_MULTIPLIER]
                           [--stock STOCK] [--limit LIMIT] [--apply]
                           [--interval INTERVAL] [--cn-stock CN_STOCK]

options:
  -h, --help            show this help message and exit
  --vc VC               指定 vendorCode（逗号分隔）
  --prefix PREFIX       前缀码筛选（4 位大写）
  --name NAME           映射表中文名包含匹配
  --shops SHOPS         限定目标店铺 id 逗号分隔（默认全部缺失店）
  --source-shops SOURCE_SHOPS
                        源店铺 id 逗号分隔（默认全部本地店铺，按完整 VC 去重）
  --price-source {snapshot,minimum}
                        价格来源：snapshot=源店快照价格；minimum=商品价格表最低售价
  --price-multiplier PRICE_MULTIPLIER
                        价格来源乘数，按分四舍五入（默认 1）
  --stock STOCK         上架库存（默认999，可显式设为0）
  --limit LIMIT         最多处理 N 个 vc（0=不限）
  --apply               真正执行（默认 dry-run）
  --interval INTERVAL   批次上架请求间隔秒
  --cn-stock CN_STOCK   按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）
```

## import-publish

```text
usage: sellerops import-publish [-h] [--cn CN] [--shops SHOPS] [--limit LIMIT]
                                [--apply] [--interval INTERVAL]
                                [--cn-stock CN_STOCK]
                                xlsx

positional arguments:
  xlsx                 他人映射表 xlsx 路径（同项目「映射总表」格式）

options:
  -h, --help           show this help message and exit
  --cn CN              他人表中文名包含过滤（逗号分隔多个）
  --shops SHOPS        限定目标店铺 id 逗号分隔（默认全部店铺）
  --limit LIMIT        最多处理 N 个商品（0=不限）
  --apply              真正执行（默认 dry-run）
  --interval INTERVAL  批次上架请求间隔秒
  --cn-stock CN_STOCK  按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）
```

## dimension

```text
usage: sellerops dimension [-h] [--vc VC] [--prefix PREFIX] [--name NAME]
                           [--shops SHOPS] [--limit LIMIT] [--dims DIMS]
                           [--apply]

options:
  -h, --help       show this help message and exit
  --vc VC          指定单个或多个 vendorCode（逗号分隔）
  --prefix PREFIX  vendorCode 4位前缀码包含匹配
  --name NAME      商品价格表产品中文名包含匹配
  --shops SHOPS    限定店铺 id 逗号分隔
  --limit LIMIT    每店最多处理 N 条（0=不限）
  --dims DIMS      自定义尺寸 '长*宽*高/毛重'（例: 8*14*26/0.3）
  --apply          真正执行（默认 dry-run）
```

## dims-check

```text
usage: sellerops dims-check [-h] [--type {dims,weight,all}] [--name NAME]
                            [--shops SHOPS] [--limit LIMIT]

options:
  -h, --help            show this help message and exit
  --type {dims,weight,all}
                        dims=尺寸偏差 / weight=重量偏差 / all=两者合并去重（默认 dims）
  --name NAME           映射表中文名包含过滤
  --shops SHOPS         限定店铺 id 逗号分隔
  --limit LIMIT         每店最多查询 N 条（0=不限）
```

## banned

```text
usage: sellerops banned [-h] [--apply] [--shops SHOPS] [--limit LIMIT] [--yes]

options:
  -h, --help     show this help message and exit
  --apply        真正执行（默认 dry-run）
  --shops SHOPS  限定店铺 id 逗号分隔
  --limit LIMIT  每店最多处理 N 条（0=不限）
  --yes          跳过不可逆确认（移到回收站）
```

## clean

```text
usage: sellerops clean [-h] --target {basket,draft,all} [--apply]
                       [--shops SHOPS] [--limit LIMIT]

options:
  -h, --help            show this help message and exit
  --target {basket,draft,all}
                        basket=回收站 / draft=草稿箱 / all=先草稿后回收站
  --apply               真正执行（默认 dry-run）
  --shops SHOPS         限定店铺 id 逗号分隔
  --limit LIMIT         每店最多处理 N 条
```

## warehouse-clear

当前不使用：此命令操作成都仓，不能用于仅允许莫斯科仓的日常任务。命令仍被注册，不代表代码已强制禁用。

```text
usage: sellerops warehouse-clear [-h] [--shops SHOPS] [--limit LIMIT]
                                 [--interval INTERVAL] [--parallel PARALLEL]
                                 [--apply] [--yes]

options:
  -h, --help           show this help message and exit
  --shops SHOPS        限定店铺 id 逗号分隔（默认全部店铺的成都仓）
  --limit LIMIT        （预留）每店最多处理 N 条，0=全部
  --interval INTERVAL  删除请求间隔秒（默认 0.3）
  --parallel PARALLEL  并发店铺数（默认 1=串行；--apply 时有效，店内仍串行）
  --apply              真正执行删除（不可逆）
  --yes                确认永久删除（后台运行必备）
```

## publish

```text
usage: sellerops publish [-h] [--nm NM] [--vc VC] [--prefix PREFIX]
                         [--price PRICE] [--dims DIMS] [--length LENGTH]
                         [--width WIDTH] [--height HEIGHT] [--weight WEIGHT]
                         [--shops SHOPS] [--stock STOCK] [--cn CN]
                         [--file FILE] [--cn-stock CN_STOCK]
                         [--interval INTERVAL] [--apply]
                         [nms ...]

positional arguments:
  nms                  WB 商品码列表（支持空格或逗号分隔，如 248364237 388854754）

options:
  -h, --help           show this help message and exit
  --nm NM              WB 商品码（逗号分隔多个，兼容命令行参数传参）
  --vc VC              完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）
  --prefix PREFIX      4位前缀码（如 ABCD 或 BCS-ABCD）
  --price PRICE        指定售价（CNY），不填时自动从本地价格映射表或商品价格表推导
  --dims DIMS          自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）
  --length LENGTH      包装长 (cm)
  --width WIDTH        包装宽 (cm)
  --height HEIGHT      包装高 (cm)
  --weight WEIGHT      包装毛重 (kg)
  --shops SHOPS        目标店铺 ID（逗号分隔，如 9352,9353；默认上架到全部活跃店铺）
  --stock STOCK        上架库存量（默认 999）
  --cn CN              商品中文名（辅助匹配价格与包装规格）
  --file FILE          批量上架文件路径（支持 .txt 每行一个商品码，或 .json 商品列表）
  --cn-stock CN_STOCK  按中文名指定库存（'中文名:库存,...'）
  --interval INTERVAL  批次/商品请求间隔秒（默认 1.0）
  --apply              真正调用平台接口执行（默认仅 dry-run 预览）
```

## create-cards

```text
usage: sellerops create-cards [-h] [--nm NM] [--vc VC] [--prefix PREFIX]
                              [--price PRICE] [--dims DIMS] [--length LENGTH]
                              [--width WIDTH] [--height HEIGHT]
                              [--weight WEIGHT] [--shops SHOPS]
                              [--stock STOCK] [--cn CN] [--file FILE]
                              [--cn-stock CN_STOCK] [--interval INTERVAL]
                              [--apply]
                              [nms ...]

positional arguments:
  nms                  WB 商品码列表（支持空格或逗号分隔，如 248364237 388854754）

options:
  -h, --help           show this help message and exit
  --nm NM              WB 商品码（逗号分隔多个，兼容命令行参数传参）
  --vc VC              完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）
  --prefix PREFIX      4位前缀码（如 ABCD 或 BCS-ABCD）
  --price PRICE        指定售价（CNY），不填时自动从本地价格映射表或商品价格表推导
  --dims DIMS          自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）
  --length LENGTH      包装长 (cm)
  --width WIDTH        包装宽 (cm)
  --height HEIGHT      包装高 (cm)
  --weight WEIGHT      包装毛重 (kg)
  --shops SHOPS        目标店铺 ID（逗号分隔，如 9352,9353；默认上架到全部活跃店铺）
  --stock STOCK        上架库存量（默认 999）
  --cn CN              商品中文名（辅助匹配价格与包装规格）
  --file FILE          批量上架文件路径（支持 .txt 每行一个商品码，或 .json 商品列表）
  --cn-stock CN_STOCK  按中文名指定库存（'中文名:库存,...'）
  --interval INTERVAL  批次/商品请求间隔秒（默认 1.0）
  --apply              真正调用平台接口执行（默认仅 dry-run 预览）
```

## publish-openapi

```text
usage: sellerops publish-openapi [-h] [--source-shop SOURCE_SHOP] [--nm NM]
                                 [--vc VC] [--all] [--file FILE] --shops SHOPS
                                 [--price PRICE] [--dims DIMS] [--stock STOCK]
                                 [--limit LIMIT] [--resume]
                                 [--wait-timeout WAIT_TIMEOUT]
                                 [--wait-interval WAIT_INTERVAL] [--apply]

options:
  -h, --help            show this help message and exit
  --source-shop SOURCE_SHOP
                        自有源店 BCS 店铺 ID；不传则从公开 CDN 读取任意商品码
  --nm NM               源 WB 商品码，逗号分隔；自有源店模式使用该店商品码
  --vc VC               源店模式为完整 VC 筛选；公开商品模式指定目标完整 VC（支持 {nm} 模板）
  --all                 源店全部非回收站商品；仅自有源店模式
  --file FILE           公开商品 JSON 对象数组：nmId/vendorCode/price，可用 card 覆盖官方字段
  --shops SHOPS         目标店铺 ID，逗号分隔；不可包含源店
  --price PRICE         新建全部规格的整数价格（目标店币种）；文件可逐项指定 price
  --dims DIMS           覆盖包装尺寸：长*宽*高/毛重，单位 cm/kg
  --stock STOCK         莫斯科仓各规格库存（默认 999，0 保留）
  --limit LIMIT         最多选择 N 个源商品（0=不限）
  --resume              按 --shops/--vc 续办已有记录，不重新建卡；沿用原图片和库存计划
  --wait-timeout WAIT_TIMEOUT
                        等待完整商品/规格标识的总时限秒（默认1800，0=只查询一次，最多7200）
  --wait-interval WAIT_INTERVAL
                        标识查询轮次间隔秒（默认15，范围1..60；仍遵守接口限流）
  --apply               执行完整上架或续办流程（默认仅预览，不等待、不写入）
```

## normalize-vc-openapi

```text
usage: sellerops normalize-vc-openapi [-h] (--vc VC | --name NAME | --all)
                                      [--shops SHOPS] [--apply]

options:
  -h, --help     show this help message and exit
  --vc VC        完整旧 VC，逗号分隔
  --name NAME    已确认的中文商品名包含匹配，不按标题或随机前缀推断
  --all          全部非回收站商品；无明确归属的项记录阻止原因
  --shops SHOPS  目标 BCS 店铺 ID，逗号分隔（默认全部店铺；任一店读取失败整轮不提交）
  --apply        提交改码（默认预览）；保留历史防重放日志，不更新本地映射/快照
```

## sync-cards-openapi

```text
usage: sellerops sync-cards-openapi [-h] --source-shop SOURCE_SHOP (--vc VC |
                                    --nm NM | --all) [--shops SHOPS]
                                    [--fields FIELDS] [--limit LIMIT]
                                    [--apply]

options:
  -h, --help            show this help message and exit
  --source-shop SOURCE_SHOP
                        修改后的源店 BCS 店铺 ID（例如袁州1店对应 ID）
  --vc VC               源店完整 VC，逗号分隔
  --nm NM               源店 WB 商品码，逗号分隔
  --all                 源店全部非回收站商品
  --shops SHOPS         目标店铺 ID，逗号分隔（默认 BCS 中除源店外的全部店铺）
  --fields FIELDS       同步字段：title,description,brand,dimensions,characteristic
                        s,photos（默认）；可选 sizes,documents,video,gtin,kizMarked
  --limit LIMIT         最多选择 N 个源商品（0=不限）
  --apply               真正提交内容/媒体更新（默认预览）
```

## promo-apply

```text
usage: sellerops promo-apply [-h] [--apply] [--shops SHOPS] [--days DAYS]
                             [--days-back DAYS_BACK] [--sleep SLEEP]

options:
  -h, --help            show this help message and exit
  --apply               真正报名（默认 dry-run 预览）
  --shops SHOPS         限定店铺 id 逗号分隔
  --days DAYS           查询结束=N天后
  --days-back DAYS_BACK
                        查询起始=N天前
  --sleep SLEEP         活动间请求间隔秒
```

## promo-goods

```text
usage: sellerops promo-goods [-h] [--shops SHOPS] [--status STATUS]
                             [--page-size PAGE_SIZE] [--max-pages MAX_PAGES]
                             [--limit LIMIT] [--no-cn]

options:
  -h, --help            show this help message and exit
  --shops SHOPS         限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）
  --status STATUS       活动状态ID逗号分隔（默认 4,9,11 = 后台默认视图）
  --page-size PAGE_SIZE
                        活动列表分页大小（默认 100）
  --max-pages MAX_PAGES
                        活动列表最多翻页数（安全上限，默认 50）
  --limit LIMIT         每店最多拉取 N 个活动（0=不限，翻页到底）
  --no-cn               不解析商品中文名（供应商代码仍解析）
```

## promo-gap

```text
usage: sellerops promo-gap [-h] [--mode {gap,waste,both}] [--shops SHOPS]
                           [--min MIN] [--days DAYS] [--date DATE]
                           [--begin BEGIN] [--end END] [--status STATUS]
                           [--listed-only] [--top TOP] [--no-cn] [--url URL]
                           [--table TABLE] [--page-size PAGE_SIZE]
                           [--max-pages MAX_PAGES] [--limit LIMIT]

options:
  -h, --help            show this help message and exit
  --mode {gap,waste,both}
                        审计方向：gap=该推没推(默认) / waste=该关的(在推广但本店单数<阈值) / both=两个都出
  --shops SHOPS         限定店铺，逗号分隔，支持店铺ID或短名（如 9352 或 袁州1；默认全部已填 cookie 店铺）
  --min MIN             单数阈值（默认 4）：gap=单数≥N；waste=单数<N
  --days DAYS           近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）
  --date DATE           单天 YYYY-MM-DD（等价 --begin=--end）
  --begin BEGIN         区间开始 YYYY-MM-DD（单独给按单天处理）
  --end END             区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
  --status STATUS       推广活动状态ID逗号分隔（默认 4,9,11 = 含暂停；只算在投用 --status 9）
  --listed-only         [gap] 只输出「可直接补推」组（默认两组都出）
  --top TOP             每组控制台只显示前 N 名（0=全部；CSV 始终写全量）
  --no-cn               不补全商品中文名（飞书表内已有的仍显示）
  --url URL             飞书表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE         飞书表名（默认 订单登记）
  --page-size PAGE_SIZE
                        推广活动列表分页大小（默认 100）
  --max-pages MAX_PAGES
                        推广活动列表最多翻页数（默认 50）
  --limit LIMIT         每店最多拉取 N 个活动（0=不限）
```

## discount

```text
usage: sellerops discount [-h] [--apply] [--threshold THRESHOLD] [--all]
                          [--below BELOW] [--target TARGET] [--name NAME]
                          [--vc VC] [--prefix PREFIX] [--shops SHOPS]
                          [--limit LIMIT] [--chunk CHUNK]

options:
  -h, --help            show this help message and exit
  --apply               真正提交修改（默认 dry-run 预览）
  --threshold THRESHOLD
                        折扣阈值：只处理折扣>该值的商品（默认 50；指定 --all 或指定 --vc 时默认不限阈值）
  --all                 不限折扣阈值，处理指定条件下的所有在架商品（等价于 --threshold -1）
  --below BELOW         额外命中折扣 <N 的商品（与 --threshold 取并集，走 WB 折扣升序列表接口；例
                        --threshold 55 --below 40 = 折扣>55% 或 <40%；-1=不启用）
  --target TARGET       目标折扣（默认 50）
  --name NAME           商品价格表产品中文名包含匹配（如 笔记本电脑）
  --vc VC               指定单个或多个 vendorCode（逗号分隔）
  --prefix PREFIX       vendorCode 4位前缀码包含匹配（如 DSGI）
  --shops SHOPS         限定店铺 id 逗号分隔（默认全部凭证店铺）
  --limit LIMIT         每店最多处理 N 条（0=不限）
  --chunk CHUNK         每批提交数量（默认 100，最大 300）
```

## discount-bcs

```text
usage: sellerops discount-bcs [-h] [--apply] [--threshold THRESHOLD]
                              [--target TARGET] [--shops SHOPS]
                              [--limit LIMIT]

options:
  -h, --help            show this help message and exit
  --apply               真正提交（默认 dry-run）
  --threshold THRESHOLD
                        阈值：只处理折扣>该值的商品；全量设目标值用 --threshold -1
  --target TARGET       目标折扣
  --shops SHOPS         限定店铺 id 逗号分隔
  --limit LIMIT         每店最多处理 N 条（0=不限）
```

## price-review

```text
usage: sellerops price-review [-h] [--apply] [--shops SHOPS] [--limit LIMIT]

options:
  -h, --help     show this help message and exit
  --apply        真正应用新价格（默认 dry-run）
  --shops SHOPS  限定店铺 id 逗号分隔
  --limit LIMIT  每店最多审核 N 个（0=全部）
```

## orders

```text
usage: sellerops orders [-h] [--begin BEGIN] [--end END] [--days DAYS]
                        [--no-sync] [--shops SHOPS] [--page-size PAGE_SIZE]

options:
  -h, --help            show this help message and exit
  --begin BEGIN         开始日期 YYYY-MM-DD
  --end END             结束日期 YYYY-MM-DD
  --days DAYS           查询最近 N 天（--begin/--end 未指定时，默认 1 天）
  --no-sync             跳过订单同步，直接查缓存
  --shops SHOPS         限定店铺 id 逗号分隔（默认全部）
  --page-size PAGE_SIZE
                        分页每页条数
```

## mabang-orders

```text
usage: sellerops mabang-orders [-h] [--days DAYS] [--page-size PAGE_SIZE]
                               [--apply]

options:
  -h, --help            show this help message and exit
  --days DAYS           新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天
  --page-size PAGE_SIZE
                        订单列表每页条数
  --apply               真正更换错误匹配（默认 dry-run 只出报表）
```

## mabang-forecast

```text
usage: sellerops mabang-forecast [-h] [--days DAYS] [--page-size PAGE_SIZE]
                                 [--apply] [--check] [--status {1,3,99,5}]
                                 [--pages PAGES] [--wait WAIT]
                                 [--upload-timeout UPLOAD_TIMEOUT]
                                 [--poll-interval POLL_INTERVAL]
                                 [--upload-waiting]

options:
  -h, --help            show this help message and exit
  --days DAYS           新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天
  --page-size PAGE_SIZE
                        订单列表每页条数
  --apply               全链路执行：生成批次→上传→轮询等待上传结束→设置交运方式（各步幂等跳过已完成项）
  --check               只查询预报批次（默认四 tab 概览：待预报/预报成功/预报失败/历史预报）
  --status {1,3,99,5}   --check 只看单个 tab：1=待预报 / 3=预报成功 / 99=预报失败 / 5=历史预报（默认四
                        tab 概览）
  --pages PAGES         --check --status 时最多打印页数（默认 1；0=翻到底）
  --wait WAIT           轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）
  --upload-timeout UPLOAD_TIMEOUT
                        轮询等待新批次上传结束的最长秒数（默认读配置
                        mabang.upload_timeout_seconds，缺省 900）
  --poll-interval POLL_INTERVAL
                        批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）
  --upload-waiting      把待上传列表(status=1)中历史批次一并补传（补传后同样轮询等待上传结束）
```

## feishu-register

```text
usage: sellerops feishu-register [-h] [--url URL] [--table TABLE]
                                 [--scope {latest,pending,all}] [--date DATE]
                                 [--begin BEGIN] [--end END] [--days DAYS]
                                 [--page-size PAGE_SIZE] [--apply]
                                 [--no-pending]

options:
  -h, --help            show this help message and exit
  --url URL             飞书多维表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE         表格名（默认 订单登记）
  --scope {latest,pending,all}
                        latest=最近500条全状态去重只登新增（默认）；pending=待处理订单；all=全部状态订单（补录
                        历史，配合 --date 或 --begin/--end）
  --date DATE           单天日期 YYYY-MM-DD（--scope all 用）
  --begin BEGIN         开始日期 YYYY-MM-DD（--scope all 用）
  --end END             结束日期 YYYY-MM-DD（--scope all 用）
  --days DAYS           待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天；用于 pending 或 latest
                        合并待处理单
  --page-size PAGE_SIZE
                        订单列表每页条数
  --apply               真正写入（默认 dry-run 只列出将登记订单）
  --no-pending          不合并「待处理订单」数据源（默认 scope=latest 会合并 orderalllist +
                        待处理订单，让只匹配但未进预报/上传/交运流程的订单也能登记）
```

## feishu-register-dedup

```text
usage: sellerops feishu-register-dedup [-h] [--url URL] [--table TABLE]
                                       [--apply] [--limit LIMIT]

options:
  -h, --help     show this help message and exit
  --url URL      飞书多维表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE  表格名（默认 订单登记）
  --apply        真正删除重复记录（默认 dry-run 只出报告）
  --limit LIMIT  本次最多删除 N 行（0=不限）
```

## mabang-stock-register

```text
usage: sellerops mabang-stock-register [-h] [--url URL] [--table TABLE]
                                       [--apply]

options:
  -h, --help     show this help message and exit
  --url URL      飞书多维表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE  目标表名（默认 马帮库存登记表）
  --apply        真正全量重建（默认 dry-run 预览）
```

## mabang-stock-daily

```text
usage: sellerops mabang-stock-daily [-h] [--url URL] [--table TABLE]
                                    [--orders-table ORDERS_TABLE]
                                    [--date DATE] [--begin BEGIN] [--end END]
                                    [--apply]

options:
  -h, --help            show this help message and exit
  --url URL             飞书多维表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE         库存表名（默认 马帮库存登记表）
  --orders-table ORDERS_TABLE
                        订单明细表名（默认 订单登记）
  --date DATE           单天日期 YYYY-MM-DD（默认今天；显式给出即进入区间模式，会删除该日之前的旧日期列）
  --begin BEGIN         区间开始 YYYY-MM-DD（显式给出即删除该日之前的旧日期列，并补建至 --end 的缺列）
  --end END             区间结束 YYYY-MM-DD（必须与 --begin/--date 同用，单独使用直接报错）
  --apply               真正建列并填充（默认 dry-run 预览）
```

## feishu-vc-stats

```text
usage: sellerops feishu-vc-stats [-h] [--days DAYS] [--date DATE]
                                 [--begin BEGIN] [--end END] [--shops SHOPS]
                                 [--top TOP] [--by-prefix] [--no-cn]
                                 [--url URL] [--table TABLE]

options:
  -h, --help     show this help message and exit
  --days DAYS    近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）
  --date DATE    单天 YYYY-MM-DD（等价 --begin=--end）
  --begin BEGIN  区间开始 YYYY-MM-DD（单独给按单天处理）
  --end END      区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
  --shops SHOPS  店铺过滤，逗号分隔，支持店铺ID或短名（如 9352 或 袁州1；默认全部）
  --top TOP      控制台只显示前 N 名（0=全部；CSV 始终写全量）
  --by-prefix    按 vendorCode 的 4 位前缀码聚合（默认完整 vendorCode）
  --no-cn        不补全商品中文名（表内已有的仍会显示）
  --url URL      飞书表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE  表名（默认 订单登记）
```

## fbs-delivery

```text
usage: sellerops fbs-delivery [-h] [--shops SHOPS] [--days DAYS] [--date DATE]
                              [--begin BEGIN] [--end END]
                              [--item-state ITEM_STATE]
                              [--supply-state SUPPLY_STATE]
                              [--include-canceled] [--limit LIMIT]
                              [--max-pages MAX_PAGES] [--top TOP] [--no-cn]
                              [--interval INTERVAL]

options:
  -h, --help            show this help message and exit
  --shops SHOPS         店铺过滤，逗号分隔，支持店铺ID或短名（默认全部已填 cookie 店铺）
  --days DAYS           近 N 天（按清单 createdAt；0=不过滤，默认）
  --date DATE           单天 YYYY-MM-DD（等价 --begin=--end）
  --begin BEGIN         区间开始 YYYY-MM-DD（单独给按单天处理）
  --end END             区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
  --item-state ITEM_STATE
                        商品级状态过滤，逗号分隔（默认 shipment=待发货；all=不过滤；可选 cancel/sold/re
                        adyForPickup/reshipment/sorted/expectedSorting=等待分拣）
  --supply-state SUPPLY_STATE
                        清单级状态过滤，逗号分隔（默认不过滤；可选 complete/onway/reshipment/sort）
  --include-canceled    把 cancel 并入 --item-state（默认排除已取消）
  --limit LIMIT         每店最多处理 N 个清单（0=不限）
  --max-pages MAX_PAGES
                        清单/商品列表翻页安全上限（默认 50，防死循环）
  --top TOP             控制台按中文名只显示前 N 名（0=全部；CSV 始终全量）
  --no-cn               不解析商品中文名（供应商代码仍解析）
  --interval INTERVAL   清单间/店铺间请求间隔秒（默认 0.3）
```

## order-stats

```text
usage: sellerops order-stats [-h] [--shops SHOPS] [--days DAYS] [--date DATE]
                             [--begin BEGIN] [--end END] [--timezone TIMEZONE]
                             [--page-size PAGE_SIZE] [--max-pages MAX_PAGES]
                             [--limit LIMIT] [--top TOP] [--no-cn] [--quiet]
                             [--interval INTERVAL] [--out OUT] [--no-detail]
                             [--detail-max-rows DETAIL_MAX_ROWS]
                             [--no-local-filter]

options:
  -h, --help            show this help message and exit
  --shops SHOPS         店铺过滤，逗号分隔，支持店铺ID或短名（默认全部已填 cookie 店铺）
  --days DAYS           近 N 天（含今天，默认 30；仅当未给 --begin/--end/--date 时生效）
  --date DATE           单天 YYYY-MM-DD（等价 --begin=--end）
  --begin BEGIN         区间开始 YYYY-MM-DD（单独给按单天处理）
  --end END             区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
  --timezone TIMEZONE   接口时区（默认 America/Los_Angeles，对齐抓包；--days/--begin/--end
                        的日期口径同此，LA 日期比北京时间晚约 15 小时）
  --page-size PAGE_SIZE
                        每页条数（默认 50，抓包实测；服务端夹小会自动降级）
  --max-pages MAX_PAGES
                        每店翻页安全上限（默认 200，防死循环）
  --limit LIMIT         每店最多拉取 N 行明细（0=不限）
  --top TOP             控制台两张表（按中文名 / 按供应商代码）各只显示前 N 名（0=全部；xlsx 始终全量）
  --no-cn               不解析商品中文名（供应商代码仍解析）
  --quiet               不打印逐页翻页进度
  --interval INTERVAL   翻页/店铺间请求间隔秒（默认 0.3）
  --out OUT             自定义 xlsx 路径（默认 data/logs/订单动态统计_<时间戳>.xlsx）
  --no-detail           不输出「订单明细」Sheet（行数大时提速、减小文件）
  --detail-max-rows DETAIL_MAX_ROWS
                        「订单明细」Sheet 最多写 N 行（0=不限）
  --no-local-filter     不在本地按 created 复核时间窗（默认复核；越界行会显式告警）
```

## mabang-process

```text
usage: sellerops mabang-process [-h] [--days DAYS] [--page-size PAGE_SIZE]
                                [--wait WAIT]
                                [--upload-timeout UPLOAD_TIMEOUT]
                                [--poll-interval POLL_INTERVAL] [--url URL]
                                [--table TABLE] [--no-pending] [--apply]

options:
  -h, --help            show this help message and exit
  --days DAYS           配置店铺的全部新增待处理订单，0=不限日期（默认）；正数=最近 N 天
  --page-size PAGE_SIZE
                        订单列表分页大小（默认 100）
  --wait WAIT           轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）
  --upload-timeout UPLOAD_TIMEOUT
                        轮询等待新批次上传结束的最长秒数（默认读配置
                        mabang.upload_timeout_seconds，缺省 900）
  --poll-interval POLL_INTERVAL
                        批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）
  --url URL             飞书表格地址（可选，默认读配置 feishu.base_url）
  --table TABLE         飞书订单登记表名（默认 订单登记）
  --no-pending          飞书登记时不合并「待处理订单」数据源（默认合并，覆盖只匹配未进预报/上传/交运的订单）
  --apply               真正执行（默认 dry-run 预览）
```

## questions

```text
usage: sellerops questions [-h] [--shops SHOPS] [--reply REPLY] [--apply]
                           [--question-id QUESTION_ID] [--reply-all] [--yes]
                           [--no-detail]

options:
  -h, --help            show this help message and exit
  --shops SHOPS         限定店铺 id 逗号分隔
  --reply REPLY         回复内容（配合 --question-id 或 --reply-all）
  --apply               真正提交回复（默认预览）
  --question-id QUESTION_ID
                        回复指定提问 ID
  --reply-all           回复本店全部未处理提问（需 --yes）
  --yes                 确认回复全部（公开发言）
  --no-detail           跳过 WB 商品详情拉取（只显示本地中文名，速度快）
```

## questions-watch

```text
usage: sellerops questions-watch [-h] [--interval INTERVAL]
                                 [--mode {front,back}] [--apply]
                                 [--shops SHOPS] [--once]

options:
  -h, --help           show this help message and exit
  --interval INTERVAL  轮询间隔秒（默认 90，或 credentials 的 ai.watch_interval）
  --mode {front,back}  front=展示提问/商品信息；back=LLM 生成草稿，提交仍需 --apply
  --apply              生成并提交回复（同时选择 back；需配 ai.api_key）
  --shops SHOPS        限定店铺 id 逗号分隔
  --once               只跑一轮就退出（测试用）
```

## chat-watch

```text
usage: sellerops chat-watch [-h] [--interval INTERVAL] [--mode {front,back}]
                            [--apply] [--shops SHOPS] [--once] [--limit LIMIT]
                            [--history-limit HISTORY_LIMIT]
                            [--since-hours SINCE_HOURS]
                            [--max-turns MAX_TURNS]

options:
  -h, --help            show this help message and exit
  --interval INTERVAL   轮询间隔秒（默认取 credentials 的 ai.watch_interval，90）
  --mode {front,back}   front=展示买家消息/商品信息；back=LLM 生成草稿，发送仍需 --apply
  --apply               生成并发送回复（同时选择 back；需配 ai.api_key）
  --shops SHOPS         限定店铺 id 逗号分隔
  --once                只跑一轮就退出（测试用）
  --limit LIMIT         每店最多取 N 个会话（默认 20）
  --history-limit HISTORY_LIMIT
                        会话历史拉取条数（默认 50）
  --since-hours SINCE_HOURS
                        只处理最近 N 小时内的买家消息（默认 72；0=不限，会补回历史旧账）
  --max-turns MAX_TURNS
                        拼进提示词的最近对话轮数（默认 3）
```

## chats

```text
usage: sellerops chats [-h] [--shops SHOPS] [--limit LIMIT]
                       [--chat-id CHAT_ID] [--history HISTORY]
                       [--since-hours SINCE_HOURS] [--no-detail]

options:
  -h, --help            show this help message and exit
  --shops SHOPS         限定店铺 id 逗号分隔
  --limit LIMIT         每店最多取 N 个会话（默认 20）
  --chat-id CHAT_ID     只看指定会话 chatID
  --history HISTORY     展示最近 N 条消息历史（默认仅在指定 --chat-id 时展示 10 条）
  --since-hours SINCE_HOURS
                        只列出最新消息在最近 N 小时内的会话（0=不限，默认）
  --no-detail           跳过 WB 商品详情拉取（只显示本地中文名，速度快）
```

## appeals

```text
usage: sellerops appeals [-h] [--shops SHOPS] [--days DAYS]
                         [--days-max DAYS_MAX] [--type {in,out}]
                         [--limit LIMIT] [--no-cn]

options:
  -h, --help           show this help message and exit
  --shops SHOPS        限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）
  --days DAYS          仅命中剩余天数恰好=N 的未处理投诉（decide_counter，0=不筛选，默认）
  --days-max DAYS_MAX  仅命中剩余天数≤N 的未处理投诉（0=不筛选，默认；与 --days 同给时 --days 优先）
  --type {in,out}      投诉方向：in=发往本店（默认）/ out=本店发出
  --limit LIMIT        每店最多拉取 N 条投诉列表（0=不限，翻页到底）
  --no-cn              不解析商品中文名（供应商代码仍会解析；本地真源查不到的商品标注「本地真源未收录」）
```

## ratings

```text
usage: sellerops ratings [-h] [--shops SHOPS] [--rating RATING]
                         [--min-rating MIN_RATING] [--max-rating MAX_RATING]
                         [--one-star] [--two-star] [--negative]
                         [--stars STARS] [--min-one-star MIN_ONE_STAR]
                         [--min-feedbacks MIN_FEEDBACKS] [--days DAYS]
                         [--name NAME] [--prefix PREFIX] [--vc VC]
                         [--limit LIMIT] [--out OUT]

options:
  -h, --help            show this help message and exit
  --shops SHOPS         限定店铺 id 逗号分隔（默认全部）
  --rating RATING       精确筛选指定评分（如 1 或 1.0）
  --min-rating MIN_RATING
                        最低评分（>=）
  --max-rating MAX_RATING
                        最高评分（<=，如 1 或 2 筛选低分商品）
  --one-star            筛选存在 1 星评论的商品（oneStar >= 1）
  --two-star            筛选存在 2 星评论的商品（twoStar >= 1）
  --negative, --bad     筛选存在差评的商品（即 1 星或 2 星差评数 >= 1）
  --stars STARS         指定星级评论筛选（逗号分隔，如 1,2 筛选含 1 星或 2 星差评的商品）
  --min-one-star MIN_ONE_STAR
                        筛选 1 星评论数 >= N 的商品
  --min-feedbacks MIN_FEEDBACKS
                        最低评价总数（默认 1，仅看有评价商品；设为 0 含无评价）
  --days DAYS           统计天数跨度（默认 365 天）
  --name NAME           按产品中文名包含筛选
  --prefix PREFIX       按 4 位前缀码筛选
  --vc VC               按 vendorCode 筛选
  --limit LIMIT         每店最多拉取商品条数（0=不限）
  --out OUT             自定义导出文件路径（支持 .csv / .xlsx）
```

## ai-test

```text
usage: sellerops ai-test [-h] [--qa QA]

options:
  -h, --help  show this help message and exit
  --qa QA     测试数据集 json（默认 data/ai_test_qa.json）
```

## cookies-update

```text
usage: sellerops cookies-update [-h] md_file

positional arguments:
  md_file     含 fetch 块的 md 文件（相对路径从数据目录 har/ 读取，或传绝对路径）

options:
  -h, --help  show this help message and exit
```

## cookie-refresh-wb

当前未启用：Playwright 尚未启动，凭证按 [手动抓包流程](CONFIGURATION.md#手动抓包更新凭证当前方式) 更新；不能把保留的参数定义当作自动续期已部署。

```text
usage: sellerops cookie-refresh-wb [-h] [--shops SHOPS] [--headed] [--apply]

options:
  -h, --help     show this help message and exit
  --shops SHOPS  限定店铺 id 逗号分隔（默认全部 WB 店铺）
  --headed       有头浏览器人工登录（仅供后续另行启用浏览器方案时使用）
  --apply        实际执行刷新（默认 dry-run 预览）
```

## daily

```text
usage: sellerops daily [-h] [--apply] [--shops SHOPS] {morning,check}

positional arguments:
  {morning,check}

options:
  -h, --help       show this help message and exit
  --apply          执行每日流程（默认预览）
  --shops SHOPS    限定店铺 ID
```

## schedule

```text
usage: sellerops schedule [-h] [--remove] [--apply] [--yes]

options:
  -h, --help  show this help message and exit
  --remove    删除全部任务
  --apply     真正注册或删除任务
  --yes       确认删除任务
```
