#!/usr/bin/env python3
"""[RFC-123 阶段0 / DEB-20261007 F1] 双锚影子**接线活体核验** (一次性, 开盘后跑).

为什么需要它 (纪律, 不是仪式):
  埋点改的是 `app/playbook/*` ⇒ HotReloader 穿不透, 必须 `systemctl restart` 才生效;
  而「代码提交 + 服务重启 + 测试全绿」都**不等于**生产真在落行 ——
  同型事故实锤: ①2026-09-01 archetype 影子因 `raw_instruction_json` 已是 dict 而
  `json.loads` 抛 TypeError 被静默吞 ⇒ **首日全灭, 攒样实际从未开始**;
  ②`ue_earnings_clue_shadow` write-only 38 天零消费方; ③eval_script 从未挂 cron ⇒
  current_n 永不更新, 「影子在攒样」是假象。
  ⇒ 本脚本只干一件事: **读生产 jsonl 真值**, 判埋点是否活着, 并给判据①的前置读数。

判读 (三态, 严禁把「零行」读成「没问题」):
  LIVE        — anchor_recorded 行 > 0 且注册表 current_n 同步 > 0 ⇒ 接线活, 攒样已开始
  DEAD        — 服务已重启且过了盘中时段, 却 0 行 ⇒ **接线死** (须查 emit 是否被吞:
                grep 日志 `[双锚影子]` warning / 开关是否真被 pb_cfg 读到)
  TOO_EARLY   — 尚未到盘中时段 (auto_execute 未跑) ⇒ 本次不可判, 不算失败

只读: 不改任何文件、不写 DB、不拉行情。冷跑无需 PYTHONPATH (自带 sys.path.insert)。
用法: venv/bin/python scripts/verify_entry_anchor_dual_live.py
回滚: 删本文件 + 删一次性 job (零生产依赖)。
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ETS = ROOT / "data" / "entry_timing_shadow.jsonl"
SCHEMA = "rfc123-s0-v1"


def _et_now() -> datetime:
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:                                          # noqa: BLE001
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def main() -> int:
    et = _et_now()
    hm = et.hour * 100 + et.minute
    # 盘中窗: 触发评估由 auto_execute 循环驱动 (09:35 起, 开盘禁触窗后), 16:00 收
    in_session = (et.weekday() < 5) and (940 <= hm <= 1600)

    if not ETS.exists():
        print(f"🔴 DEAD: 生产影子档不存在 {ETS} — 埋点写口从未落盘")
        return 1

    rows, bad = [], 0
    for ln in ETS.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:                                      # noqa: BLE001
            bad += 1

    anch = [r for r in rows if r.get("anchor_recorded") is True]
    today = [r for r in anch if str(r.get("ts", ""))[:10] == et.strftime("%Y-%m-%d")]

    # 注册表 current_n 是否同步 (口径: where:anchor_recorded=True)
    reg_n = None
    try:
        from app.playbook.shadow_registry import REGISTRY, refresh_counts
        refresh_counts()
        reg_n = next((s.current_n for s in REGISTRY if s.id == "entry_anchor_dual"), None)
    except Exception as e:                                     # noqa: BLE001
        print(f"⚠️ 注册表读数失败 (不影响 jsonl 判活): {type(e).__name__}: {e}")

    print(f"ET={et:%Y-%m-%d %H:%M} 周{et.weekday() + 1} | 盘中窗={'是' if in_session else '否'}")
    print(f"ETS 总行数={len(rows)} (坏行={bad}) | anchor_recorded 行={len(anch)} | 今日={len(today)}")
    print(f"注册表 entry_anchor_dual.current_n={reg_n} (口径 where:anchor_recorded=True)")

    if not anch:
        if not in_session:
            print("⏳ TOO_EARLY: 尚未到盘中时段 (auto_execute 未跑) ⇒ 本次不可判, 非失败")
            return 0
        print("🔴 DEAD: 已过盘中时段却 0 行 ⇒ **接线死**。排查顺序:")
        print("   1) grep '\\[双锚影子\\]' logs/ftseek_us.log  (A6 fail-loud warning = 块构造异常)")
        print("   2) 核 pb_cfg 真读到开关: playbook.execution_parameters.entry_anchor_dual_shadow_enabled")
        print("   3) 核服务启动时刻晚于提交时刻 (HotReloader 穿不透 playbook.*)")
        return 1

    # ── 活着 ⇒ 给判据①前置读数 (生成锚来源分布 / 分歧率 / missing 率) ──
    srcs = Counter((r.get("dual_anchor") or {}).get("gen_source") for r in anch)
    divs, tdiv = [], Counter()
    for r in anch:
        d = r.get("dual_anchor") or {}
        if d.get("schema") != SCHEMA:
            srcs["__schema_mismatch__"] += 1
        v = d.get("divergence_pct")
        if isinstance(v, (int, float)):
            divs.append(abs(float(v)))
        td = d.get("trigger_divergent")
        tdiv["True" if td is True else ("False" if td is False else "None(missing)")] += 1

    n = len(anch)
    miss = srcs.get("missing", 0)
    print(f"生成锚来源: {dict(srcs)}")
    print(f"missing 率 = {miss}/{n} = {100 * miss / n:.1f}%  (RFC-123 判据①门: <1%)")
    if divs:
        ds = sorted(divs)
        print(f"|分歧| 中位={st.median(ds):.4f}%  p90={ds[int(len(ds) * 0.9)]:.4f}%  "
              f"max={ds[-1]:.4f}%  (对照 E18 历史真 reprice 幅度中位 1.0101%)")
    judged = tdiv.get("True", 0) + tdiv.get("False", 0)
    if judged:
        print(f"双锚触发分歧率 = {tdiv['True']}/{judged} = {100 * tdiv['True'] / judged:.1f}%  "
              f"(RFC-123 判据②门: ≥5%; missing 行不计入分子分母)")
    print(f"trigger_divergent 分布: {dict(tdiv)}")

    if reg_n in (0, None):
        print("⚠️ jsonl 有行但注册表 current_n 未同步 ⇒ 计数口径/refresh 链路有问题 (须查)")
        return 1
    print("✅ LIVE: 埋点接线活, 攒样已开始 (阶段1 裁决须待 ≥10 交易日 + 判据①-④实测)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
