"""命令定义：listing。"""
import argparse
from sellerops.cli.binding import bind
from sellerops.cli.arguments import add_shop_scope, nonempty, shop_ids, add_publish_args


def register(sub: argparse._SubParsersAction, *, planned_only: bool = False) -> None:
    p = sub.add_parser('replicate', help="跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认）")
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
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个 vc（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")


    p.add_argument("--interval", type=float, default=1.0, help="批次上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")
    bind(p, 'sellerops.features.publishing.replicate', 'run')

    p = sub.add_parser('publish', help="批量发布上架：输入 WB 商品码推送到店铺（支持指定前缀/完整VC/价格/尺寸/店铺）")
    add_publish_args(p)
    bind(p, 'sellerops.features.publishing.bulk_publish', 'run')

    p = sub.add_parser('create-cards', help="建卡发布上架：输入 WB 商品码推送到店铺（支持自定义完整VC/俄文详情与主图建卡）")
    add_publish_args(p)
    bind(p, 'sellerops.features.publishing.card_publish', 'run')

    p = sub.add_parser('publish-openapi', help='[备用] WB Open API 上架：建卡、有限等待标识、上传图片和莫斯科库存；支持续办')
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
    p.add_argument('--apply', action='store_true', help='执行完整上架或续办流程（默认仅预览，不等待、不写入）')
    bind(p, 'sellerops.features.publishing.open_api_publish', 'run')

    p = sub.add_parser('normalize-vc', help='WB Open API：纠正历史 VC 前缀，保留原尾部，冲突时追加跨店一致序号')
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--vc', default='', help='完整旧 VC，逗号分隔')
    selection.add_argument('--name', type=nonempty, help='已确认的完整中文商品名')
    selection.add_argument('--name-contains', type=nonempty, help='中文商品名包含指定文字')
    selection.add_argument('--all', action='store_true', help='全部非回收站商品；无明确归属的项记录阻止原因')
    add_shop_scope(p)
    p.add_argument('--apply', action='store_true', help='提交改码（默认预览）；保留历史防重放日志，不更新本地映射/快照')
    bind(p, 'sellerops.features.publishing.vc_normalization', 'run')

    if planned_only:
        return

    p = sub.add_parser('sync-cards', help='WB Open API：按相同完整 VC 将源店商品内容同步到其他店')
    p.add_argument('--source-shop', type=int, required=True, help='修改后的源店 BCS 店铺 ID（例如袁州1店对应 ID）')
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
