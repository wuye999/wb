# 日常操作流程

命令均在项目根目录执行，店铺 ID `1001`、商品码 `123456789` 和商品名仅为示例。优先限定店铺和商品；不加 `--shops` 的命令通常会覆盖其全部可用店铺。完整参数见 [CLI](CLI.md)，配置见 [CONFIGURATION](CONFIGURATION.md)。

## 看懂读写边界

| 操作类型 | 命令 | 执行行为 |
| --- | --- | --- |
| 不联网的帮助/任务预览 | 任意 `--help`、daily/schedule 不加 --apply | 显示定义，不执行平台业务任务 |
| 平台查询与报表 | shops、orders、dims-check、promo-goods、promo-gap、feishu-vc-stats、fbs-delivery、order-stats、chats、appeals、ratings | 读取对应平台，通常生成本地输出；orders 默认先同步订单 |
| 本地工作台与映射 | mapping、review、mapping-check、mismatch-check、merge、mapping-import、mapping-rename、shops-mapping | 写本地 HTML、表格或状态，没有通用 --apply 保护；部分主店模式可能枚举 BCS 店铺 |
| 数据更新 | fetch | 默认触发 WB→BCS 同步，再写本地快照；不是每次运行前的必选步骤 |
| 业务写入 | 改价、库存、上架、清理、折扣、促销、订单处理、飞书写入、客服发送 | 默认预览，--apply 提交；范围和确认机制逐命令核对 |
| 会话/AI | cookies-update、cookie-refresh-wb、ai-test、客服监听 | 可能回写凭证/状态或调用 LLM；细节见下文 |

所有 WB/BCS 写入提交并记录结果后结束，不做写后查询，不追加同步或映射表合并。终端中的“提交成功”不是即时最终状态证明。本地日志、接口错误和任务号是本次汇报依据。

## 当前运行约定

