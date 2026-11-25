"""命令定义：listing。"""
import argparse
from sellerops.cli.binding import bind
from sellerops.cli.arguments import add_shop_scope, nonempty, shop_ids, add_publish_args


def register(sub: argparse._SubParsersAction, *, planned_only: bool = False) -> None:
    p = sub.add_parser('replicate', help='[BCS建卡] 跨店复制上架：把部分覆盖的商品上架到缺失店铺（默认预览）')
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--vc", type=nonempty, help="完整 VC")
    selection.add_argument("--prefix", type=nonempty, help="四位前缀")
    selection.add_argument("--name", type=nonempty, help="完整中文商品名")
    selection.add_argument("--name-contains", type=nonempty, help="中文商品名包含指定文字")
    selection.add_argument("--all-products", action="store_true", help="源店全部商品")
    add_shop_scope(p)
    p.add_argument("--source-shops", default="", help="源店铺 id 逗号分隔（默认全部本地店铺，按完整 VC 去重）")
    p.add_argument("--price-source", choices=["snapshot", "minimum"], default="snapshot",
                   help="价格来源：snapshot=源店快照价格；minimum=商品价格表最低售价")
    p.add_argument("--price-multiplier", type=float, default=1.0, help="价格来源乘数，按分四舍五入（默认 1）")
    p.add_argument("--stock", type=int, default=999, help="上架库存（默认999，可显式设为0）")
    p.add_argument("--limit", type=int, default=0, help="初筛后最多准备 N 个候选 VC（0=不限）")
    p.add_argument("--apply", action="store_true", help="兼容保留；确认预览生成的计划单后，用 apply --plan 提交")


    p.add_argument("--interval", type=float, default=1.0, help="批次上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")
    bind(p, 'sellerops.features.publishing.replicate', 'run')

    p = sub.add_parser('publish', help='[旧通道/不保留完整VC，日常优先用 create-cards] 批量推送 WB 商品到店铺')
    add_publish_args(p)
    bind(p, 'sellerops.features.publishing.bulk_publish', 'run')

    p = sub.add_parser('create-cards', help='[BCS建卡] 按 WB 商品码上架，支持保留完整 VC 和自定义详情')
    add_publish_args(p)
    bind(p, 'sellerops.features.publishing.card_publish', 'run')

    p = sub.add_parser('publish-openapi', help='[备用/仅BCS通道不可用时] WB Open API 上架，支持按原记录续办')
    p.add_argument('--source-shop', type=int, help='自有源店 BCS 店铺 ID；不传则从公开 CDN 读取任意商品码')
    p.add_argument('--nm', default='', help='源 WB 商品码，逗号分隔；自有源店模式使用该店商品码')
    p.add_argument('--vc', default='', help='源店模式为完整 VC 筛选；公开商品模式指定目标完整 VC（支持 {nm} 模板）')
    p.add_argument('--all', action='store_true', help='源店全部非回收站商品；仅自有源店模式')
    p.add_argument('--file', default='', help='公开商品 JSON 对象数组：nmId/vendorCode/price，可用 card 覆盖官方字段')
    p.add_argument('--shops', type=shop_ids, required=True, help='目标店铺 ID，逗号分隔；不可包含源店')
    p.add_argument('--price', type=int, help='新建全部规格的整数价格（目标店币种）；文件可逐项指定 price')
    p.add_argument('--dims', default='', help='覆盖包装尺寸：长*宽*高/毛重，单位 cm/kg')
    p.add_argument('--stock', type=int, default=999, help='莫斯科仓各规格库存（默认 999，0 保留）')
    p.add_argument('--limit', type=int, default=0, help='最多选择 N 个源商品（0=不限）')
    p.add_argument('--resume', action='store_true', help='按 --shops/--vc 续办已有记录，不重新建卡；沿用原图片和库存计划')
    p.add_argument('--wait-timeout', type=float, default=1800, help='等待完整商品/规格标识的总时限秒（默认1800，0=只查询一次，最多7200）')
    p.add_argument('--wait-interval', type=float, default=15, help='标识查询轮次间隔秒（默认15，范围1..60；仍遵守接口限流）')
    p.add_argument('--apply', action='store_true', help='兼容保留；确认上架或续办计划单后，用 apply --plan 提交')
    bind(p, 'sellerops.features.publishing.open_api_publish', 'run')

    p = sub.add_parser('normalize-vc', help='WB Open API：纠正历史 VC 前缀，保留原尾部，冲突时追加跨店一致序号')
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--vc', default='', help='完整旧 VC，逗号分隔')
    selection.add_argument('--name', type=nonempty, help='已确认的完整中文商品名')
    selection.add_argument('--name-contains', type=nonempty, help='中文商品名包含指定文字')
    selection.add_argument('--all', action='store_true', help='全部非回收站商品；无明确归属的项记录阻止原因')
    add_shop_scope(p)
    p.add_argument('--apply', action='store_true', help='兼容保留；确认改码计划单后，用 apply --plan 提交')
    bind(p, 'sellerops.features.publishing.vc_normalization', 'run')

    if planned_only:
        return

    p = sub.add_parser('import-publish', help='[BCS建卡] 导入标准商品表并上架（按 --prefix + 源数字码生成目标VC，默认预览，加 --apply 提交）')
    p.add_argument('--file', required=True, help='标准商品表 .xlsx 或 UTF-8 .csv')
    p.add_argument('--shops', type=shop_ids, required=True, help='目标店铺 ID，逗号分隔')
    p.add_argument('--prefix', required=True, help='目标 VC 前缀，4 位大写英文字母')
    p.add_argument('--price-multiplier', type=float, default=1.0, help='表中售价的乘数（默认 1.0）')
    p.add_argument('--price', type=float, help='统一售价（CNY），覆盖表中售价和乘数')
    p.add_argument('--stock', type=int, default=999, help='上架库存（默认 999，可设 0）')
    p.add_argument('--limit', type=int, default=0, help='查重与校验后最多提交的商品组数（默认 0，不限）')
    p.add_argument('--interval', type=float, default=0.5, help='商品组提交间隔秒（默认 0.5）')
    p.add_argument('--apply', action='store_true', help='提交 BCS 建卡（默认只预览）')
    bind(p, 'sellerops.features.publishing.import_publish', 'run')

    p = sub.add_parser('sync-cards', help='WB Open API：按相同完整 VC 将源店商品内容同步到其他店')
    p.add_argument('--source-shop', type=int, required=True, help='修改后的源店 BCS 店铺 ID（例如示例店铺对应的 1001）')
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--vc', default='', help='源店完整 VC，逗号分隔')
    selection.add_argument('--nm', default='', help='源店 WB 商品码，逗号分隔')
    selection.add_argument('--all', action='store_true', help='源店全部非回收站商品')
    add_shop_scope(p)
    p.add_argument('--fields', default='title,description,brand,dimensions,characteristics,photos',
                   help='同步字段：title,description,brand,dimensions,characteristics,photos（默认）；可选 sizes,documents,video,gtin,kizMarked')
    p.add_argument('--limit', type=int, default=0, help='最多选择 N 个源商品（0=不限）')
    p.add_argument('--apply', action='store_true', help='真正提交内容/媒体更新（默认预览）')
    bind(p, 'sellerops.features.publishing.open_api_sync', 'run')
