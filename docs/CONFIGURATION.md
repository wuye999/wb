# 环境与凭证配置

配置由 [路径模块](../src/sellerops/settings/paths.py)、[凭证访问器](../src/sellerops/settings/credentials.py) 和各平台集成读取。常驻凭证只放自己的 `data/credentials.json`，不要贴入源码、文档或公开提交。商品报告的临时BCS账号按下文通过标准输入传入，仅留在本次进程内存。可参考 [空值配置样例](../examples/credentials.example.json)，其中示例店铺和空字段不能直接用于生产。

## 功能需要哪些配置

| 功能 | 主要前置条件 |
| --- | --- |
| 帮助、daily/schedule 默认预览 | Python 与基础依赖，不读取平台凭证 |
| shops、orders、BCS 来源 fetch、BCS 通道写入 | BCS 凭证；本地表格/快照由具体用例决定 |
| fetch 默认 Open API 来源 | BCS 凭证和 BCS 各店保存的官方 apiKey，具备 Content、Prices and Discounts、Marketplace 权限；不要求后台 Cookie |
| publish-openapi、sync-cards-openapi | BCS 保存的各店官方 apiKey；Content 读写权限，上架另需 Marketplace 仓库读取/库存写入权限；固定中国区，不要求后台 Cookie，见 [专门说明](OPEN_API_LISTINGS.md) |
| price、stock、trash、折扣、促销、客服、WB 报表 | 对应 WB 店铺完整 Cookie 三件套；涉及商品匹配时还需本地数据 |
| 上架、跨店复制、BCS 库存/下架中的仓库解析 | BCS 与对应店铺 WB 凭证，以及本地价格/映射/快照；默认仓库解析走 WB |
| mabang-orders、mabang-forecast、mabang-process | 马帮凭证与店铺映射、本地商品/库存 SKU 映射；取消单过滤还可能读取 WB |
| 飞书登记、库存表、飞书统计、promo-gap | 可用的 lark-cli 用户身份、表格权限和目标表；其中登记/库存还需马帮相关配置 |
| AI 草稿与 ai-test | LLM key、完整请求端点和模型；自动发送还需 WB 凭证及显式 --apply |
| WB 凭证维护 | 当前由用户手动登录、抓包并写入；Playwright 浏览器刷新能力保留，但尚未启动或启用 |

表中是主要依赖。dry-run 也可能调用这些平台的读取接口，不能等同离线检查。

`publish-openapi --apply` 在建卡受理后允许按待办完整 VC 有限查询本店 nmID/chrtID，核对生成条码后上传图片和莫斯科库存。默认等待上限1800秒、轮询间隔15秒；超时保留记录，用 `publish-openapi --resume` 续办。此轮询不写本地商品快照，不触发 BCS 同步，也不在图片/库存提交后验证结果。其余写入的提交后结束约定保持不变。

## 仓库范围：仅允许莫斯科

当前所有涉及仓库的读取和修改，**默认且仅允许使用目标店铺的莫斯科仓库 ID**。这适用于库存查询/修改/清零、下架前清库存、回收站归零、发布上架、跨店复制，以及 AI 临时脚本。「全部商品」「全店」只扩大商品范围，不扩大到全仓；成都仓库及其他俄语地区仓当前都不操作。

- 通过 WB 仓库列表确认该店的「莫斯科」仓库，业务请求使用响应行的 `id`。同一行的 `warehouseId` 是另一套 office 标识，不能互换。
- 每家店单独确认 ID；不能把某家店的莫斯科 ID 复制给其他店，也不能取列表第一条代替名称核对。仓库枚举只用于识别目标，不代表其他仓库获得操作授权。
- 找不到莫斯科仓、名称与 ID 不匹配或无法核实时，中止该店操作，不回退到成都或其他俄罗斯地区仓，不临时更改默认仓库配置。
- 临时脚本和底层调用也要先校验 ID，库存读取传入莫斯科 `store_id` / `warehouse_id`，写入前再次核对计划中的仓库 ID。马帮的 `mabang.warehouse_id` 属于马帮自己的标识体系，当前也须核实为莫斯科业务仓，不能套用 WB ID。
- `warehouse-clear` 专门针对成都仓库，虽然代码和 CLI 中保留该命令，**当前操作流程不使用它**。

