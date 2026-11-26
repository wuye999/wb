# 故障排查

先记录使用的解释器、命令、店铺范围、失败阶段和终端打印的结果文件路径。不要为了诊断刚提交的 WB/BCS 写入自动补跑同步或查询；先读取本次返回和本地日志。向维护者反馈时脱敏凭证与业务明细。

## 启动与路径

| 现象 | 处理 |
| --- | --- |
| 找不到 requests/openpyxl | 用运行脚本的同一解释器执行 `python -m pip install -e .`；确认没有把依赖装进另一个 Python |
| `No module named sellerops` | 在项目目录用 `python run.py --help`，或先 editable 安装；不要直接运行包内单个 py 文件 |
| 读取了其他数据目录 | 检查命令 --data-dir 和 SELLEROPS_DATA_DIR 环境变量，再确认 run.py 所在项目目录 |
| `unrecognized arguments` | 看对应 --help 和 CLI 参考；写命令不提供 --sync/--verify/--no-verify 参数 |
| 文件找不到 | 命令中的相对输入/输出文件路径通常相对当前工作目录；默认 data 根目录则由项目路径确定。这两者不同 |
| 参数解析退出码 2 | 检查必填项、选项拼写、互斥参数和取值。`product clean` 必须给 `--target`。`--data-dir` 可放在子命令前后 |

