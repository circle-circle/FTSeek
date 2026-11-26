#!/bin/bash
# 提名选择性（Lift）周报 — DEB-20261105 执行项, record-only
# 1) 用 Futu K_DAY 补齐最近 10 天日线 (只读行情) 2) 落反事实行 3) 出读数
set -uo pipefail
cd /opt/ftseek/FTSeek-US || exit 1
export PYTHONPATH=.
./venv/bin/python scripts/non_nominated_counterfactual.py --refresh-bars --days 10 || exit 1
./venv/bin/python scripts/non_nominated_counterfactual.py --report