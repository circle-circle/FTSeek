"""UE LONG 禁入先行验证脚本的契约钉 (2026-09-23).

钉住的是**不可静默漂移的纪律面**, 不是数字:
  1. 只读生产库 (mode=ro URI) + 产物落 data/reports (T6: 禁 /tmp 依赖);
  2. 判据在脚本头预注册 (G1..G8 逐条在位) — 判据被删/被改即等于重新裁决;
  3. 安慰剂固定 seed + 固定重抽次数 (可复现);
  4. 产物含两臂 Δ + 8 门判定字段 (缺字段 = 读数不可复核).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ue_long_cut_replay.py"
PRODUCT = REPO / "data" / "reports" / "ue_long_cut_replay_20260923.json"


def test_script_is_read_only_and_writes_to_data_reports():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "mode=ro" in src, "回放脚本必须只读生产库 (mode=ro URI)"
    assert "data/reports/" in src or '"reports"' in src or "reports" in src
    assert "/tmp" not in src.replace("# ", ""), "产物禁落 /tmp (T6: 重启即失, 下游断供)"
    assert "UPDATE" not in src.upper() and "INSERT" not in src.upper(), "回放脚本禁写生产库"


def test_criteria_are_preregistered_in_script_header():
    src = SCRIPT.read_text(encoding="utf-8")
    for token in ("G1 样本量", "G2 显著性", "G3 边际", "G4 安慰剂", "G5 误杀", "G6 轴有效性",
                  "G7 重叠", "G8 切窗", "见数前定稿"):
        assert token in src, f"预注册判据缺 {token} (判据漂移 = 重新裁决)"
    assert re.search(r"SEED\s*=\s*\d+", src), "安慰剂必须固定 seed (可复现)"
    assert re.search(r"N_PLACEBO\s*=\s*\d+", src), "安慰剂重抽次数必须显式固定"


def test_product_carries_two_arms_and_gate_verdicts():
    if not PRODUCT.exists():  # 首次运行前允许缺 (产物由脚本生成)
        return
    d = json.loads(PRODUCT.read_text(encoding="utf-8"))
    for k in ("blocked", "ue_short", "retained_pool", "dA_no_redeploy", "dB_redeploy_at_pool_mean",
              "G1_n_ge_40", "G2", "G3", "G4", "G5", "G6_axis", "G8_windows"):
        assert k in d, f"产物缺 {k} (读数不可复核)"
    b = d["blocked"]
    assert b["n"] > 0 and "net_mean" in b and "net_median" in b and "day_t" in b, "拦截组须 mean+median+日聚类t 三列 (T18)"