pip 下载超时时，继续使用运行项目的同一个 Python。可临时切换到阿里云 PyPI 镜像，或保留当前源并延长超时。任选一个命令即可。镜像说明见 [阿里云 PyPI 镜像](https://developer.aliyun.com/mirror/pypi)。

```powershell
python -m pip install -e . -i https://mirrors.aliyun.com/pypi/simple/
python -m pip install -e . --timeout 60
```

## 凭证与外部工具

| 现象 | 处理 |
| --- | --- |
| BCS 会话失效 | 检查本机 `bcs.login`，运行 `login bcs` 更新会话 |
| BCS 要求滑块、设备验证或网关校验 | 已配账号密码时先用 `login bcs` 直连。遇到网页验证时按 [平台连接配置](CONFIGURATION.md#通过-workbuddy-连接平台) 准备专用浏览器并在官网完成验证 |
| WB 后台 401/403 | 停止该店操作，另行运行 `login wb` |
| WB 直连登录或恢复较慢 | 页面连接最多等待 3 分钟，恢复店铺列表最多等待 4 分钟。页面接通后有 20 分钟完成验证和关窗。已经登录但店铺列表恢复超时，可用 `login wb --resume` 检查已保存的登录态 |
| WB 提示缺少店铺、绑定冲突或只读鉴权失败 | 原凭证保留。按报错核对当前 WB 账号、店铺访问权限和 BCS 对应关系 |
| 马帮缺少 www_cookie/shop_map | 按 CONFIGURATION 准备真实字段；shop_map 键须与马帮订单中的店铺文本一致 |
| 马帮要求腾讯验证码或短信验证 | 已配账号密码时先用 `login mabang` 直连。遇到网页验证时按 [平台连接配置](CONFIGURATION.md#通过-workbuddy-连接平台) 准备专用浏览器并在官网验证。认证失败前已有业务提交时先核对状态 |
| 专用浏览器缺少、版本不符或资料被占用 | 关闭占用资料的专用窗口，按 [平台连接配置](CONFIGURATION.md#通过-workbuddy-连接平台) 检查依赖并运行 `login setup` |
| `login setup` 下载失败或超时 | 核对终端所列来源和产物。Windows x64 可设置 `SELLEROPS_BROWSER_SOURCE=npmmirror` 或按需调高 `SELLEROPS_BROWSER_TIMEOUT_SECONDS`。遇到证书、重定向或 SHA-256 校验失败时，保留原安装并将该产物和错误交维护者核对 |
| 马帮 NO_SKU / 匹配失败 | 查看本次报表并核对中文归属。「发货方式」填写「马帮发货」「WB发货」或「不处理」，选「马帮发货」时补齐「马帮库存SKU」。更换失败单须处理后才能推进预报和交运 |
| 处理新增订单（`order process`）时跳过飞书登记 | 缺少飞书地址或 `lark-cli` 时会提示跳过，马帮步骤成功则返回 0。需要登记时补齐地址、CLI 和表权限，按需用 `order register` 补登 |
| `report ratings` 采集失败或只得到部分结果 | 核对 BCS 会话和对应店铺 `apiKey` 的评价读取权限及 `--wb-region`。查看 `data.failed_shops` 和本次导出。见 [查询报表与客服](USAGE.md#查询报表与客服) |
| `report products` 拒绝 `--url` / `--table`，或有效出单少于订单明细 | 移除旧参数。商品报告无需飞书或 `lark-cli`。近 7/14 天按 WB 下单日统计，口径 A 仅计在途和买断，缺 VC 的订单跳过候选匹配。退货统计按 `--order-days` 计算，采集失败时查看 `data.orders_complete` 和「数据覆盖说明」 |
| lark-cli 未找到 | 检查 `SELLEROPS_LARK_CLI` 是否指向存在的文件，或在未设置该变量时检查 PATH，确认工具支持 base 子命令和用户身份 |
| 飞书链接/表不存在、无权限 | 核对 base_url、--table、用户登录态和表权限；项目不会自动创建整份业务表 |
| 飞书分页报无 next_offset/没有前进 | 中止为避免漏读和重复登记；交维护者检查 CLI 输出/版本，不绕过去重改成只读第一页 |
| LLM 401/端点错误或生成失败 | 核对 key、model 和完整 chat/completions 请求地址。`support ai-test` 会使用本地样例调用 LLM |

## 数据与业务结果

| 现象 | 处理 |
| --- | --- |
| 商品价格表缺列 | 在 Sheet1 第一行补齐「产品中文名」「最低售价」表头，再重试预览 |
| `doctor` 提示价格表有填表异常 | 查看终端的类型、计数和示例 Excel 行号。JSON 的 `data.price_book` 提供同一组行号、计数和跳过的校验。公式无缓存时可用 Excel 重算并保存 |
| 公式无法解析/无缓存 | 用 Excel 等表格软件重算并保存，或明确填写有效售价；不把空价补零提交 |
| Excel PermissionError | 关闭占用目标表的 Excel/预览程序，保留备份，重试原本地操作；不用删除映射表解决 |
| `report vc-sales` 结果不完整或 Excel 中零单 | 该命令独立读取 WB 订单动态。重复游标、达到翻页上限仍有下一页或店铺读取失败时，核对 `data.failed_shops` 和日志，已有 Excel 属于部分结果。若完整采集但零单，仍会导出空汇总表，可核对店铺和北京时间下单日期。文件路径见 `data.artifacts.workbook` |
| JSON 损坏 | 从自己的备份恢复或交维护者定位；不要清空 state。跨进程锁文件长期存在是正常现象 |
| `sync` 或 `catalog fetch` 店铺失败、退出码非零 | 用 `--json` 回执查看 `data.errors`。成功店铺已更新，失败店铺保留原快照。`authentication` 表示 BCS 鉴权失败，`authentication_aborted` 表示本店受鉴权熔断影响，`batch_timeout` 表示远程阶段已达时间预算 |
| 快照回执提示 BCS 缓存，或体检提示 `cache_unconfirmed` / `stale` | 对照 `data.fetched[].snapshot_status` 区分同步未确认和显式跳过。未确认的提交、轮询或未启动原因见 `data.sync_warnings`。即使刚保存缓存，店铺的新鲜度仍取决于 `last_confirmed_sync_at` |
| BCS 商品重复、快照或单店表行数偏大 | 新读取会对同店相同 nmId/完整 VC 的兼容记录去重，保留完整原行；旧文件不会因升级自动改写。非空业务字段冲突、标识歧义或没有完整原行时会报错，不随意保留第一/最后一行。规则见 [数据契约](DATA.md#映射快照与标识)；整理旧文件前先备份 |
| 更新后单店表变空 | 有效空快照会清除该店旧商品明细和总表中的该店覆盖，历史归属仍保留。核对本次 `sync` 结果和 `report mapping` 导出 |
| `doctor` 的仓库更新检查不可用 | 平台连接状态与该检查独立。本机 `main` 分支可在网络恢复后按需用 `doctor --check-updates` 跳过缓存重查。更新提示由运营决定是否手动拉取 |
| `report mapping` 提示缺少快照 | 用 `shop list` 核对该店的快照状态，经运营要求再单独运行 `sync`。审核结果用 `catalog merge` 或 `map batch-set --file` 导入 |
| 筛选结果为空/全部跳过 | 核对 `--shops`、VC 或中文名、商品归属和已有快照，按原范围排查 |
| 默认业务仓未配置、匹配不到或匹配多个 / 计划出现其他仓库 ID | 中止该店操作。按 `data/credentials.json` 中 `wb.warehouse_keyword` 核对本店仓库列表，只使用唯一匹配并核实的本店默认业务仓（如莫斯科仓或运营配置的其他业务仓）的 `id`。不猜测、不跨店复用 ID、不回退未配置的仓库，也不使用 office 字段 `warehouseId` 代替 |
| `ops stock` 或 `ops trash` 提示计划不完整、预览非零退出 | 查看被阻止的店铺和完整 VC，核对本店默认业务仓、商品和规格 ID。需要更新快照时按用户要求单独运行 `sync` |
| `apply --plan` 提交下架时清库存失败 | 任何规格失败或回执不完整都会阻止整件商品移入回收站，其他正常商品可继续。按本次日志处理具体失败项，保留已受理回执 |
| 价格审核显示空清单或读取失败 | 成功回执返回 `quarantineGoods: null` 表示暂无待审核商品。如果出现店铺异常或非零退出，查看 `ops review-price --json` 的 `data.result.failed_shops` 并核对会话或分页报错 |
| 提交后本地显示旧价格/库存 | 本地快照可能尚未更新。依据本次回执和日志结束。`catalog fetch --no-sync` 只读 BCS 已有缓存，无法核对 WB 最新值 |
| 超时但可能已被接受 | 保留任务号和本次结果，汇报未知/部分完成；不立即重放整批，不自动换通道再提交 |
| 订单统计日期与本机不一致 | 检查 `report orders` 默认的 America/Los_Angeles 时区、日期窗口和创建或更新时间口径 |
| 飞书登记出现重复 | `order register` 只追加去重后的新增记录。存量清理用 `order deduplicate` 预览，确认后加 `--apply` |
| 库存表历史列不见了 | 检查是否执行过全量库存重建或显式日期区间模式，先用自己的表备份恢复，不再次重建覆盖 |
| WB 发货群缺 PDF、部分加入失败或结果不明 | 仅在配送已受理、只缺 PDF 时用 `order group-ship --resume --apply` 补导出。部分加入失败或结果不明时按原批次回执人工处理 |

## 每日任务与退出码

`task run` 和 `task schedule` 默认预览。`task run morning --apply` 提交促销报名并生成待确认折扣计划，`task run adjust-discounts --apply` 只生成待确认折扣计划。用 `task schedule --apply` 注册的 Windows 任务沿用相同行为，按电脑本地时间和注册时记录的解释器、路径及店铺范围运行。移动项目后需重新注册。

`task run` 某一步非零退出会停止后续步骤，先查看 `data/logs/YYYY-MM-DD/daily_YYYYMMDD.log`。普通命令遇到失败也可能已有部分成功提交，非零退出不能作为整批重试的依据。

监听命令使用 --once 时，本轮读取、生成或发送失败会非零退出；持续监听则记录轮次异常后继续下一轮。查看本次日志和 CSV 区分已提交、失败、跳过及人工待处理项。退出码 0 不代表快照是最新值或平台已最终生效。各功能所需依赖见 [配置](CONFIGURATION.md)。

CLI 参数错误通常为 2，未捕获业务异常由主入口转为 1，中断为 130；具体处理器可返回自己的状态。不要因中断就断言远端请求未发生，按最后已提交步骤汇报。

## 给维护者的最小问题描述

提供：项目版本或提交号、Python 版本、脱敏命令及范围、出现问题的阶段、返回码、成功/失败/跳过计数、脱敏错误及相关日志片段。明确是否已经提交过写请求。密码只报「已填/未填」。不要附带原始 credentials.json、Cookie、抓包、浏览器资料或完整商业表格；如需样例，在 _scratch 准备脱敏的最小输入。
