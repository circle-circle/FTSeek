"""市场环境账本 (RFC-190 源头化落地) 的纯函数承重测试。

守三件:
1. `pct_rank` 含自身且 0-100, 样本内最大值必须是 100 —— 口径错了会把"历史最高恐慌"
   显示成 99 分位, 后续所有"分位分桶"结论跟着漂;
2. `realized_vol` 用**日对数收益**且样本不足返回 None (不许用 0 冒充"低波动");
3. `proxy_gap` 的符号: >0 = 代理低估真值。符号反了会把"代理偏低 36%"读成"偏高"。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from market_env_ledger import (  # noqa: E402
    bucketize,
    pct_change,
    pct_rank,
    proxy_gap,
    realized_vol,
)


def test_pct_rank_inclusive_and_bounded():
    window = [10.0, 20.0, 30.0, 40.0]
    assert pct_rank(10.0, window) == 25.0   # 含自身
    assert pct_rank(40.0, window) == 100.0
    assert pct_rank(5.0, window) == 0.0
    assert pct_rank(1.0, []) is None


def test_realized_vol_needs_enough_samples_and_uses_log_returns():
    flat = [100.0] * 25
    assert realized_vol(flat, 20) == 0.0
    assert realized_vol([100.0] * 10, 20) is None      # 样本不足 → None, 不用 0 冒充
    # 已知序列: 每日 +1% 恒定 → 收益标准差 0
    seq = [100.0 * (1.01 ** i) for i in range(21)]
    assert realized_vol(seq, 20) == 0.0
    # 有波动时必须 > 0 且年化 (×√252)
    zig = [100.0, 102.0] * 11
    rv = realized_vol(zig, 20)
    assert rv is not None and rv > 10


def test_proxy_gap_sign_means_proxy_understates():
    # 代理 18.04, 真值(前收) 28.18 → 代理低估 → 负号
    assert proxy_gap(18.04, 28.18) == -35.98
    assert proxy_gap(30.0, 20.0) == 50.0
    assert proxy_gap(None, 20.0) is None
    assert proxy_gap(18.0, None) is None


def test_pct_change_guards_zero_and_none():
    assert pct_change(100.0, 110.0) == 10.0
    assert pct_change(None, 110.0) is None
    assert pct_change(0.0, 110.0) is None


def test_bucketize_sums_and_reports_missing():
    rows = [
        {"qqq_ret_pct": -2.5, "net": -100.0},
        {"qqq_ret_pct": -1.2, "net": 50.0},
        {"qqq_ret_pct": 0.5, "net": 30.0},
        {"qqq_ret_pct": None, "net": 999.0},   # 缺失不得被算进任何桶
    ]
    out = bucketize(rows, "qqq_ret_pct", [(-100, -2), (-2, 100)])
    assert out[0] == {"bucket": "[-100,-2)", "n": 1, "net": -100.0}
    assert out[1] == {"bucket": "[-2,100)", "n": 2, "net": 80.0}
    assert out[2] == {"bucket": "(missing)", "n": 1, "net": 0.0}