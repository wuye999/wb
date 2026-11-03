# WB Open API 备用上架与跨店内容更新

`publish`、`create-cards`、`replicate`、`import-publish` 保持原 BCS 通道。新增命令须显式调用，不替代默认接口，也不在 BCS 或 WB 失败后自动换通道重放。全部默认预览，只有 `--apply` 才提交。

## 配置与执行边界

从 BCS 的 `fetch_open_api_shops()` 获取每店自己的官方 `apiKey`，使用中国区 `.cn` 主机；BCS 只提供店铺和令牌，不参与商品同步或采集代理，不要求 WB 后台 Cookie。新功能不新增凭证字段、不启动浏览器、不自动续期。源店与目标店使用各自的令牌；401/403 时在 BCS 对应店铺更新权限或密钥。

上架需要 Content 读写权限，以及 Marketplace 的仓库读取/库存写入权限。跨店内容更新只需要 Content；不修改价格或库存，不枚举仓库。令牌类型会影响限额，低限额基础令牌可能受到 429 限制，当前不自动长时间等待或重试写入。

商品模板、类目和仓库读取先于建卡写入；建卡受理后只允许为完成后续上架而有限查询待办完整 VC 的新商品/规格标识。全部标识查询结束后提交就绪商品的图片与库存，记录受理结果并结束，不查询最终生效结果、不触发 BCS 同步、不运行 fetch/merge。读取失败不能当成空店。各店独立失败，其他已准备好的店可继续；某店提交失败后，该店后续商品不再发送。HTTP 成功只代表已受理，不能证明最终生效。

## 备用上架：自有源店

以下 ID 均为占位示例。`--source-shop` 与 `--shops` 都使用 BCS 店铺 ID；`--nm` 如用于源店筛选，必须是源店的 WB 商品码，不能从 VC 尾号猜测。

```powershell
python run.py publish-openapi --source-shop 1001 --shops 1002,1003 --vc "BCS-TEST-123456789" --price 59 --stock 20
python run.py publish-openapi --source-shop 1001 --shops 1002 --nm 123456789 --price 59 --dims "10*20*30/0.5"
```

源店按 `--vc`、`--nm`、`--all` 三选一；完整 VC 原样保留。读取完整官方商品卡，包括标题、说明、品牌、包装、属性、规格和证书；目标卡不带源店 nmID/chrtID，也不复制源店条码或证书 ID。每个目标店生成新条码；每请求最多提交100组独立商品，保留完整规格，不把多个规格缩成第一条。

`--price` 是目标店币种的正整数，应用于新卡全部规格，不自动借用或换算源店价格。`--stock` 默认999，显式0保留。`--dims` 可覆盖包装 cm/kg；不传时使用源卡包装。标题超过60字符、必需属性缺失、无高清图片、规格不完整或尺寸/价格无效时阻止该项。

目标在架商品、回收站卡、共用的 `复制上架记录.json` 和本通道阶段记录均用于查重。不自动恢复回收站卡，不把已有提交记录清空后重建。目标每店必须先确认唯一可用的莫斯科仓名称和 `id`，不使用 office 标识、不回退其他仓。目标店铺必须显式指定，不能包含源店。

## 备用上架：任意 WB 商品码

官方 Content 不能读取其他卖家的私有商品卡。这种来源从 WB 公开 CDN 的俄文 `card.json` 获取内容，再由目标店官方 Open API 建卡，不经过 BCS 商品接口。

```powershell
python run.py publish-openapi --nm 123456789 --vc "BCS-TEST-123456789" --shops 1002 --price 59 --dims "10*20*30/0.5"
python run.py publish-openapi --nm 123456789,123456790 --vc "BCS-TEST-{nm}" --shops 1002 --price 59 --dims "10*20*30/0.5"
python run.py publish-openapi --file "examples/publish-openapi.example.json" --shops 1002
```

公开详情按俄文属性名关联目标类目的官方 charcID；数值属性仅接受明确的数值及匹配单位，不猜测混合文本。无法获得必需属性时阻止提交，按错误中的属性名补齐文件。标题不会自动截断。公开详情不保证包含完整服装尺码表，默认只建无尺码规格 `techSize=0`；有尺码商品必须在输入文件的 `card.sizes` 中显式列出全部规格，不能把默认值当作原商品完整规格。

JSON 文件是非空对象数组，每项为 `nmId`、`vendorCode`、`price`，可用 `card` 对象覆盖官方字段，例如 `title`、`description`、`dimensions`、`characteristics`、`sizes`、`documents`。`card` 仍是源模板，提交前会剥离源标识和条码。属性 ID 和证书须对应实际类目与商品；样例中的 ID、描述和金额只是语法示例。每项可指定整数 `price`，否则使用 CLI 的 `--price`。可将文件放在自己的 `data/` 或 `_scratch/`。

## 合并上架、有限等待与续办

官方建卡是异步的，首次受理不返回新 nmID，服务同步可能需要30分钟。现在由同一个 `publish-openapi --apply` 完成“生成条码 → 建卡 → 有限等待完整商品/规格标识 → 上传图片 → 设置莫斯科库存”。默认预览不生成条码、不等待或写入。图片顺序、原计划库存和每阶段回执保存在 `data/state/WB_OpenAPI上架记录.json`。本通道创建时仅准备图片，不自动复制源视频。

默认 `--wait-timeout 1800`、`--wait-interval 15`，单位秒；总等待范围0..7200，间隔范围1..60，`0` 表示只查询一轮。仍遵守平台限流；单次读取有独立 HTTP 超时，实际结束可能超过等待时限一个正在执行的读取。等待按时间和轮次双重限制，单次休眠不超过30秒。只按待办完整 VC 查询非回收站商品（包括无图片商品），不全量刷新本地商品快照。

