# wb_ops · 鉴权与凭证说明（CREDENTIALS.md）

> 统一凭证文件：`data/credentials.json`（⚠️ 含真实令牌，**严禁提交 git / 分享 / 上传云端**）。

## 一、凭证文件结构

```json
{
  "bcs": {
    "base_url": "https://wb.bcserp.com/prod-api",
    "token": "<BCS 登录 JWT>",
    "limit_key": "<userId.base64url>",
    "cookie_extra": "_c_WBKFRo=...; sidebarStatus=1"
  },
  "wb": {
    "root_version": "v1.108.1",
    "shops": [
      { "shopName": "主号7", "shopId": 5272,
        "authorizev3": "<用户级 JWT>",
        "wb_seller_lk": "<店铺级 JWT，含 Z-Sid>",
        "cookie": "_wbauid=...; x-supplier-id-external=...; cfidsw-wb=..." }
      // ... 每家店一项（作者环境共 5 店：5272/5273/5276/5280/5281；换成你自己的店铺）
    ]
  }
}
```

> ⚠ 上面的 `shopName`/`shopId` 及后文的 sid 前缀、店铺清单都是**作者账号的店铺**，换账号请全部替换成你自己的。

- 由 `wb_ops/credentials.py` 统一加载，提供 `bcs_headers()`、`wb_shop(sid)`、`wb_shop_ids()` 等访问器。
- `wb.py cookies-update` 会自动写回本文件的 `wb.shops`。

## 二、两套鉴权体系

### A. BCS 云端 API（wb.bcserp.com/prod-api）

| 字段 | 位置 | 说明 |
|---|---|---|
| `Authorization: Bearer <token>` | `bcs.token` | BCS 登录 JWT；同时出现在 Cookie 的 `Admin-Token` |
| `X-Limit-Key` | `bcs.limit_key` | 必填，格式 `<userId>.<43位base64url>`；缺失时网关返回 **405**（非 401） |
| `Cookie` | `bcs.token`+`bcs.limit_key`+`bcs.cookie_extra` | `Admin-Token` + `Limit-Key` + 额外 cookie |

- 失效表现：token 过期 → **401**；缺 Limit-Key → **405**。

### B. WB 卖家后台（seller.wildberries.ru 系）

| 字段 | 位置 | 说明 |
|---|---|---|
| `authorizev3` | 每店 | 用户级 JWT（多店可同用，同一登录用户） |
| `wb-seller-lk` | 每店 | 店铺级 JWT（EdDSA），含 Z-Sid，**每店不同** |
| `cookie` | 每店 | 整串，含 `cfidsw-wb`（Cloudflare，经 sec/api/fl 加密轮换，无法自刷新）、`x-supplier-id-external`（=Z-Sid） |

- sid 前缀 → 店铺名的映射（`cookies-update` 靠它把抓包会话匹配到店铺）：**已自动从凭证动态推导**——每家店的 `wb_seller_lk` JWT 里内嵌了稳定标识 `Z-Sid`（前 8 位即 sid 前缀），`cookies.py` 的 `build_sid_map()` 会从 `credentials.json` 的每家店解码得到映射，**无需在代码里硬编码任何店铺**。换账号/换店铺后，只要 `credentials.json` 里的 `wb_seller_lk` 是你自己的店铺凭证，`cookies-update` 即可自动匹配，无需改代码。
- 失效表现：`cfidsw-wb` 过期 → **403**。保鲜实测 ≥46 小时，建议每周刷新。

## 三、如何刷新

### WB cookie（403 时）

1. 登录对应店铺 `seller.wildberries.ru`，按 **F12** 打开开发者工具。

   - **情况 A（正常浏览器）**：直接点顶部 **Network（网络）** 标签 → 刷新页面。
   - **情况 B（受限浏览器，很常见）**：开发者工具默认只能打开「**Console 控制台**」和「**Elements 检查元素**」，看不到 Network 标签。此时在 **Console 控制台**粘贴运行下面这段 JS——它会向一个**不存在的 URL 发起请求**，产生一条失败请求；运行后**点击那条失败的红色请求**，即可进入 Network（网络）面板：

     ```javascript
     // 粘贴到 Console 控制台运行：向不存在的 URL 发起请求，用于打开受限浏览器的网络面板
     fetch('https://seller.wildberries.ru/api/__devtools_open__' + Date.now() + Math.random().toString(36).slice(2))
       .catch(() => console.log('已触发失败请求：请在控制台/网络列表点击它进入 Network 面板'));
     ```

2. 进入 Network 面板后，**刷新页面**（或触发一次操作），点任意 API 请求（如 `promotions/timeline`）→ **Request Headers**。
3. 复制三处值：`authorizev3`、`wb-seller-lk`、`Cookie:` 整串。
   > ⚠ `Cookie` 必须从 Network 的 `Cookie:` 请求头复制完整值（`document.cookie` 拿不到 HttpOnly 项）。
4. 把 fetch 块存为 md（可参考 `data/har/` 里的旧文件格式），运行：
```bash
python wb.py cookies-update data/har/我的抓包.md
```
5. 脚本按 Z-Sid 匹配店铺，自动写回 credentials.json 的 `wb.shops`。

### BCS token（401 时）

重新登录 BCS 后，把新 token / limit_key 填进 `data/credentials.json` 的 `bcs` 段。

## 四、校验

```bash
# 快速校验凭证完整性（读配置不联网）
python -c "from wb_ops import credentials; ok,p=credentials.get().validate(); print('OK' if ok else p)"
# 端到端验证
python wb.py shops       # BCS 通 = token/limit_key 有效
python wb.py promo-apply # WB 通 = 5 店 cookie 有效
```

## 五、安全注意事项

1. **凭证文件严禁外泄**：`data/credentials.json`、`data/har/*.md`（抓包源）、`_archive/`（含旧 cookies.json 与旧 config.py）都含真实令牌。
2. 若日后初始化 git，务必把 `data/credentials.json`、`data/har/`、`_archive/` 加入 `.gitignore`。
3. 不要在日志、截图、聊天里粘贴完整 token 值。
