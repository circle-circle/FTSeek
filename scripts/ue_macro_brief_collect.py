#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UE 宏观披露 brief 采集 (RFC-076, 2026-09-04)
================================================
背景: 8/29 macro_calendar_shadow_collect 因"零消费方"停采 (记录在
  config/unified_config.yaml macro_calendar 注释)。本脚本给它接上第一个
  真实消费方: UE clue brief 的宏观披露段。

设计 (极简复用, 不重写采集逻辑):
  - 直接 import 现有采集器内部函数 _build_snapshot / _load_prev_snapshot /
    _atomic_write_json (scripts/macro_calendar_shadow_collect.py)。
  - 采集 → 原子写 latest/history/state (与采集器产出格式完全一致, 状态连续)。
  - 派生: 本次 period_advanced 的指标 = "新披露" → 生成
    data/ue_macro_brief_{ET日期}.json, UE scan 21:10 CST 消费。
  - 无新披露 → 不写/不覆盖 brief 文件 (幂等, 防重跑清空)。
  - 失败降级: 任何异常打印 WARNING 后退出, 不影响主流程。

时点: cron 21:05 CST (EDT 期 = 09:05 ET, 非农等 08:30 ET 发布数据已入
  FRED, 实测 2026-09-04 09:19 ET PAYEMS 8月值可取)。
  ⚠️ EST 切换后 UE 21:10 CST = 08:10 ET 早于宏观发布, 届时需重评时点。

回滚: 删 crontab 行; 或 config universe_expansion.clue_macro_brief=false。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))


def main() -> int:
    from scripts.macro_calendar_shadow_collect import (
        _build_snapshot, _load_prev_snapshot, _atomic_write_json,
        _LATEST_OUT, _HISTORY_OUT, _STATE, _load_state, _save_state,
        _now_et,
    )

    now = _now_et()
    try:
        prev = _load_prev_snapshot()
        snapshot = _build_snapshot(now)
    except Exception as e:
        print(f"[ue_macro_brief] 采集失败 (不写状态, 下轮重试): {e}")
        return 1

    # 与采集器一致的落盘 (保持状态连续, 防下次运行重复判"新披露")
    _HISTORY_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    _atomic_write_json(_LATEST_OUT, snapshot)
    state = _load_state()
    state["last_run_date_et"] = snapshot.get("as_of_date", "")
    state["last_run_ts"] = snapshot.get("run_ts_et", "")
    state["last_status"] = "ok"
    _save_state(state)

    # 派生 brief: period_advanced = 自上次采集以来新披露
    lines = []
    releases = []
    for sid, ind in (snapshot.get("indicators") or {}).items():
        if ind.get("status") != "ok":
            continue
        if not ind.get("period_advanced_since_last_run"):
            continue
        name = ind.get("name", sid)
        val = ind.get("value")
        prev_val = ind.get("prev_value")
        period = (ind.get("period_end") or "")[:7]
        prev_ref = prev.get(sid, {}).get("period_end", "?")[:7]
        lines.append(
            f"[HIGH/MACRO_RELEASE] {name} ({period}): {val} | "
            f"前值({prev_ref}): {prev_val} | FRED 新披露, 注意宏观→板块传导")
        releases.append({"series": sid, "name": name, "period": period,
                         "value": val, "prev_value": prev_val})

    if not lines:
        print("[ue_macro_brief] 无新披露指标, 不生成 brief (UE 侧无注入)")
        return 0

    today_et = snapshot.get("as_of_date", now.strftime("%Y-%m-%d"))
    brief = {
        "date": today_et,
        "generated_at_et": snapshot.get("run_ts_et", ""),
        "source": "FRED via macro_calendar_shadow_collect._build_snapshot",
        "new_releases": releases,
        "brief_lines": lines,
    }
    out = _BASE / "data" / f"ue_macro_brief_{today_et}.json"
    _atomic_write_json(out, brief)
    print(f"[ue_macro_brief] 生成 {out.name}: {len(lines)} 条新披露")
    for ln in lines:
        print(f"  {ln}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
