#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周报包装器: 以 --weekly 模式运行 dual_caliber_report.py (no_agent cron 不支持传参)。"""
import sys

sys.argv.append("--weekly")
import dual_caliber_report  # noqa: E402 (同目录)

dual_caliber_report.main(sys.argv[1:])
