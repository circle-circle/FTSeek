#!/usr/bin/env bash
# 盘后只读记账：|盘前跳空| 排序归因（DEB-20261106 §43–§45）
# 无成交/非交易日 → 无输出（cron 静默）；有数据 → 一行摘要。
set -u
cd /opt/ftseek/FTSeek-US || exit 0
exec ./venv/bin/python scripts/gap_rank_attribution.py --brief 2>/dev/null