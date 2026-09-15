# -*- coding: utf-8 -*-
"""
wb_ops 折扣快速改价（discount-scan / discount-wb）

已全面升级为 WB 原生批量改折扣引擎（upload/task 接口，批量分片提交，默认不做写后验证），
替代原依赖 BCS 批量与单条回退的混合引擎。
详见 wb_ops.discount_wb。
"""
from . import discount_wb


def run(args):
    return discount_wb.run(args)


if __name__ == "__main__":
    discount_wb.main()