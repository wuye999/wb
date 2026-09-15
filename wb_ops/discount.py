# -*- coding: utf-8 -*-
"""
wb_ops 折扣改价（统一入口，采用最新 WB 原生批量引擎）

默认直接调用 WB 原生 upload/task 批量接口（详见 wb_ops.discount_wb）。
若需使用旧版 BCS 慢速改折扣，请使用 wb.py discount-bcs（或 wb_ops.discount_bcs）。
"""
from . import discount_wb


def run(args):
    return discount_wb.run(args)


if __name__ == "__main__":
    discount_wb.main()
