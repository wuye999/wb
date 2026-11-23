"""WorkBuddy 商品归属操作。"""
import argparse

from sellerops.cli.binding import bind
from sellerops.cli.catalog import add_mismatch_args


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('candidates', help='查看待认领分组、唯一前缀和最低售价倍数参考')
    p.add_argument('--limit', type=int, default=20, help='最多返回的候选组数')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_candidates')

    p = sub.add_parser('workbench', help='生成带图待认领工作台（data/workbench/）')
    bind(p, 'sellerops.features.catalog.workbench', 'run_workbench')

    p = sub.add_parser('batch-set', help='按完整 VC 导入工作台审核结果或批量确认')
    p.add_argument('--file', help='审核结果 JSON 数组文件路径，省略时从标准输入读取')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_batch_set')

    add_mismatch_args(sub.add_parser('check-mismatch', help='生成货不对板大图画廊（按中文名分组并标记异常）'))

    for parser in sub.choices.values():
        parser.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