涉及仓库的读取与修改，**默认且仅允许各店自己的莫斯科仓库 ID**。这包括库存、下架前清库存、回收站归零和上架；“全店”“全部商品”也只在莫斯科仓范围内操作。先核对仓库名称和 `id`，不跨店复用 ID，不把 office 字段 `warehouseId` 当成库存仓库；没有可确认的莫斯科仓时中止，不换成都或其他俄罗斯地区仓。详见 [仓库范围](CONFIGURATION.md#仓库范围仅允许莫斯科)。

WB 凭证目前通过人工登录、手动抓包并写入维护；Playwright 尚未启动、浏览器自动续期未启用。鉴权失败时按 [手动抓包说明](CONFIGURATION.md#手动抓包更新凭证当前方式) 处理，包括受限浏览器打开 Network 的方法，不把启动自动浏览器作为默认排错步骤。

## 商品映射与人工审核

已有原 data 时，按需要生成工作台：

```powershell
python run.py mapping
python run.py review
python run.py mapping-check --tol 5
python run.py mismatch-check --cn "示例商品" --days 7
```

按输出路径打开 `data/workbench/` 中的 HTML，核对候选归属、图片、中文名或商品码。默认 mapping 和 review 的 VC 中心审核导出交给 merge；不同工作台的结果协议不能混用：

```powershell
python run.py merge "_scratch/review.json"
python run.py shops-mapping --shop-id 1001
python run.py mapping-rename --vc "BCS-TEST-123456789" --cn "修正后的商品名"
```

这些命令直接更新本地映射或状态。旧单店格式核对结果使用 `mapping-import 文件.json`，不要用它替代多店 review→merge 流程。`merge` 不从平台重新拉取商品，也不表示远端状态已经更新。

只有用户明确要求获取最新商品数据时，独立运行：

```powershell
python run.py fetch --shop-id 1001
python run.py merge
```

`fetch --no-sync` 只读取 BCS 现有缓存，不保证是最新数据；不要把它当成即时写后验证。

如果已在 BCS 后台完成同步，直接运行以下两条命令，无需临时脚本或额外先运行 shops-mapping：

```powershell
python run.py fetch --no-sync
python run.py merge
```

先确认 fetch 成功退出，再运行 merge。任一店铺拉取失败时，成功店铺已更新，失败店铺保留原快照；失败名单写入 `data/state/多店铺同步状态.json`，命令以非零状态退出。处理错误并重新成功拉取前，不继续合并，以免混用新旧快照。

`merge` 会依据有效快照刷新各店本地映射，再合并总表。有效的空快照也会将该店单店表重建为空，移除旧商品覆盖；历史归属池不因此删除。缺失或损坏的快照不当作空店铺处理，会报错中止。这里的单店表刷新仅修改本地文件，不触发 BCS 同步。

## 改价与库存

先看预览，确认后才执行相同范围的写入：

```powershell
python run.py price --shops 1001 --name "示例商品"
python run.py price --shops 1001 --name "示例商品" --apply --yes
python run.py stock --shops 1001 --vc "BCS-TEST-123456789" --amount 20
python run.py stock --shops 1001 --vc "BCS-TEST-123456789" --amount 20 --apply
```

改价默认使用价格表「实际售价」向下取整，`--price` 可指定整数目标价；只改折扣可以使用 `--keep-price --discount`。库存默认 amount=0，执行归零需 `--yes`，不要把没有 --amount 的命令当作补库存。

`stock` 默认使用本地映射筛选供应商代码，并从本地商品快照解析规格 ID（`--resolve snapshot`）。库存归零直接将目标规格设为 0，不需要先联网读取当前库存，即使本地库存已为 0 也照常提交。缺少仓库确认或任一规格 ID 时阻止该商品，预览和执行均以非零状态报告；不通过自动全量联网查询补齐。仓库 ID 仍需逐店确认，更新商品快照使用独立的 fetch/merge 流程。

`price/stock` 默认走 WB；`price-bcs/stock-bcs` 是另选的 BCS 通道。WB 凭证失效时先手动抓包更新，不要让 AI 自动换通道提交同一批可能已被接受的操作。实时库存取数也须限定莫斯科：`stock --resolve live` 的底层规格读取可返回账号级多仓数据，不能把这些结果直接拼成全仓任务；无法保证读取和计划范围仅为莫斯科时，不使用该路径执行。

部分大幅降价会被 WB 的隔离区规则限制；查看本次跳过和错误原因，不通过反复重跑绕过。明确需要审核新价格时，可单独预览/执行 `price-review`；用户明确要求整合审核时才使用支持它的 `--auto-review`。

## 发布商品与跨店补齐

```powershell
python run.py publish 123456789 --shops 1001 --price 59 --dims "10*20*30/0.5" --stock 20
python run.py create-cards 123456789 --shops 1001 --vc "BCS-TEST-123456789" --price 59 --dims "10*20*30/0.5" --stock 20
python run.py publish --file "examples/publish.example.json" --shops 1001
python run.py replicate --shops 1001 --name "示例商品" --limit 1
python run.py import-publish "_scratch/foreign.xlsx" --shops 1001 --limit 1
```

这些示例均为预览，需要真实提交时另外加 `--apply`。发布默认库存是 999；需要其他数量时显式设置 `--stock`，跨店/导入可使用 `--cn-stock`。

`publish` 使用批量发布协议，平台按前缀生成 VC，不保证透传任意完整自定义 VC；需要完整 VC 建卡语义时使用 `create-cards`。`replicate` 使用建卡协议保留原始完整 VC，同一 VC 在不同源店只选一次；WB 商品码读取映射表，不从 VC 尾段猜测。默认使用本地快照和提交记录计算缺失目标，不逐商品联网扫描列表。目标本地全状态快照中已有的卡（包括回收站）会跳过；此命令不自动恢复回收站卡。快照可能滞后，执行前需结合已知操作回执核对范围，不能把刚下架的旧快照行再次当作在架源商品。

复制上架可分别限定源店与目标店，并按商品价格表的「最低售价」定价：

```powershell
python run.py replicate --source-shops 1001,1002 --shops 1003 --price-source minimum --price-multiplier 10 --stock 999
```

示例默认预览，加 `--apply` 提交。`--price-source` 默认 `snapshot`（源店价格），倍数默认1；`minimum` 必须匹配唯一的同中文名最低售价，不能回退到「实际售价」或整数价。金额按分四舍五入；库存默认999，显式0保留为0。包装尺寸与毛重优先映射表，其次价格表，再次公开商品详情。上架前准备整批详情与载荷，仅解析目标店莫斯科仓；任一目标仓缺失时阻止该商品组，提交后记录受理响应并结束。

## 尺寸、下架和清理

```powershell
python run.py dimension --shops 1001 --name "示例商品"
python run.py dimension --shops 1001 --vc "BCS-TEST-123456789" --dims "10*20*30/0.5"
python run.py dims-check --shops 1001 --type all
python run.py trash --shops 1001 --vc "BCS-TEST-123456789"
python run.py banned --shops 1001 --limit 10
python run.py clean --target draft --shops 1001 --limit 10
```

- 自定义尺寸必须同时用 VC、中文名或前缀限定；不指定 --dims 时依据价格表包装列。
- `trash --apply --yes` 从本地快照读取商品和全部规格 ID，先清莫斯科仓库存，再移回收站；无需先单独运行 stock，也不全量查询远端商品或库存。无法确认唯一可用的莫斯科仓库时阻止该店；商品 ID 或任一规格 ID 缺失时阻止该商品，不只清部分规格后下架。
- 清库存失败或受理回执不完整时，受影响商品不会移回收站，其他正常商品继续；命令返回非零状态，日志记录被阻止原因。预览发现不完整计划也返回非零状态。收到清零受理回执后直接进入回收站提交，不进行写后查询验证。`trash-bcs` 同样遵守清库存前提，是独立 BCS 通道，不作为 WB 失败后的自动兜底。
- `banned --apply` 会先要求输入确认，再提交被阻止商品到回收站；已确认授权范围的自动化可传 --yes。拒绝确认时不创建平台会话，提交后不重查列表。
- `clean --target draft/basket/all --apply` 会实际清理，命令没有 --yes 参数。all 包含草稿和回收站，使用前确认两类数据都在用户授权范围。
- `warehouse-clear` 针对成都仓库永久删除，不符合当前仅操作莫斯科仓的约定，当前不使用该命令；代码中存在该功能不表示获得操作授权。

这些操作不能仅凭“有 dry-run”就扩大范围或自动确认。

## 折扣、促销和推广审计

```powershell
python run.py discount --shops 1001 --name "示例商品" --target 50
python run.py discount --shops 1001 --threshold 55 --below 40 --target 50
python run.py discount-bcs --shops 1001 --threshold 50 --target 50
python run.py promo-apply --shops 1001
python run.py price-review --shops 1001
python run.py promo-goods --shops 1001
python run.py promo-gap --shops 1001 --mode both --days 7
```

折扣、促销报名和价格审核需要 `--apply` 才提交；后三类查询/审计的结果位置以终端打印为准。折扣 threshold/below 是并集：`>55` 或 `<40`；--vc、--all、显式阈值的优先关系见帮助。

`promo-gap` 依赖飞书订单登记、目标店快照和推广信息，按目标店合计销量与各店推广状态计算缺口/浪费。它只输出建议，不自动创建或关闭广告，不应把其他店的商品码直接用于本店写入。

## 订单履约与飞书

```powershell
python run.py orders --shops 1001 --days 1 --no-sync
python run.py mabang-orders
python run.py mabang-forecast --check
python run.py mabang-process
python run.py feishu-register
python run.py feishu-register-dedup --limit 20
python run.py mabang-stock-register
python run.py mabang-stock-daily
```

`orders` 不加 --no-sync 时会先同步订单。其 days 和其他命令的 days 不一定是同一日期口径。

`mabang-process --apply` 默认处理 `mabang.shop_map` 配置店铺的全部新增待处理订单，不限日期。顺序为库存 SKU 匹配、预报上传、成功状态轮询、交运；配置飞书地址时再登记飞书。`mabang-orders`、`mabang-forecast` 及飞书登记合并的待处理单也使用相同日期语义：`--days 0`（默认）不发送起止日期限制；明确要处理最近 N 天时传正数，例如 `--days 7`。仍只处理配置店铺与新增待处理状态，不包含其他店铺或历史已完成订单。列表完整翻页，达到安全上限或重复分页时报错，不用部分结果继续执行。更换失败、缺 SKU、预报失败或未完成的订单会按流程阻断后续履约，依据本次日志处理，不删除状态后盲目重跑。

预报状态等待是交运前必要步骤，不能当成写后验证而跳过。`--check` 只是查看预报批次，`mabang-forecast --apply` 才执行整条预报/交运链。

`feishu-register --apply` 按订单编号去重增量登记，latest 模式合并最近订单和待处理订单。历史补录可明确指定：

```powershell
python run.py feishu-register --scope all --date 2026-10-01
```

用户确认后再加 --apply。去重命令只清已有重复记录，库存全量登记则会清空并重建库存表；都不能当作每天的无害初始化。`mabang-stock-daily` 默认补今天和更新现有日期列，显式 --begin/--date 会进入可删除旧日期列的区间模式，详见 [数据契约](DATA.md)。

## 查询报表与客服

```powershell
python run.py feishu-vc-stats --days 7
python run.py fbs-delivery --shops 1001 --days 7
python run.py order-stats --shops 1001 --days 30
python run.py appeals --shops 1001 --days-max 5
python run.py ratings --shops 1001 --one-star
python run.py questions --shops 1001 --no-detail
python run.py chats --shops 1001 --limit 10 --no-detail
```

报告一般写入 data/logs，部分命令支持 --out。`order-stats` 默认时区为 America/Los_Angeles，统计窗口按该时区解释，不要直接当成本机当天；导出还区分订单创建时间/更新时间的口径。

提问回复先确定店铺和提问 ID：

```powershell
python run.py questions --shops 1001 --question-id "QUESTION_ID" --reply "回复内容"
python run.py questions --shops 1001 --question-id "QUESTION_ID" --reply "回复内容" --apply
```

第一条预览，第二条发送。批量回复还需 --reply-all 与 --yes；不能因一条提问授权而回复所有店铺全部提问。

```powershell
python run.py questions-watch --shops 1001 --once
python run.py questions-watch --shops 1001 --mode back --once
python run.py chat-watch --shops 1001 --mode back --once
python run.py ai-test --qa "examples/ai_test_qa.example.json"
```

监听默认 front，只展示并记录已展示状态；back 会调用 LLM 生成草稿，仍需 --apply 才自动提交/发送。--apply 也会切换到后台模式。不加 --once 会持续轮询；停止用 Ctrl+C。`chat-watch` 默认只处理最近 72 小时，--since-hours 0 会扩大到历史消息，不作为普通补跑参数。

`ai-test` 会联网调用 LLM 并写对照报告，不向买家发送；不要与 `scripts/check.py` 的离线测试混淆。

## 每日任务与 Windows 计划任务

```powershell
python run.py daily morning
python run.py daily check
python run.py schedule
```

morning = 促销报名 + 改折扣，check = 只改折扣。这里的“check”是历史任务名称，并非只读检查；`daily check --apply` 会写平台。daily 预览只打印步骤，用户授权后可以使用：

```powershell
python run.py daily morning --shops 1001 --apply
python run.py schedule --apply
python run.py schedule --remove --apply --yes
```

schedule 注册 SellerOps_Morning（09:00）及 SellerOps_Check11/15/19（11:00、15:00、19:00），按运行电脑本地时间执行；自动任务自身包含 --apply，默认覆盖相应步骤的全部可用店铺。当前 schedule 不提供 --shops，所以只运行指定店铺时，应使用显式 scoped 的 daily 命令或由维护者设计任务，不能假设预览里的范围会自动继承。

注册时固定当前解释器、代码路径和数据路径；移动目录或换解释器后需重新注册，并清理不再使用的任务以免重复执行。daily 每步最多等待 1 小时，某步非零退出会停止后续步骤；日志在 data/logs/daily_日期.log。
