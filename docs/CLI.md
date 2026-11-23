# 命令参考

由实际命令定义生成。使用方法见 [操作流程](USAGE.md)。
全局参数 `--data-dir` 可放在子命令前或后。商品计划单用 `apply --plan` 提交。

## shop list

查看本地店铺状态和快照时间

实现入口 [run_list](../src/sellerops/features/catalog/shops.py)

- `--name` 精确查找店名
- `--json` 输出单个 JSON 对象

## shop archive

将店铺软归档

实现入口 [run_status](../src/sellerops/features/catalog/shops.py)

- `--shop-id` 店铺 ID
- `--json` 输出单个 JSON 对象

## shop restore

恢复店铺

实现入口 [run_status](../src/sellerops/features/catalog/shops.py)

- `--shop-id` 店铺 ID
- `--json` 输出单个 JSON 对象

## catalog fetch

拉取商品快照

实现入口 [run_fetch](../src/sellerops/cli/catalog_handlers.py)

- 最多选一项 --shop-id
- `--shop-id` 店铺ID（默认全部）
- `--source` 快照来源（默认bcs快照；wb-openapi需显式选择官方读取，不自动回退）（bcs / wb-openapi）
- `--parallel` WB Open API并发店铺数（正整数，默认4；1为串行，单店及BCS来源不受影响）
- `--no-sync` BCS来源：跳过后台同步直接读取已同步快照；Open API来源无需同步

## catalog mapping

生成带图待认领工作台（同 map workbench）

实现入口 [run_workbench](../src/sellerops/features/catalog/workbench.py)

- `--json` 输出单个 JSON 对象

## catalog check-mismatch

货不对板大图画廊（同 map check-mismatch）

实现入口 [run_mismatch_check](../src/sellerops/cli/catalog_handlers.py)

- `--cn` 只渲染指定中文名（精确匹配，如『牙膏-紫色』）
- `--begin` 开始日期 YYYY-MM-DD（按商品创建时间筛选）
- `--end` 结束日期 YYYY-MM-DD（缺省=今天）
- `--days` 最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）

## catalog merge

保存待认领工作台审核结果（同 map batch-set --file）

实现入口 [run_batch_set](../src/sellerops/features/catalog/vc_mapping.py)

- `file` 审核结果 JSON 数组文件路径
- `--json` 输出单个 JSON 对象

## catalog rename

纠偏/修改商品中文名（按卖家 SKU 重绑）

实现入口 [run_mapping_rename](../src/sellerops/cli/catalog_handlers.py)

- `--vc` 要改名的 vendorCode
- `--cn` 新产品中文名
- `--reason` 改名原因说明
- `--file` 批量改名 JSON 文件路径（含 [{'vc': ..., 'cn': ...}]）

## catalog shop-mappings

刷新/生成各店铺独立映射表（exports/shop_*.xlsx）

实现入口 [run_shops_mapping](../src/sellerops/cli/catalog_handlers.py)

- `--shop-id` 指定店铺ID（默认全部活跃店铺）
- `--force` 兼容旧参数；导出总是读取当前库

## product price

改价或按显式价格意图设折扣（默认 WB 后台，默认预览）

