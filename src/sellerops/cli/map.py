"""WorkBuddy 商品归属操作。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('candidates', help='按完整 VC 和同源码查看待认领商品')
    p.add_argument('--limit', type=int, default=20, help='最多返回的候选组数')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_candidates')

    p = sub.add_parser('batch-set', help='确认明确列出的完整 VC')
    p.add_argument('--file', help='批量确认 JSON 文件路径，省略时从标准输入读取')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_batch_set')

    p = sub.add_parser('export-pending', help='导出六列待确认 Excel 表')
    p.add_argument('--out', default='', help='输出路径，默认 exports/待确认商品.xlsx')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_export')

    p = sub.add_parser('import-excel', help='导入六列表的运营最终确认')
    p.add_argument('file', help='待确认商品 Excel 路径')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_import')

    for parser in sub.choices.values():
        parser.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
