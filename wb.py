#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wildberries/BCS 卖家自动化 · 统一入口

用法：python wb.py <子命令>
  子命令清单见 docs/CLI.md，或在命令行运行 `python wb.py --help`。

本文件只是薄启动器，所有逻辑在 wb_ops 包内。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wb_ops.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