实现入口 [run_price](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- 最多选一项 --price / --keep-price
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--yes` 兼容保留；确认计划单后，用 apply --plan 提交
- `--price` 指定正整数价格
- `--keep-price` 保持各规格原价，须同时指定折扣
- `--discount` 折扣 0-100，配合 --price 或 --keep-price
- `--club-discount` BCS 俱乐部折扣 0-100，须明确价格意图
- `--auto-review` 改价后自动「应用新价格」（降价 30-49.9% 进审查时，精确匹配刚改价商品）
- `--chunk` 每批提交条数（≤300，对齐 discount 实践，默认 100）
- `--interval` 批/店间请求间隔秒
- `--channel` WB 支持设置批次，BCS 使用固定批次；俱乐部折扣仅 BCS 支持（wb / bcs）

## product stock

预览库存修改计划（默认 WB 原生在线接口，归零用 --amount 0）

实现入口 [run_stock](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--yes` 兼容保留；确认计划单后，用 apply --plan 提交
- `--amount` 目标库存，归零传 0
- `--chunk` 每批提交条数（≤1000，探针实测 1000 可行，默认 500）
- `--interval` 批/店间请求间隔秒
- `--max-pages` WB 游标分页安全上限（--resolve live 时使用）
- `--resolve` snapshot=本地完整规格（默认，缺标识时官方API定向补齐）；live=另读莫斯科当前库存，缺库存记录不减少规格（snapshot / live）
- `--channel` WB 支持实时库存；BCS 仅支持快照和固定批次（wb / bcs）

## product trash

下架（默认 WB 原生 moveNmsToTrash 移回收站；先清库存；不可逆）

实现入口 [run_trash](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--yes` 兼容保留；确认计划单后，用 apply --plan 提交
- `--chunk` 每批移回收站条数（≤300，默认 300）
- `--interval` 批间间隔秒
- `--channel` 提交平台，默认 WB 后台（wb / bcs）

## product dimensions

批量修改尺寸（按价格表或 --dims 自定义，dry-run 默认，--apply 执行）

实现入口 [run](../src/sellerops/features/products/dimension.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--limit` 每店最多处理 N 条（0=不限）
- `--dims` 自定义尺寸 '长*宽*高/毛重'（例: 8*14*26/0.3）
- `--apply` 真正执行（默认 dry-run）

## product check-dimensions

查询 WB 包装尺寸/重量偏差待验证商品（只读）

实现入口 [run](../src/sellerops/features/products/dims_check.py)

- 最多选一项 --name / --name-contains
- `--type` dims=尺寸偏差 / weight=重量偏差 / all=两者合并去重（默认 dims）（dims / weight / all）
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--shops` 限定店铺 id 逗号分隔
- `--limit` 每店最多查询 N 条（0=不限）

## product banned

查询被阻止的商品；下架用 ops trash 生成计划单

实现入口 [run](../src/sellerops/features/products/banned.py)

- 必选其一 --shops / --all-shops
- `--apply` 兼容保留；下架用 ops trash 生成计划单，确认后用 apply --plan 提交
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--limit` 每店最多处理 N 条（0=不限）
- `--yes` 跳过不可逆确认（移到回收站）

## product clean

清理草稿箱/回收站

实现入口 [run](../src/sellerops/features/products/clean.py)

- 必选其一 --shops / --all-shops
- `--target` basket=回收站 / draft=草稿箱 / all=先草稿后回收站（basket / draft / all）
- `--apply` 真正执行（默认 dry-run）
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--limit` 每店最多处理 N 条，草稿和回收站共用总额，0 为不限
- `--yes` 确认永久删除所列商品

## product discount

设目标折扣，--threshold 只修正超过阈值的商品。BCS 仅支持 --all-products

实现入口 [run_discount](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--channel` （wb / bcs）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--target` 目标折扣，必填（0 至 100）
- `--threshold` 仅修改平台当前折扣严格高于此值的商品
- `--below` 也修改平台当前折扣严格低于此值的商品（WB）
- `--limit` 每店最多处理的商品数
- `--chunk` 每批商品数（仅 WB，默认 100）

## product review-price

预览价格审核计划（降价 30-49.9% 进审查的商品）

实现入口 [run](../src/sellerops/features/products/price_review.py)

- 必选其一 --shops / --all-shops
- `--apply` 兼容保留；确认价格审核计划单后，用 apply --plan 提交
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--limit` 每店最多审核 N 个（0=全部）

## listing replicate

跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认）

实现入口 [run](../src/sellerops/features/publishing/replicate.py)

- 必选其一 --vc / --prefix / --name / --name-contains / --all-products
- 必选其一 --shops / --all-shops
- `--vc` 完整 VC
- `--prefix` 四位前缀
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--all-products` 源店全部商品
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--source-shops` 源店铺 id 逗号分隔（默认全部本地店铺，按完整 VC 去重）
- `--price-source` 价格来源：snapshot=源店快照价格；minimum=商品价格表最低售价（snapshot / minimum）
- `--price-multiplier` 价格来源乘数，按分四舍五入（默认 1）
- `--stock` 上架库存（默认999，可显式设为0）
- `--limit` 最多处理 N 个 vc（0=不限）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--interval` 批次上架请求间隔秒
- `--cn-stock` 按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）

## listing publish

批量发布上架：输入 WB 商品码推送到店铺（支持指定前缀/完整VC/价格/尺寸/店铺）

实现入口 [run](../src/sellerops/features/publishing/bulk_publish.py)

- 必选其一 --shops / --all-shops
- `nms` WB 商品码列表（支持空格或逗号分隔，如 123456789 123456790）
- `--nm` WB 商品码（逗号分隔多个，兼容命令行参数传参）
- `--vc` 完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）
- `--prefix` 4位前缀码（如 ABCD 或 BCS-ABCD）
- `--sku` 商品价格表中的卖家 SKU，适用于本次输入商品
- `--random-prefix` 没有唯一登记前缀时，明确允许在预览时随机生成
- `--price` 指定售价（CNY），不填时使用本地快照价
- `--dims` 自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）
- `--length` 包装长 (cm)
- `--width` 包装宽 (cm)
- `--height` 包装高 (cm)
- `--weight` 包装毛重 (kg)
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--stock` 上架库存量（默认 999）
- `--cn` 商品中文名（辅助匹配价格与包装规格）
- `--file` 批量上架文件路径（支持 .txt 每行一个商品码、.json 商品列表或 .csv 表格）
- `--cn-stock` 按中文名指定库存（'中文名:库存,...'）
- `--interval` 批次/商品请求间隔秒（默认 1.0）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交

## listing create-cards

建卡发布上架：输入 WB 商品码推送到店铺（支持自定义完整VC/俄文详情与主图建卡）

实现入口 [run](../src/sellerops/features/publishing/card_publish.py)

- 必选其一 --shops / --all-shops
- `nms` WB 商品码列表（支持空格或逗号分隔，如 123456789 123456790）
- `--nm` WB 商品码（逗号分隔多个，兼容命令行参数传参）
- `--vc` 完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）
- `--prefix` 4位前缀码（如 ABCD 或 BCS-ABCD）
- `--sku` 商品价格表中的卖家 SKU，适用于本次输入商品
- `--random-prefix` 没有唯一登记前缀时，明确允许在预览时随机生成
- `--price` 指定售价（CNY），不填时使用本地快照价
- `--dims` 自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）
- `--length` 包装长 (cm)
- `--width` 包装宽 (cm)
- `--height` 包装高 (cm)
- `--weight` 包装毛重 (kg)
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--stock` 上架库存量（默认 999）
- `--cn` 商品中文名（辅助匹配价格与包装规格）
- `--file` 批量上架文件路径（支持 .txt 每行一个商品码、.json 商品列表或 .csv 表格）
- `--cn-stock` 按中文名指定库存（'中文名:库存,...'）
- `--interval` 批次/商品请求间隔秒（默认 1.0）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交

## listing publish-openapi

[备用] WB Open API 上架：建卡、有限等待标识、上传图片和莫斯科库存；支持续办

实现入口 [run](../src/sellerops/features/publishing/open_api_publish.py)

- `--source-shop` 自有源店 BCS 店铺 ID；不传则从公开 CDN 读取任意商品码
- `--nm` 源 WB 商品码，逗号分隔；自有源店模式使用该店商品码
- `--vc` 源店模式为完整 VC 筛选；公开商品模式指定目标完整 VC（支持 {nm} 模板）
- `--all` 源店全部非回收站商品；仅自有源店模式
- `--file` 公开商品 JSON 对象数组：nmId/vendorCode/price，可用 card 覆盖官方字段
- `--shops` 目标店铺 ID，逗号分隔；不可包含源店
- `--price` 新建全部规格的整数价格（目标店币种）；文件可逐项指定 price
- `--dims` 覆盖包装尺寸：长*宽*高/毛重，单位 cm/kg
- `--stock` 莫斯科仓各规格库存（默认 999，0 保留）
- `--limit` 最多选择 N 个源商品（0=不限）
- `--resume` 按 --shops/--vc 续办已有记录，不重新建卡；沿用原图片和库存计划
- `--wait-timeout` 等待完整商品/规格标识的总时限秒（默认1800，0=只查询一次，最多7200）
- `--wait-interval` 标识查询轮次间隔秒（默认15，范围1..60；仍遵守接口限流）
- `--apply` 兼容保留；确认上架或续办计划单后，用 apply --plan 提交

## listing normalize-vc

WB Open API：纠正历史 VC 前缀，保留原尾部，冲突时追加跨店一致序号

实现入口 [run](../src/sellerops/features/publishing/vc_normalization.py)

- 必选其一 --vc / --name / --name-contains / --all
- 必选其一 --shops / --all-shops
- `--vc` 完整旧 VC，逗号分隔
- `--name` 已确认的完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--all` 全部非回收站商品；无明确归属的项记录阻止原因
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--apply` 兼容保留；确认改码计划单后，用 apply --plan 提交

## listing sync-cards

WB Open API：按相同完整 VC 将源店商品内容同步到其他店

实现入口 [run](../src/sellerops/features/publishing/open_api_sync.py)

- 必选其一 --vc / --nm / --all
- 必选其一 --shops / --all-shops
- `--source-shop` 修改后的源店 BCS 店铺 ID（例如示例店铺对应的 1001）
- `--vc` 源店完整 VC，逗号分隔
- `--nm` 源店 WB 商品码，逗号分隔
- `--all` 源店全部非回收站商品
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--fields` 同步字段：title,description,brand,dimensions,characteristics,photos（默认）；可选 sizes,documents,video,gtin,kizMarked
- `--limit` 最多选择 N 个源商品（0=不限）
- `--apply` 真正提交内容/媒体更新（默认预览）

## marketing enroll

促销报名（cookie 会话，applyAll）

实现入口 [run](../src/sellerops/features/marketing/promo.py)

- `--apply` 真正报名（默认 dry-run 预览）
- `--shops` 限定店铺 id 逗号分隔
- `--days` 查询结束=N天后
- `--days-back` 查询起始=N天前
- `--sleep` 活动间请求间隔秒

## marketing ads

查询 WB 广告推广中被推广的商品（中文名/供应商代码/WB商品码，只读）

实现入口 [run](../src/sellerops/features/marketing/adverts.py)

- `--shops` 限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）
- `--status` 活动状态ID逗号分隔（默认 4,9,11 = 后台默认视图）
- `--page-size` 活动列表分页大小（默认 100）
- `--max-pages` 活动列表最多翻页数（安全上限，默认 50）
- `--limit` 每店最多拉取 N 个活动（0=不限，翻页到底）
- `--no-cn` 不解析商品中文名（供应商代码仍解析）

## marketing audit

只读：推广 × 销量双向错配审计（gap=该推没推 / waste=在推但没销量该关）

实现入口 [run](../src/sellerops/features/marketing/promo_gap.py)

- `--mode` 审计方向：gap=该推没推(默认) / waste=该关的(在推广但本店单数<阈值) / both=两个都出（gap / waste / both）
- `--shops` 限定店铺，逗号分隔，支持店铺ID或短名（如 1001 或 示例店铺；默认全部已填 cookie 店铺）
- `--min` 单数阈值（默认 4）：gap=单数≥N；waste=单数<N
- `--days` 近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）
- `--date` 单天 YYYY-MM-DD（等价 --begin=--end）
- `--begin` 区间开始 YYYY-MM-DD（单独给按单天处理）
- `--end` 区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
- `--status` 推广活动状态ID逗号分隔（默认 4,9,11 = 含暂停；只算在投用 --status 9）
- `--listed-only` [gap] 只输出「可直接补推」组（默认两组都出）
- `--top` 每组控制台只显示前 N 名（0=全部；CSV 始终写全量）
- `--no-cn` 不补全商品中文名（飞书表内已有的仍显示）
- `--url` 飞书表格地址（可选，默认读配置 feishu.base_url）
- `--table` 飞书表名（默认 订单登记）
- `--page-size` 推广活动列表分页大小（默认 100）
- `--max-pages` 推广活动列表最多翻页数（默认 50）
- `--limit` 每店最多拉取 N 个活动（0=不限）

## order list

订单查询（自动同步 + 查询指定日期区间）

实现入口 [run](../src/sellerops/features/orders/orders.py)

- `--begin` 开始日期 YYYY-MM-DD
- `--end` 结束日期 YYYY-MM-DD
- `--days` 查询最近 N 天（--begin/--end 未指定时，默认 1 天）
- `--no-sync` 跳过订单同步，直接查缓存
- `--shops` 限定店铺 id 逗号分隔（默认全部）
- `--page-size` 分页每页条数

## order match

按价格表发货方式分流待处理订单，匹配/更换马帮库存SKU

实现入口 [run](../src/sellerops/features/orders/matching.py)

- `--days` 新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天
- `--page-size` 订单列表每页条数
- `--apply` 真正更换错误匹配（默认 dry-run 只出报表）

## order forecast

马帮预报批次：已匹配商品订单生成预报批次并上传（dry-run 默认；--check 查批次状态）

实现入口 [run_forecast](../src/sellerops/features/orders/forecast.py)

- `--days` 新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天
- `--page-size` 订单列表每页条数
- `--apply` 全链路执行：生成批次→上传→轮询等待上传结束→设置交运方式（各步幂等跳过已完成项）
- `--check` 只查询预报批次（默认四 tab 概览：待预报/预报成功/预报失败/历史预报）
- `--status` --check 只看单个 tab：1=待预报 / 3=预报成功 / 99=预报失败 / 5=历史预报（默认四 tab 概览）（1 / 3 / 99 / 5）
- `--pages` --check --status 时最多打印页数（默认 1；0=翻到底）
- `--wait` 轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）
- `--upload-timeout` 轮询等待新批次上传结束的最长秒数（默认读配置 mabang.upload_timeout_seconds，缺省 900）
- `--poll-interval` 批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）
- `--upload-waiting` 把待上传列表(status=1)中历史批次一并补传（补传后同样轮询等待上传结束）

## order register

马帮订单登记到飞书多维表格（按订单编号去重，dry-run 默认）

实现入口 [run](../src/sellerops/features/orders/feishu_register.py)

- `--url` 飞书多维表格地址（可选，默认读配置 feishu.base_url）
- `--table` 表格名（默认 订单登记）
- `--scope` latest=最近500条全状态去重只登新增（默认）；pending=待处理订单；all=全部状态订单（补录历史，配合 --date 或 --begin/--end）（latest / pending / all）
- `--date` 单天日期 YYYY-MM-DD（--scope all 用）
- `--begin` 开始日期 YYYY-MM-DD（--scope all 用）
- `--end` 结束日期 YYYY-MM-DD（--scope all 用）
- `--days` 待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天；用于 pending 或 latest 合并待处理单
- `--page-size` 订单列表每页条数
- `--apply` 真正写入（默认 dry-run 只列出将登记订单）
- `--no-pending` 不合并「待处理订单」数据源（默认 scope=latest 会合并 orderalllist + 待处理订单，让只匹配但未进预报/上传/交运流程的订单也能登记）

## order deduplicate

存量修复：「订单登记」表内重复订单编号清理（按订单编号分组，每组保留表内最早一条、其余删除；dry-run 默认）

实现入口 [run](../src/sellerops/features/orders/feishu_register_dedup.py)

- `--url` 飞书多维表格地址（可选，默认读配置 feishu.base_url）
- `--table` 表格名（默认 订单登记）
- `--apply` 真正删除重复记录（默认 dry-run 只出报告）
- `--limit` 本次最多删除 N 行（0=不限）

## order stock-register

拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」（含附件列「图」）

实现入口 [run](../src/sellerops/features/orders/mabang_stock.py)

- `--url` 飞书多维表格地址（可选，默认读配置 feishu.base_url）
- `--table` 目标表名（默认 马帮库存登记表）
- `--apply` 真正全量重建（默认 dry-run 预览）

## order stock-days

「马帮库存登记表」日期列管理：默认只建今天列+更新全部已有日期列；--begin/--date 任一显式给出即进入区间模式（删早于该日的旧列+补建区间缺列，--end 需同用）

实现入口 [run_daily](../src/sellerops/features/orders/mabang_stock_daily.py)

- `--url` 飞书多维表格地址（可选，默认读配置 feishu.base_url）
- `--table` 库存表名（默认 马帮库存登记表）
- `--orders-table` 订单明细表名（默认 订单登记）
- `--date` 单天日期 YYYY-MM-DD（默认今天；显式给出即进入区间模式，会删除该日之前的旧日期列）
- `--begin` 区间开始 YYYY-MM-DD（显式给出即删除该日之前的旧日期列，并补建至 --end 的缺列）
- `--end` 区间结束 YYYY-MM-DD（必须与 --begin/--date 同用，单独使用直接报错）
- `--apply` 真正建列并填充（默认 dry-run 预览）

## order group-ship

WB 中国区 FBS 发货群供货：按店铺和中文名建供货、转配送并导出 PDF

实现入口 [run](../src/sellerops/features/orders/fbs_group_ship.py)

- `--shops` 限定店铺 ID，逗号分隔（默认 BCS 全部店铺）
- `--name` 精确限定一个商品中文名（默认全部标记商品）
- `--resume` 查看已受理配送的待导出供货；加 --apply 补取 PDF
- `--apply` 执行供货和导出（默认预览）；与 --resume 同用时仅补取 PDF

## order process

马帮订单处理一体：匹配商品→预报单→上传（自动发货）→物流交运→飞书登记（登记含只匹配未进预报流程的订单）

实现入口 [run](../src/sellerops/features/orders/mabang_process.py)

- `--days` 配置店铺的全部新增待处理订单，0=不限日期（默认）；正数=最近 N 天
- `--page-size` 订单列表分页大小（默认 100）
- `--wait` 轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）
- `--upload-timeout` 轮询等待新批次上传结束的最长秒数（默认读配置 mabang.upload_timeout_seconds，缺省 900）
- `--poll-interval` 批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）
- `--url` 飞书表格地址（可选，默认读配置 feishu.base_url）
- `--table` 飞书订单登记表名（默认 订单登记）
- `--no-pending` 飞书登记时不合并「待处理订单」数据源（默认合并，覆盖只匹配未进预报/上传/交运的订单）
- `--apply` 真正执行（默认 dry-run 预览）

## report mapping

从本地库导出价格映射总表或单店表

实现入口 [run_export](../src/sellerops/features/catalog/mapping_sync.py)

- `--shop-id` 导出指定店铺；省略时导出总表
- `--out` 输出路径（默认 exports/ 下的 .xlsx）
- `--json` 输出单个 JSON 对象

## report products

只读：默认筛选低评分、高退货率及近期出单补货候选 → Excel

实现入口 [run](../src/sellerops/features/reports/product_report.py)

- `--details` 追加原始汇总、订单、评价和链接证据表（默认仅简洁筛选与覆盖说明）
- `--shops` 本机目标店铺ID，逗号分隔（默认本机BCS账号全部店铺；不筛选临时账号或飞书来源）
- `--extra-bcs-stdin` 从标准输入读取临时BCS账号的一个或多个fetch抓包，仅补充统计，凭证不落盘
- `--order-days` 退货统计下单窗口，含今天（1–31天，默认30）
- `--feedback-days` 评价日期窗口，含今天（默认0：接口可访问的全部历史）
- `--wb-region` WB官方主机区域（默认ru；不自动跨域重试）（ru / cn）
- `--url` 飞书多维表格地址（默认feishu.base_url）
- `--table` 飞书来源表（默认订单登记）
- `--out` 输出.xlsx路径（默认exports/商品运营报告_时间.xlsx）
- `--json` 输出单个 JSON 对象

## report vc-sales

只读：飞书「订单登记」按供应商代码(BCS编号)统计单数并降序（跨店合并；支持店铺/日期过滤）

实现入口 [run](../src/sellerops/features/reports/feishu_vc_stats.py)

- `--days` 近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）
- `--date` 单天 YYYY-MM-DD（等价 --begin=--end）
- `--begin` 区间开始 YYYY-MM-DD（单独给按单天处理）
- `--end` 区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
- `--shops` 店铺过滤，逗号分隔，支持店铺ID或短名（如 1001 或 示例店铺；默认全部）
- `--top` 控制台只显示前 N 名（0=全部；CSV 始终写全量）
- `--by-prefix` 按 vendorCode 的 4 位前缀码聚合（默认完整 vendorCode）
- `--no-cn` 不补全商品中文名（表内已有的仍会显示）
- `--url` 飞书表格地址（可选，默认读配置 feishu.base_url）
- `--table` 表名（默认 订单登记）
- `--json` 输出单个 JSON 对象

## report delivery

只读：WB FBS「配送中」清单及其全部商品采集+统计（按清单 createdAt 时段、商品状态默认 shipment=待发货；控制台汇总 + CSV 明细）

实现入口 [run](../src/sellerops/features/reports/fbs_delivery.py)

- `--shops` 店铺过滤，逗号分隔，支持店铺ID或短名（默认全部已填 cookie 店铺）
- `--days` 近 N 天（按清单 createdAt；0=不过滤，默认）
- `--date` 单天 YYYY-MM-DD（等价 --begin=--end）
- `--begin` 区间开始 YYYY-MM-DD（单独给按单天处理）
- `--end` 区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
- `--item-state` 商品级状态过滤，逗号分隔（默认 shipment=待发货；all=不过滤；可选 cancel/sold/readyForPickup/reshipment/sorted/expectedSorting=等待分拣）
- `--supply-state` 清单级状态过滤，逗号分隔（默认不过滤；可选 complete/onway/reshipment/sort）
- `--include-canceled` 把 cancel 并入 --item-state（默认排除已取消）
- `--limit` 每店最多处理 N 个清单（0=不限）
- `--max-pages` 清单/商品列表翻页安全上限（默认 50，防死循环）
- `--top` 控制台按中文名只显示前 N 名（0=全部；CSV 始终全量）
- `--no-cn` 不解析商品中文名（供应商代码仍解析）
- `--interval` 清单间/店铺间请求间隔秒（默认 0.3）
- `--json` 输出单个 JSON 对象

## report orders

只读：WB「订单动态」近一月订单按商品中文名统计（订单数/退货数/退货率/完单口径退货率/未完结）→ xlsx 多 Sheet（汇总 + 按供应商代码 + 每店 + 订单明细）

实现入口 [run](../src/sellerops/features/reports/order_stats.py)

- `--shops` 店铺过滤，逗号分隔，支持店铺ID或短名（默认全部已填 cookie 店铺）
- `--days` 近 N 天（含今天，默认 30；仅当未给 --begin/--end/--date 时生效）
- `--date` 单天 YYYY-MM-DD（等价 --begin=--end）
- `--begin` 区间开始 YYYY-MM-DD（单独给按单天处理）
- `--end` 区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）
- `--timezone` 接口时区（默认 America/Los_Angeles，对齐抓包；--days/--begin/--end 的日期口径同此，LA 日期比北京时间晚约 15 小时）
- `--page-size` 每页条数（默认 50，抓包实测；服务端夹小会自动降级）
- `--max-pages` 每店翻页安全上限（默认 200，防死循环）
- `--limit` 每店最多拉取 N 行明细（0=不限）
- `--top` 控制台两张表（按中文名 / 按供应商代码）各只显示前 N 名（0=全部；xlsx 始终全量）
- `--no-cn` 不解析商品中文名（供应商代码仍解析）
- `--quiet` 不打印逐页翻页进度
- `--interval` 翻页/店铺间请求间隔秒（默认 0.3）
- `--out` 自定义 xlsx 路径（默认 exports/订单动态统计_<时间戳>.xlsx）
- `--no-detail` 不输出「订单明细」Sheet（行数大时提速、减小文件）
- `--detail-max-rows` 「订单明细」Sheet 最多写 N 行（0=不限）
- `--no-local-filter` 不在本地按 created 复核时间窗（默认复核；越界行会显式告警）
- `--json` 输出单个 JSON 对象

## report ratings

查询商品链接评分与评价情况（只读；支持按评分/店铺/1星评论筛选，导出包含价格表数据的报表）

实现入口 [run](../src/sellerops/features/reports/ratings.py)

- 最多选一项 --name / --name-contains
- `--shops` 限定店铺 id 逗号分隔（默认全部）
- `--rating` 精确筛选指定评分（如 1 或 1.0）
- `--min-rating` 最低评分（>=）
- `--max-rating` 最高评分（<=，如 1 或 2 筛选低分商品）
- `--one-star` 筛选存在 1 星评论的商品（oneStar >= 1）
- `--two-star` 筛选存在 2 星评论的商品（twoStar >= 1）
- `--negative, --bad` 筛选存在差评的商品（即 1 星或 2 星差评数 >= 1）
- `--stars` 指定星级评论筛选（逗号分隔，如 1,2 筛选含 1 星或 2 星差评的商品）
- `--min-one-star` 筛选 1 星评论数 >= N 的商品
- `--min-feedbacks` 最低评价总数（默认 1，仅看有评价商品；设为 0 含无评价）
- `--days` 统计天数跨度（默认 365 天）
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 按 4 位前缀码筛选
- `--vc` 按 vendorCode 筛选
- `--limit` 每店最多拉取商品条数（0=不限）
- `--out` 自定义导出文件路径（支持 .csv / .xlsx）
- `--json` 输出单个 JSON 对象

## support questions

买家未处理提问查询与回复

实现入口 [run](../src/sellerops/features/support/questions.py)

- `--shops` 限定店铺 id 逗号分隔
- `--reply` 回复内容（配合 --question-id 或 --reply-all）
- `--apply` 真正提交回复（默认预览）
- `--question-id` 回复指定提问 ID
- `--reply-all` 回复本店全部未处理提问（需 --yes）
- `--yes` 确认回复全部（公开发言）
- `--no-detail` 跳过 WB 商品详情拉取（只显示本地中文名，速度快）

## support watch-questions

买家提问实时监听（front=前台AI打印提问/商品信息+手动回复，back=后台AI LLM 自动回复）

实现入口 [run](../src/sellerops/features/support/questions_watch.py)

- `--interval` 轮询间隔秒（默认 90，或 credentials 的 ai.watch_interval）
- `--mode` front=展示提问/商品信息；back=LLM 生成草稿，提交仍需 --apply（front / back）
- `--apply` 提交后台生成的回复；单独使用时选择 back，不能与 front 同用
- `--shops` 限定店铺 id 逗号分隔
- `--once` 只跑一轮就退出（测试用）

## support watch-chats

AI 售后客服：买家聊天实时监听（front=前台AI展示/手动，back=后台AI自动回复；默认只展示）

实现入口 [run](../src/sellerops/features/support/chat_watch.py)

- `--interval` 轮询间隔秒（默认取 credentials 的 ai.watch_interval，90）
- `--mode` front=展示买家消息/商品信息；back=LLM 生成草稿，发送仍需 --apply（front / back）
- `--apply` 发送后台生成的回复；单独使用时选择 back，不能与 front 同用
- `--shops` 限定店铺 id 逗号分隔
- `--once` 只跑一轮就退出（测试用）
- `--limit` 每店最多取 N 个会话（默认 20）
- `--history-limit` 会话历史拉取条数（默认 50）
- `--since-hours` 只处理最近 N 小时内的买家消息（默认 72；0=不限，会补回历史旧账）
- `--max-turns` 拼进提示词的最近对话轮数（默认 3）

## support chats

查询 WB 买家聊天会话与消息历史（只读，用于验证/排障）

实现入口 [run](../src/sellerops/features/support/chat_inspect.py)

- `--shops` 限定店铺 id 逗号分隔
- `--limit` 每店最多取 N 个会话（默认 20）
- `--chat-id` 只看指定会话 chatID
- `--history` 展示最近 N 条消息历史（默认仅在指定 --chat-id 时展示 10 条）
- `--since-hours` 只列出最新消息在最近 N 小时内的会话（0=不限，默认）
- `--no-detail` 跳过 WB 商品详情拉取（只显示本地中文名，速度快）

## support appeals

查询 WB 平台投诉单（只读；未处理/剩余天数筛选，输出去重商品编号与供应商代码）

实现入口 [run](../src/sellerops/features/support/complaints.py)

- `--shops` 限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）
- `--days` 仅命中剩余天数恰好=N 的未处理投诉（decide_counter，0=不筛选，默认）
- `--days-max` 仅命中剩余天数≤N 的未处理投诉（0=不筛选，默认；与 --days 同给时 --days 优先）
- `--type` 投诉方向：in=发往本店（默认）/ out=本店发出（in / out）
- `--limit` 每店最多拉取 N 条投诉列表（0=不限，翻页到底）
- `--no-cn` 不解析商品中文名（供应商代码仍会解析；本地真源查不到的商品标注「本地真源未收录」）

## support ai-test

使用本地问答集调用 LLM 对照生成回复（会联网）

实现入口 [run](../src/sellerops/features/support/ai_reply_test.py)

- `--qa` 测试数据集 json（默认 data/ai_test_qa.json）

## task run

每日任务（morning=促销报名+生成折扣计划 / adjust-discounts=只生成折扣计划）

实现入口 [run_daily_cmd](../src/sellerops/features/system/daily.py)

- 必选其一 --shops / --all-shops
- `mode` （morning / adjust-discounts）
- `--apply` 运行每日流程（默认只列步骤）；折扣计划仍需确认后用 apply --plan 提交
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺

## task schedule

创建/删除 Windows 计划任务（默认预览，--apply 执行）

实现入口 [run](../src/sellerops/features/system/schedule.py)

- 最多选一项 --shops / --all-shops
- `--shops` 注册在这些店铺运行的任务
- `--all-shops` 注册覆盖全部配置店铺的任务
- `--remove` 删除全部任务
- `--apply` 真正注册或删除任务
- `--yes` 确认删除任务

## task clean

按保留天数清理日志、交付物和临时目录（默认预览）

实现入口 [run](../src/sellerops/features/system/clean.py)

- `--days` 保留最近 N 天，至少 1 天（默认 20）
- `--apply` 删除过期文件和空子目录
- `--json` 输出单个 JSON 对象

## map candidates

查看待认领分组、唯一前缀和最低售价倍数参考

实现入口 [run_candidates](../src/sellerops/features/catalog/vc_mapping.py)

- `--limit` 最多返回的候选组数
- `--json` 输出单个 JSON 对象

## map workbench

生成带图待认领工作台（exports/workbench/）

实现入口 [run_workbench](../src/sellerops/features/catalog/workbench.py)

- `--json` 输出单个 JSON 对象

## map batch-set

按完整 VC 导入工作台审核结果或批量确认

实现入口 [run_batch_set](../src/sellerops/features/catalog/vc_mapping.py)

- `--file` 审核结果 JSON 数组文件路径，省略时从标准输入读取
- `--json` 输出单个 JSON 对象

## map check-mismatch

生成货不对板大图画廊（按中文名分组并标记异常）

实现入口 [run_mismatch_check](../src/sellerops/cli/catalog_handlers.py)

- `--cn` 只渲染指定中文名（精确匹配，如『牙膏-紫色』）
- `--begin` 开始日期 YYYY-MM-DD（按商品创建时间筛选）
- `--end` 结束日期 YYYY-MM-DD（缺省=今天）
- `--days` 最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）
- `--json` 输出单个 JSON 对象

## doctor

快速体检

实现入口 [run](../src/sellerops/features/system/doctor.py)

- `--json` 输出单个 JSON 对象
- `--check-online` 显式进行只读鉴权

## sync

同步商品快照

实现入口 [run_fetch](../src/sellerops/cli/catalog_handlers.py)

- 最多选一项 --all / --shop-id
- `--all` 同步全部在营店铺
- `--shop-id` 店铺ID（默认全部）
- `--source` 快照来源（默认bcs快照；wb-openapi需显式选择官方读取，不自动回退）（bcs / wb-openapi）
- `--parallel` WB Open API并发店铺数（正整数，默认4；1为串行，单店及BCS来源不受影响）
- `--no-sync` BCS来源：跳过后台同步直接读取已同步快照；Open API来源无需同步
- `--json` 输出单个 JSON 对象

## ops price

改价或按显式价格意图设折扣（默认 WB 后台，默认预览）

实现入口 [run_price](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- 最多选一项 --price / --keep-price
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--yes` 兼容保留；确认计划单后，用 apply --plan 提交
- `--price` 指定正整数价格
- `--keep-price` 保持各规格原价，须同时指定折扣
- `--discount` 折扣 0-100，配合 --price 或 --keep-price
- `--club-discount` BCS 俱乐部折扣 0-100，须明确价格意图
- `--auto-review` 改价后自动「应用新价格」（降价 30-49.9% 进审查时，精确匹配刚改价商品）
- `--chunk` 每批提交条数（≤300，对齐 discount 实践，默认 100）
- `--interval` 批/店间请求间隔秒
- `--channel` WB 支持设置批次，BCS 使用固定批次；俱乐部折扣仅 BCS 支持（wb / bcs）
- `--json` 输出单个 JSON 对象

## ops stock

预览库存修改计划（默认 WB 原生在线接口，归零用 --amount 0）

实现入口 [run_stock](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--yes` 兼容保留；确认计划单后，用 apply --plan 提交
- `--amount` 目标库存，归零传 0
- `--chunk` 每批提交条数（≤1000，探针实测 1000 可行，默认 500）
- `--interval` 批/店间请求间隔秒
- `--max-pages` WB 游标分页安全上限（--resolve live 时使用）
- `--resolve` snapshot=本地完整规格（默认，缺标识时官方API定向补齐）；live=另读莫斯科当前库存，缺库存记录不减少规格（snapshot / live）
- `--channel` WB 支持实时库存；BCS 仅支持快照和固定批次（wb / bcs）
- `--json` 输出单个 JSON 对象

## ops trash

下架（默认 WB 原生 moveNmsToTrash 移回收站；先清库存；不可逆）

实现入口 [run_trash](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--yes` 兼容保留；确认计划单后，用 apply --plan 提交
- `--chunk` 每批移回收站条数（≤300，默认 300）
- `--interval` 批间间隔秒
- `--channel` 提交平台，默认 WB 后台（wb / bcs）
- `--json` 输出单个 JSON 对象

## ops discount

设目标折扣，--threshold 只修正超过阈值的商品。BCS 仅支持 --all-products

实现入口 [run_discount](../src/sellerops/cli/product.py)

- 必选其一 --shops / --all-shops
- 必选其一 --sku / --name / --name-contains / --prefix / --vc / --all-products
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--sku` 商品价格表卖家 SKU
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--prefix` 商品价格表前缀码
- `--vc` 完整 VC，多个用逗号分隔
- `--all-products` 目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）
- `--channel` （wb / bcs）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--target` 目标折扣，必填（0 至 100）
- `--threshold` 仅修改平台当前折扣严格高于此值的商品
- `--below` 也修改平台当前折扣严格低于此值的商品（WB）
- `--limit` 每店最多处理的商品数
- `--chunk` 每批商品数（仅 WB，默认 100）
- `--json` 输出单个 JSON 对象

## ops review-price

预览价格审核计划（降价 30-49.9% 进审查的商品）

实现入口 [run](../src/sellerops/features/products/price_review.py)

- 必选其一 --shops / --all-shops
- `--apply` 兼容保留；确认价格审核计划单后，用 apply --plan 提交
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--limit` 每店最多审核 N 个（0=全部）
- `--json` 输出单个 JSON 对象

## ops replicate

跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认）

实现入口 [run](../src/sellerops/features/publishing/replicate.py)

- 必选其一 --vc / --prefix / --name / --name-contains / --all-products
- 必选其一 --shops / --all-shops
- `--vc` 完整 VC
- `--prefix` 四位前缀
- `--name` 完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--all-products` 源店全部商品
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--source-shops` 源店铺 id 逗号分隔（默认全部本地店铺，按完整 VC 去重）
- `--price-source` 价格来源：snapshot=源店快照价格；minimum=商品价格表最低售价（snapshot / minimum）
- `--price-multiplier` 价格来源乘数，按分四舍五入（默认 1）
- `--stock` 上架库存（默认999，可显式设为0）
- `--limit` 最多处理 N 个 vc（0=不限）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--interval` 批次上架请求间隔秒
- `--cn-stock` 按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）
- `--json` 输出单个 JSON 对象

## ops publish

批量发布上架：输入 WB 商品码推送到店铺（支持指定前缀/完整VC/价格/尺寸/店铺）

实现入口 [run](../src/sellerops/features/publishing/bulk_publish.py)

- 必选其一 --shops / --all-shops
- `nms` WB 商品码列表（支持空格或逗号分隔，如 123456789 123456790）
- `--nm` WB 商品码（逗号分隔多个，兼容命令行参数传参）
- `--vc` 完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）
- `--prefix` 4位前缀码（如 ABCD 或 BCS-ABCD）
- `--sku` 商品价格表中的卖家 SKU，适用于本次输入商品
- `--random-prefix` 没有唯一登记前缀时，明确允许在预览时随机生成
- `--price` 指定售价（CNY），不填时使用本地快照价
- `--dims` 自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）
- `--length` 包装长 (cm)
- `--width` 包装宽 (cm)
- `--height` 包装高 (cm)
- `--weight` 包装毛重 (kg)
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--stock` 上架库存量（默认 999）
- `--cn` 商品中文名（辅助匹配价格与包装规格）
- `--file` 批量上架文件路径（支持 .txt 每行一个商品码、.json 商品列表或 .csv 表格）
- `--cn-stock` 按中文名指定库存（'中文名:库存,...'）
- `--interval` 批次/商品请求间隔秒（默认 1.0）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--json` 输出单个 JSON 对象

## ops create-cards

建卡发布上架：输入 WB 商品码推送到店铺（支持自定义完整VC/俄文详情与主图建卡）

实现入口 [run](../src/sellerops/features/publishing/card_publish.py)

- 必选其一 --shops / --all-shops
- `nms` WB 商品码列表（支持空格或逗号分隔，如 123456789 123456790）
- `--nm` WB 商品码（逗号分隔多个，兼容命令行参数传参）
- `--vc` 完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）
- `--prefix` 4位前缀码（如 ABCD 或 BCS-ABCD）
- `--sku` 商品价格表中的卖家 SKU，适用于本次输入商品
- `--random-prefix` 没有唯一登记前缀时，明确允许在预览时随机生成
- `--price` 指定售价（CNY），不填时使用本地快照价
- `--dims` 自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）
- `--length` 包装长 (cm)
- `--width` 包装宽 (cm)
- `--height` 包装高 (cm)
- `--weight` 包装毛重 (kg)
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--stock` 上架库存量（默认 999）
- `--cn` 商品中文名（辅助匹配价格与包装规格）
- `--file` 批量上架文件路径（支持 .txt 每行一个商品码、.json 商品列表或 .csv 表格）
- `--cn-stock` 按中文名指定库存（'中文名:库存,...'）
- `--interval` 批次/商品请求间隔秒（默认 1.0）
- `--apply` 兼容保留；确认预览生成的计划单后，用 apply --plan 提交
- `--json` 输出单个 JSON 对象

## ops publish-openapi

[备用] WB Open API 上架：建卡、有限等待标识、上传图片和莫斯科库存；支持续办

实现入口 [run](../src/sellerops/features/publishing/open_api_publish.py)

- `--source-shop` 自有源店 BCS 店铺 ID；不传则从公开 CDN 读取任意商品码
- `--nm` 源 WB 商品码，逗号分隔；自有源店模式使用该店商品码
- `--vc` 源店模式为完整 VC 筛选；公开商品模式指定目标完整 VC（支持 {nm} 模板）
- `--all` 源店全部非回收站商品；仅自有源店模式
- `--file` 公开商品 JSON 对象数组：nmId/vendorCode/price，可用 card 覆盖官方字段
- `--shops` 目标店铺 ID，逗号分隔；不可包含源店
- `--price` 新建全部规格的整数价格（目标店币种）；文件可逐项指定 price
- `--dims` 覆盖包装尺寸：长*宽*高/毛重，单位 cm/kg
- `--stock` 莫斯科仓各规格库存（默认 999，0 保留）
- `--limit` 最多选择 N 个源商品（0=不限）
- `--resume` 按 --shops/--vc 续办已有记录，不重新建卡；沿用原图片和库存计划
- `--wait-timeout` 等待完整商品/规格标识的总时限秒（默认1800，0=只查询一次，最多7200）
- `--wait-interval` 标识查询轮次间隔秒（默认15，范围1..60；仍遵守接口限流）
- `--apply` 兼容保留；确认上架或续办计划单后，用 apply --plan 提交
- `--json` 输出单个 JSON 对象

## ops normalize-vc

WB Open API：纠正历史 VC 前缀，保留原尾部，冲突时追加跨店一致序号

实现入口 [run](../src/sellerops/features/publishing/vc_normalization.py)

- 必选其一 --vc / --name / --name-contains / --all
- 必选其一 --shops / --all-shops
- `--vc` 完整旧 VC，逗号分隔
- `--name` 已确认的完整中文商品名
- `--name-contains` 中文商品名包含指定文字
- `--all` 全部非回收站商品；无明确归属的项记录阻止原因
- `--shops` 目标店铺 ID，多个用逗号分隔
- `--all-shops` 本动作的全部目标店铺
- `--apply` 兼容保留；确认改码计划单后，用 apply --plan 提交
- `--json` 输出单个 JSON 对象

## apply

按运营确认的固定计划单提交

实现入口 [run_apply](../src/sellerops/cli/ops.py)

- `--plan` 预览生成的计划单号
- `--json` 输出单个 JSON 对象

## login

安装专用浏览器或连接 BCS、马帮、WB

实现入口 [run](../src/sellerops/features/system/login.py)

- 最多选一项 --resume / --keep-open
- `platform` （setup / bcs / mabang / wb）
- `--resume` 仅检查专用浏览器中已保存的登录态
- `--keep-open` 等待网页登录成功后，在同一个窗口完成连接
- `--bind` 仅 WB，确认一组新店对应关系，可重复指定

## migrate-legacy

离线迁移旧商品资料、快照和提交记录

实现入口 [run](../src/sellerops/persistence/migrate_legacy.py)
