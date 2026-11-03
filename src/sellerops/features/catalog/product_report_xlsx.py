"""只读运营报告工作簿：指标、证据、覆盖和失败原因分表展示。"""
import os
from pathlib import Path
import tempfile
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from sellerops.domain.product_reports import ProductReport
from sellerops.features.catalog.product_report_analysis import analyze

RETURN_COLUMNS = [('orders', '商品件数'), ('combined_returns', '取消及退货合计'),
                  ('return_rate', '兼容退货率'), ('done_rate', '完单口径兼容退货率'),
                  ('cancel', '取消数'), ('cancel_rate', '取消率'), ('returned', '退货数'),
                  ('defective', '瑕疵退货数'), ('actual_rate', '实际退货数/商品件数'),
                  ('pending', '未完结数'), ('buyout', '买断数'), ('unknown', '未知状态数')]
SALES_COLUMNS = [('key', '完整VC/未知分组'), ('cn', '商品中文名'), ('orders7', '近7天登记单数'),
                 ('qty7', '近7天登记件数'), ('orders14', '近14天登记单数'), ('qty14', '近14天登记件数'),
                 ('sources', '来源店铺（包含外部店）'), ('urls', '来源商品链接'),
                 ('known_negative', '已授权范围内已知差评数'), ('risk_scope', '风险数据范围'),
                 ('sales_scope', '飞书数据完整性'), ('notes', '来源数据问题')]


