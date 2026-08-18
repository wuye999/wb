# data/ 目录说明（克隆后你需要在这里创建自己的文件）

> 本目录被 `.gitignore` 整体忽略，**仓库里不含任何数据**。克隆后请按下面结构，在本地 `data/` 下创建你自己的文件。

## 必须创建

### 1. `data/credentials.json` —— 统一凭证

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
      {
        "shopName": "店铺名",
        "shopId": 12345,
        "authorizev3": "<用户级 JWT>",
        "wb_seller_lk": "<店铺级 JWT，含 Z-Sid>",
        "cookie": "_wbauid=...; x-supplier-id-external=...; cfidsw-wb=..."
      }
    ]
  }
}
```

凭证获取方法见 `../docs/CREDENTIALS.md`。⚠️ 本文件含真实令牌，**严禁提交到 git / 分享**。

### 2. `data/商品价格表.xlsx` —— 商品清单（唯一权威）

7 列结构（第一行表头）：`产品图片 / 卖家SKU / 产品中文名 / 双倍售价 / 尺寸 / 最低售价 / vendorCode前缀码`

- 第 4 列「双倍售价」= 最低售价 ×2（店铺价 = floor(双倍售价)）。
- 第 7 列「vendorCode前缀码」：4 位大写、全局唯一（上架时用于生成 `BCS-{前缀}-{WB原始nmId}`）。

## 自动生成（无需手动创建）

- `products/` —— `wb.py fetch` 拉取的各店商品快照
- `state/` —— 同步状态 / 自动新增清单
- `workbench/` —— 生成的工作台 HTML
- `logs/` —— 运行日志与结果 CSV
- `价格映射表.xlsx` —— `wb.py merge` 自动重建（唯一状态源，勿手改）

## 可选（刷新凭证时用）

- `har/` —— 从浏览器抓包导出的 md/har 文件，供 `wb.py cookies-update` 消费