这是现行操作约束，并非所有底层 API 都已经自动强制校验。当前业务仓库解析器只接受唯一可用的莫斯科仓，不回退其他地区；某些底层库存读取未传 store_id 时仍可返回多仓，调用方必须核对并限定莫斯科。开发要求和私有协议资料入口见 [开发指南](DEVELOPMENT.md#当前必须保留的运行约定)。

## BCS：bcs

| 字段 | 说明 |
| --- | --- |
| `base_url` | API 根地址；缺省为代码中的 BCS 地址，按自己账户的服务地址填写 |
| `token` | 登录 Token，填写值本身；代码会拼接 Bearer 和 Cookie |
| `limit_key` | 网关 X-Limit-Key |
| `cookie_extra` | 其他需要附带的 Cookie 内容，保留原格式 |

401 通常表示凭证失效；网关错误还可能与 limit_key 有关。店铺枚举来自 BCS 的 `shops` 命令，与 WB Cookie 是否完整是两项检查。

店铺原始响应的 `apiKey` 是该店 **WB Open API** 令牌，不能填到 `bcs.token` 或 `wb.authorizev3`。现有 `fetch_shop_list()` 只返回 id/name；默认商品快照、只读报告和显式 Open API 商品写入通过内部 `fetch_open_api_shops()` 分页读取对应店铺的官方令牌，不输出或保存密钥副本，也无需新增凭证段。

库存、改价、尺寸、下架及定向折扣操作在本地必要字段不足时，也可在提交前用此令牌按本店完整VC/商品ID只读补齐；完整数据不额外查询商品，补齐不更新快照或映射，不改变这些命令原有的WB网页/BCS写入通道。官方令牌失效在BCS更新，网页写入的Cookie失效仍按下文人工抓包更新，不能互相替代或自动换通道。

`fetch` 默认使用中国区 `.cn` 官方主机。个人令牌可用于自有程序及本地软件，快照只需要 Content、Prices and Discounts、Marketplace 的读取权限；基础令牌的类别可用性与限额不同，不能只凭令牌字符串判断。401/403 时检查 BCS 对应店铺 apiKey 是否有效及权限是否齐全，更新后重新执行独立 fetch；不改用 Cookie、浏览器或旧缓存。429 会结束该店采集并保留旧快照，稍后再按原范围重试。原 BCS 缓存拉取须显式 `--source bcs`。

`product-report` 要求 BCS 已保存各店有效的官方令牌，并具有 Analytics、Feedbacks and Questions、Marketplace 类别权限。默认使用 `.ru` 官方主机，需使用中国区主机时显式传 `--wb-region cn`；不自动切换主机或改走后台 Cookie。常规限额以个人、服务或带密钥基础令牌为前提，低限额基础令牌可能触发429；限流或401/403时该阶段停止并记录未知，不启动浏览器。飞书仍使用已登录的 lark-cli。[报告说明](PRODUCT_REPORT.md)

`product-report --extra-bcs-stdin` 可临时引入其他BCS账号：由调用方或AI通过标准输入管道传入一个或多个完整JSON形式fetch抓包（UTF-8，传完关闭输入流），包含该账号匹配的Authorization、X-Limit-Key和Cookie。仅接受上述BCS固定域名，不执行抓包请求、不写入本机配置或文件。临时店铺只补充统计，`--shops` 只筛本机目标；下次运行需重新提供。具体输入、去重及报告范围见 [临时账号说明](PRODUCT_REPORT.md#临时引入其他bcs账号)。

## Wildberries：wb

`wb.shops` 是店铺对象数组，代码实际读取以下驼峰字段：

| 字段 | 说明 |
| --- | --- |
| `shopId` | 整数店铺 ID，与相应业务中的店铺一致 |
| `shopName` | 显示名称 |
| `authorizev3` | 对应店铺的授权值 |
| `wb_seller_lk` | 卖家会话令牌 |
| `cookie` | 同一店铺的完整 Cookie 字符串 |

后三项缺少任何一项，店铺就不会进入可用 WB 店铺列表。已有配置中的其他键也应保留；不要把不同店铺的三件套拼在一起。可选 `wb.root_version` 用于请求版本，未填时采用代码缺省值，不要把文档示例版本当成必须替换的最新值。

### 手动抓包更新凭证：当前方式

当前由用户在已登录的浏览器里手动抓包，再手工填写 `data/credentials.json`，或用 `cookies-update` 导入人工保存的 fetch 文本。**Playwright 尚未启动，浏览器自动登录/自动续期流程未启用**。源码里的浏览器模块、401 刷新钩子和 `cookie-refresh-wb` 命令属于保留能力，不能当作当前已部署方案；遇到鉴权失败仍走以下人工流程，不自行安装或启动 Playwright。

1. 人工登录对应店铺的 `seller.wildberries.ru`，确认当前店铺。按 F12 打开开发者工具。
2. 正常浏览器直接打开 **Network（网络）** 面板，再刷新页面或触发一次普通页面读取。
3. 某些受限浏览器只显示 **Console（控制台）** 和 **Elements（检查元素）**，看不到 Network。可在 Console 运行下面的定位代码，再点击产生的红色失败请求进入 Network：

```javascript
fetch('https://seller.wildberries.ru/api/__devtools_open__' + Date.now() + Math.random().toString(36).slice(2))
  .catch(() => console.log('已触发失败请求：请点击该红色请求进入 Network 面板'));
```

这里访问的是故意构造的不存在的地址，失败是预期结果；这条请求只用于打开面板，**不能作为凭证来源**。

4. 进入 Network 后刷新页面，选择一个真实的后台 API 请求，例如促销列表读取，查看 **Request Headers**。
5. 从同一店铺的同一会话提取 `authorizev3`、`seller-lk`（也兼容 `wb-seller-lk`）和完整 `Cookie` 请求头。Cookie 要从 Network 的请求头复制；`document.cookie` 取不到 HttpOnly 项，不能代替完整值。
6. 手工填写时，对应到 `wb.shops` 中正确的 `shopId` / `shopName`：`authorizev3` → `authorizev3`，`seller-lk` / `wb-seller-lk` → `wb_seller_lk`，`Cookie` → `cookie`。备份原文件，保留 JSON 格式和其他字段，不把不同店铺的三件套混用。

如需导入文本，在 Network 对真实请求选择 **Copy as fetch**，把代码作为文本保存到 `data/har/session.md`。检查 fetch 的 `headers` 中三个值齐全，使用双引号、小写键名 `authorizev3`、`seller-lk`（或 `wb-seller-lk`）、`cookie`；复制结果若未包含 Cookie，需把 Request Headers 中的完整值补到文本里。无需再次执行这段带会话的 fetch。

```powershell
python run.py cookies-update "session.md"
```

`cookies-update` 的相对输入路径以当前数据目录的 `har/` 为基准，因此上述命令读取 `data/har/session.md`；不要再把 `data/har/` 前缀重复传入。其他位置可传绝对路径。它直接修改凭证，没有 `--apply` 预览机制。

导入按现有 `wb_seller_lk` 的 `Z-Sid` 前 8 位匹配店铺。首次配置、现有令牌损坏或出现“未识别 sid”时，先人工核实店铺并填写对应项，不能按抓包顺序猜测归属；导入后查看更新的店铺名单。抓包原文、Cookie、Token 与备份只保存在自己的数据区，不放入源码、公开文档或 Git。

更新凭证不等于商品写后验证，不因此自动执行同步、拉取或合并。以后若另行决定启用 Playwright，应由维护者先明确部署与操作方案，再同步修改这里的运行约定；不能只因代码里存在相应功能就改用它。

## 马帮：mabang

已有配置时整体保留。新账户至少核实：

| 字段 | 说明 |
| --- | --- |
| `www_cookie` | www 域登录 Cookie，读取订单和鉴权使用 |
| `shop_map` | JSON 对象：键是马帮订单中的店铺标识文本，值是本地店铺标签，例如 `示例店铺(1001)`；不能假设键总是 WB ID |
| `warehouse_id` | 马帮库存匹配使用的真实仓库 ID；新账户必须明确填写，不依赖代码中的历史缺省值 |
| `aamz_cookie` | aamz 域会话；未填时代码回退 www_cookie，实际是否可用取决于会话 |
| `api_bearer` | api 域 Bearer 值，不加 Bearer 前缀；可由程序续期 |
| `api_key` | api 域 key；也可从 www_cookie 的指定登录 Cookie 中提取 |
| `handover_keyword` | 用于匹配交运物流渠道的关键词；核对自己账户的渠道 |
| `handover_channel_id` / `handover_channel_value` | 渠道识别及配置回退值，按自己账户保留/设置 |
| `upload_timeout_seconds` | 预报等待超时，缺省 900 秒；CLI 可覆盖 |
| `upload_poll_interval` | 预报状态轮询间隔，缺省 20 秒；CLI 可覆盖 |

马帮读取方法要求非空 www_cookie 和 shop_map。部分接口会自动续期 api token，并回写自己的凭证文件。预报物流还使用集成模块中的协议默认值；换马帮账户或物流方案时，要让维护者核对这些默认值与渠道，不能只填示例 JSON 就假定可发货。

订单处理默认只覆盖 `shop_map` 配置店铺的全部新增待处理订单。`--days 0`（默认）不发送日期限制，显式正数才限制最近 N 天；不将其他店铺或历史已完成订单纳入履约。

## 飞书：feishu 与本机 lark-cli

- `feishu.base_url` 填有权限的多维表格链接；命令的 `--url` 可覆盖。
- 本机需安装并登录支持当前 `base +...` 子命令的 lark-cli，程序以 `--as user` 调用。登录/安装方法使用所安装 CLI 自带帮助，不把不同版本的身份配置混用。
- 在 PATH 中可找到 `lark-cli` / `lark-cli.cmd`，或用 `SELLEROPS_LARK_CLI` 环境变量指定可执行文件的完整路径。
- 先准备现有目标表和列，程序不会替你新建整份业务表格。默认表名为「订单登记」「马帮库存登记表」，可由各命令的 `--table` 覆盖。

表字段见 [数据契约](DATA.md#飞书业务表)。用户身份需具备目标操作的权限：统计只需读取，登记、清理和库存重建还需相应写入权限。

项目不内置这份外部 CLI，也不能把任意同名工具视为兼容。新电脑应使用维护者确认的配套版本及其安装说明；确认支持 `base +url-resolve`、`+table-list`、`+field-list`、`+record-list` 以及所需的记录/字段写入、附件子命令。不要凭未经核实的 npm 包名安装另一个 CLI 后直接执行库存重建。

在终端用 `where.exe lark-cli`、`lark-cli --help` 检查本地入口和帮助；检查结果不等于表格授权已有效。路径不在 PATH 时可配置：

```powershell
$env:SELLEROPS_LARK_CLI = "D:\Tools\lark-cli.cmd"
```

这里的路径是占位示例，必须换成实际配套工具。飞书功能以本机 CLI、用户登录与表格权限都可用为前提；其他不依赖飞书的功能可独立配置使用。

## AI：ai

| 字段 | 说明 |
| --- | --- |
| `api_key` | LLM 服务密钥；代码也读取历史 `deepseek_key`，新配置使用 api_key |
| `base_url` | **完整的聊天补全请求地址**，例如包含 `/chat/completions`；客户端直接 POST 到这里，不自动拼接路径 |
| `model` | 服务支持的模型标识 |
| `max_tokens` | 输出上限；不同调用场景可能使用不同的缺省值，建议显式填写 |
| `watch_interval` | 客服轮询间隔秒数，缺省 90；可由命令 `--interval` 覆盖 |

不填端点或模型时代码使用内置缺省值；这不意味着已有 key 可跨服务使用。先用自己的问答样例运行 `ai-test` 检查输出质量，再单独授权自动发送。后台模式会把买家问题和商品信息发送给所配置 LLM 服务。