```powershell
python run.py publish-openapi --source-shop 1001 --shops 1002 --vc "BCS-TEST-123456789" --price 59 --stock 20 --wait-timeout 1800 --wait-interval 15 --apply
```

取得本店 nmID 后还要等待完整 chrtID 和本次生成的全部规格条码。模糊搜索命中的其他 VC 不使用；未知条码、类目冲突或重复标识阻止提交。某些商品超时，其他就绪商品仍可完成后续提交，但整轮返回非零，不能把建卡受理算成完整上架成功。读取错误或401/403/429停止该店；其他店可继续。图片/库存接口若仍因平台服务延迟而拒绝，不盲目重试写入，保留实际阶段和错误。

超时或中断后，按保存的原计划续办，不需要重新获取源商品详情：

```powershell
python run.py publish-openapi --resume --shops 1002 --vc "BCS-TEST-123456789"
python run.py publish-openapi --resume --shops 1002 --vc "BCS-TEST-123456789" --wait-timeout 1800 --apply
```

`--resume` 仅使用 `--shops/--vc` 选择已受理记录，不接受新的来源、文件、价格、包装或库存参数；沿用原图片与库存计划。续办再次确认莫斯科仓名称和 `id` 与原记录一致，再查询本店完整标识并提交剩余阶段。图片和库存分别记录受理回执；已受理阶段不重复提交。图片成功后库存失败也会保留图片受理记录，并返回非零状态。重新执行原建卡命令，遇到本通道已受理记录也只续办，已完成记录跳过，不重复建卡。

旧命令 `publish-openapi-finish` 已合并到 `publish-openapi --resume`；已有 version=1 阶段记录可直接使用，无需迁移或清空。

建卡或任一后续写入处于 `submitting/unknown` 时，禁止自动重放；需要维护者依据原回执或用户另行授权的诊断处理记录，不能通过清空状态重试。条码准备阶段失败但尚未提交建卡可按原范围重新执行。记录与共用上架去重文件的作用见 [数据契约](DATA.md#关键状态文件)。

## 按完整 VC 同步修改到其他店

在源店改好商品后，读取该店最新官方卡片，将选择的内容覆盖到目标店相同完整 VC 的商品。目标店 nmID 和 chrtID 独立解析，绝不使用源店商品码或 VC 尾号替代目标标识。

```powershell
python run.py sync-cards-openapi --source-shop 1001 --vc "BCS-TEST-123456789" --shops 1002,1003
python run.py sync-cards-openapi --source-shop 1001 --nm 123456789 --fields title,description,dimensions --apply
python run.py sync-cards-openapi --source-shop 1001 --all --shops 1002 --limit 10
```

`--source-shop` 填袁州1店对应的 BCS ID；不按店名模糊猜测。`--vc/--nm/--all` 三选一。省略 `--shops` 时目标是 BCS 中除源店外的全部店；显式目标不能包含源店。`--limit` 限制源商品数量。目标缺少同 VC 时跳过，不新建、不恢复回收站商品；类目不同、VC/标识重复或关键字段缺失则报失败。

默认字段为 `title,description,brand,dimensions,characteristics,photos`，分别是标题、说明、品牌、包装尺寸/重量、属性和图片。`--fields` 是完整选择列表，不是追加列表；可显式选择下列额外字段：

| 字段 | 行为与限制 |
| --- | --- |
| `sizes` | 保留目标 chrtID/条码；单规格可更新尺码名称，多规格只能按唯一的 techSize/wbSize 匹配，不按位置猜测，不自动增删规格 |
| `documents` | 复制源证书内容并去掉源证书 ID；默认不跨店覆盖证书，未选中时完整携带目标证书 |
| `video` | 使用源视频直链；只能上传 MP4/MOV，官方常见的 HLS/m3u8 链接会阻止该项，不能猜测原视频 URL |
| `gtin`、`kizMarked` | 显式覆盖额外 GTIN 或商品标记确认；默认保留目标值 |

官方更新是完整覆盖，因此未选字段也会从本店执行前商品卡保留并一并提交，保留目标全部规格、条码、证书和标记信息；不传只读的属性名称、尺寸有效性、证书审核结果。价格、折扣、库存、商品归组、标签不在内容同步范围。

图片/视频另走媒体接口，按源图片顺序整体替换；只改图片时保留目标视频，只改视频时保留目标图片。需保留的视频若只有 m3u8 链接，会阻止整项媒体更新，避免静默丢失视频；可用 `--fields title,description,dimensions` 单独处理其他内容。空媒体集无法提交，源字段缺失不会当成清空。媒体链接列表相同时不发送；其他所选字段也相同则整项跳过。

每次预览与提交均在 `data/logs/` 保存 JSON，包含源/目标商品码、计划字段、载荷和各阶段受理回执。商品内容成功而媒体失败时记录部分失败，命令返回非零，不宣称全部完成。日志不含令牌、Cookie 或原始错误响应；开发回归使用合成数据，不执行真实店铺写入。

协议核对日期：2026-11-02，合并流程更新于2026-11-03。字段与异步限制依据 [WB 官方中国区商品接口](https://dev.wildberries.cn/openapi/item-management)，工作流概览见 [官方调用顺序](https://dev.wildberries.cn/cases/135/the-wb-api-call-map)。本项目只保留完成后续上架所需的新标识轮询，图片/库存提交后不查询最终结果。