def _sheet(wb: Workbook, title: str, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
    ws = wb.create_sheet(title)
    ws.append([label for _, label in columns])
    for row in rows:
        ws.append([row.get(key) for key, _ in columns])
    for cells in ws.iter_rows():
        for cell in cells:
            if isinstance(cell.value, str):
                # 所有外部文本写成字符串，禁止评价/商品名称被 Excel 当公式执行。
                cell.data_type = 's'
                if cell.value.startswith(('https://www.wildberries.ru/catalog/', 'https://www.wildberries.cn/catalog/')) and '\n' not in cell.value:
                    cell.hyperlink = cell.value
            cell.font = Font(name='微软雅黑', size=10)
            if cell.hyperlink:
                cell.font = Font(name='微软雅黑', size=10, color='0563C1', underline='single')
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    for cell in ws[1]:
        cell.fill = PatternFill('solid', fgColor='17365D')
        cell.font = Font(name='微软雅黑', size=10, color='FFFFFF', bold=True)
    for i, (key, label) in enumerate(columns, 1):
        letter = ws.cell(1, i).column_letter
        ws.column_dimensions[letter].width = (100 if key == 'value' else 48
                                             if key in {'urls', 'url', 'text', 'cons', 'note', 'message'}
                                             else min(34, max(16, len(label) * 2 + 2)))
        if key.endswith('_rate'):
            for cells in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cells[0].number_format = '0.00%'
        elif key == 'rating':
            for cells in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cells[0].number_format = '0.00'
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 34
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def write_report(report: ProductReport, path: Path | str) -> None:
    """原子保存 xlsx；空白数值代表未知或分母为零，不能补零。"""
    target = Path(path)
    if target.suffix.lower() != '.xlsx':
        raise ValueError('运营报告输出必须为 .xlsx')
    result = analyze(report)
    wb = Workbook()
    wb.remove(wb.active)
    metadata = [
        ('生成时间', report.generated_at), ('报告日期时区', 'UTC+8'),
        ('订单下单日期范围', f'{report.order_begin} 至 {report.end}，含首尾日期'),
        ('评价范围', f'{report.feedback_begin or "接口可访问的全部历史"} 至 {report.end}'),
        ('官方主机区域', report.region), ('数据状态', '部分数据缺失，请先查看错误' if report.failed else '所选数据采集完成'),
        ('兼容退货率', '(取消+退货+瑕疵退货)/商品件数；完单口径分母=商品件数-未完结，保留旧口径'),
        ('实际退货比例', '(退货+瑕疵退货)/商品件数；不是已售商品的售后退货率'),
        ('未知状态', '计入商品件数并单列，未猜测其是否完结；完单口径保留旧分母，需人工核对'),
        ('评价统计', '1–2星为差评；评分为所取评价星级的算术平均，不等于前台合并卡评分'),
        ('评价覆盖', '未处理、已处理及归档按评价ID去重；追加评价关系保留，不冒充前台当前可见评价数'),
        ('空白与零', '空白为未知或无法计算；零仅表示已取范围未发现。部分数据均仅为已知部分'),
        ('商品覆盖', '仅比较本地快照；快照中未见不等于实时缺少。提交记录不等于已生效'),
        ('仓库范围', '订单动态srid关联同店FBS订单rid，以warehouseId核对目录确认的莫斯科仓id；评价无仓库维度'),
        ('飞书口径', '一条登记记录计一单，订单量求和为件数；全部来源店铺，近7天和14天均含今天'),
        ('出单与风险', '所有出单候选均保留，不自动扣除风险VC；外部或无授权店铺风险未知'),
        ('操作边界', '仅生成本地报告；未上架/下架/修改/写飞书，未同步或更新快照'),
    ]
    _sheet(wb, '数据覆盖说明', [('key', '项目'), ('value', '说明')],
           [{'key': key, 'value': value} for key, value in metadata])
    coverage = [{**shop, **report.coverage.get(shop['id'], {})} for shop in report.shops]
    _sheet(wb, '店铺数据覆盖', [('id', '店铺ID'), ('name', '店铺'), ('warehouse_id', '莫斯科仓ID'),
                            ('warehouse_name', '莫斯科仓名称'), ('snapshot', '本地快照'),
                            ('fetched_at', '快照时间'), ('orders', '订单采集'), ('feedbacks', '评价采集'),
                            ('outside_warehouse', '未计入其他仓订单数'), ('outside_created', '下单日期越界数'),
                            ('fbs_orders', '仓库归属关联订单数')], coverage)
    common = [('key', '统计分组'), ('cn', '商品中文名'), *RETURN_COLUMNS,
              ('shops', '统计涉及店铺'), ('vcs', '完整VC'), ('urls', '店铺商品链接'), ('scope', '订单数据覆盖')]
    _sheet(wb, '中文名退货汇总', common, result['by_cn'])
    _sheet(wb, 'VC退货汇总', common, result['by_vc'])
    _sheet(wb, 'VC差评汇总', [('key', '完整VC/未知分组'), ('cn', '商品中文名'),
                             ('negative', '已知1–2星差评数'), ('feedback_count', '已取评价数'),
                             ('rating', '已取评价均分'), *[(f'star_{i}', f'{i}星数') for i in range(1, 6)],
                             ('shops', '统计涉及店铺'), ('urls', '店铺商品链接'),
                             ('scope', '评价数据覆盖')], result['vc_feedback'])
    _sheet(wb, '店铺链接指标', [('shop_id', '店铺ID'), ('shop_name', '店铺'), ('vc', '完整VC'),
                             ('nm_id', '本店WB商品码'), ('cn', '商品中文名'), ('url', '商品链接'),
                             ('order_scope', '订单完整性'), *RETURN_COLUMNS,
                             ('feedback_scope', '评价完整性'), ('feedback_count', '已取评价数'),
                             ('rating', '已取评价均分'), ('negative', '已知1–2星差评数'),
                             *[(f'star_{i}', f'{i}星数') for i in range(1, 6)],
                             ('source', '链接信息来源'), ('note', '人工判断提示')], result['links'])
    _sheet(wb, '差评明细', [('shop_id', '店铺ID'), ('shop_name', '店铺'), ('vc', '完整VC'),
                          ('nm_id', '本店WB商品码'), ('cn', '商品中文名'), ('url', '商品链接'),
                          ('feedback_id', '评价ID'), ('created', '评价时间UTC+8'), ('stars', '星级'),
                          ('text', '评价正文'), ('pros', '优点'), ('cons', '缺点'), ('state', '处理状态'),
                          ('parent_id', '原评价ID'), ('child_id', '追加评价ID')], result['negative'])
    _sheet(wb, '近7及14天出单', SALES_COLUMNS, result['sales'])
    _sheet(wb, '飞书出单明细', [('日期', '付款日期UTC+8'), ('vc', '完整VC'), ('cn', '商品中文名'),
                              ('shops_text', '来源店铺'), ('nm_id', '来源WB商品码'), ('url', '商品链接'),
                              ('source_url', '登记原始链接'), ('order_id', '订单编号'), ('qty', '登记件数'),
                              ('record_id', '飞书记录ID'), ('notes', '来源数据问题')],
           [{**row, 'shops_text': '、'.join(row['shops'])} for row in report.sales])
    _sheet(wb, '店铺链接缺口', [('shop_id', '目标店铺ID'), ('shop_name', '目标店铺'), ('state', '本地覆盖状态'),
                              ('target_urls', '目标店铺已有链接'), ('fetched_at', '目标快照时间'),
                              ('submitted', '已有提交记录时间'), *SALES_COLUMNS], result['gaps'])
    _sheet(wb, '订单明细', [('shop_id', '店铺ID'), ('shop_name', '店铺'), ('vc', '完整VC'),
                          ('nm_id', '本店WB商品码'), ('cn', '商品中文名'), ('url', '商品链接'),
                          ('order_id', '商品订单srid'), ('created', '下单时间UTC+8'),
                          ('updated', '状态时间UTC+8'), ('status', '订单状态'), ('cancel_type', '取消类型'),
                          ('warehouse_id', '已确认莫斯科仓ID'), ('warehouse', '匹配仓库名称'),
                          ('warehouse_report_name', '订单动态原始仓库名称'),
                          ('price', '卖家价格')], result['orders'])
    _sheet(wb, '异常与缺失', [('severity', '级别'), ('source', '数据来源'), ('shop_id', '店铺ID'),
                             ('message', '原因')], [vars(issue) for issue in report.issues])
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.product-report-', suffix='.xlsx', dir=target.parent)
    os.close(handle)
    try:
        wb.save(temporary)
        os.replace(temporary, target)
    finally:
        wb.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
