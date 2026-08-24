#!/usr/bin/env python3
"""8/24 富途通道裁决窗触发 (B1 收尾, 2026-08-22).

预注册裁决窗 8/24 到期但无任何触发安排 → 本 wrapper 跑评估脚本并输出紧凑摘要.
no_agent cron: stdout 即推送. 裁决语义由脚本按 DEB-20260812 §五 预注册判据给出.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
VENV = REPO / "venv" / "bin" / "python3"
EVAL = REPO / "scripts" / "futu_channel_shadow_eval.py"
OUT = REPO / "data" / "futu_channel_shadow_eval_latest.json"

r = subprocess.run([str(VENV), str(EVAL)], capture_output=True, text=True,
                   timeout=900, cwd=str(REPO))
if r.returncode != 0:
    print(f"❌ futu_channel_shadow_eval 失败 rc={r.returncode}\n{r.stderr[-800:]}")
    sys.exit(1)

try:
    data = json.loads(OUT.read_text())
except Exception as e:
    print(f"❌ 读取评估结果失败: {e}\nstdout尾:\n{r.stdout[-600:]}")
    sys.exit(1)

lines = ["📋 富途通道 8/24 裁决窗评估 (DEB-20260812 §五 预注册判据)"]
b = data.get("capital_flow", {})
if b.get("error"):
    lines.append(f"⚠️ capital_flow: {b['error']}")
else:
    lines.append(f"capital_flow: n={b.get('n_pairs')} 质量门{'✓' if b.get('quality_pass') else '✗'}"
                 f" 边际IC={b.get('rank_ic_marginal')} (t={b.get('ic_t_stat')},"
                 f" {'显著' if b.get('ic_significant_95') else '不显著'})")
    lines.append(f"  增量IC(vs OFI)={b.get('rank_ic_incremental_vs_ofi', 'N/A')}"
                 f" OOS同号={'✓' if b.get('oos_same_sign') else '✗'}"
                 f" 候选裁决: {b.get('verdict_candidate', '样本不足')}")
    if b.get("ofi_warning"):
        lines.append(f"  ⚠️ {b['ofi_warning']}")
a = data.get("derivative_unusual", {})
if a.get("error"):
    lines.append(f"⚠️ derivative_unusual: {a['error']}")
else:
    lines.append(f"derivative_unusual: 事件{a.get('events_dedup')} 盘中{a.get('events_intraday')}"
                 f" 配对n={a.get('n_paired')} (门n≥{a.get('n_gate')})")
    if a.get("n_paired"):
        lines.append(f"  事件后均值={a.get('post_event_mean_ret')} 超额均值={a.get('excess_mean_ret')}"
                     f" 超额胜率={a.get('excess_win_rate')}")
lines.append("判官裁决由人工按预注册原文定夺, 脚本仅给候选 (record-only 零行为变更)")
print("\n".join(lines))
