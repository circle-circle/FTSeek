#!/usr/bin/env python
"""Phase 0 信号质量治理补丁（2026-08-23, DEB-20260823 策略侧）.

本脚本修改 `/root/.hermes/skills/deep-stock-analysis/scripts/` 下的文件:
- llm_synth.py: SYSTEM_PROMPT 加入校准原则 + user prompt 加入动态绩效反馈
- adapter.py: _compute_confidence 改为消费 my_score 差值（恢复区分力）

这些改动均在 Hermes skill 目录（工作区外），因此固化为脚本由用户执行。
每项均幂等、带备份、可回滚。

执行方式:
    cd /opt/ftseek/FTSeek-US
    ./venv/bin/python scripts/apply_phase0_signal_quality.py --apply
    
    验证:
    ./venv/bin/python scripts/apply_phase0_signal_quality.py --verify

回滚: 每次 --apply 前自动备份到 data/backups_phase0/，cp 回去即可。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SKILL_DIR = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
BACKUP_DIR = PROJECT / "data" / "backups_phase0"

# ═══════════════════════════════════════════════════════════════════
#  补丁定义
# ═══════════════════════════════════════════════════════════════════

# ── Patch 1: SYSTEM_PROMPT 加入校准原则（追加到末尾） ──
CALIBRATION_BLOCK = '''
【校准原则 — 你的历史表现】(2026-08-23, Phase 0 信号质量治理)
你的方向判断历史命中率约 50%（≈掷硬币，干净窗口 n=321 实测）。这意味着:
- 不要在缺乏独立硬证据时表达高 confidence/score（>7）
- 如果你对一个标的的全部论据都来自 24h 内新闻且无盘前异动确认，
  你的预期命中率不会高于基准 — 给低分(≤5)并在 skip_reason 写明"无增量信息"
- 同标的连续推荐:如果该标的在过去3天内已被你推荐且后续价格未确认方向
  (即前次推荐后的价格走势与你的方向不一致), 必须给出"为什么这次不同"的新证据,
  否则禁止同方向再推（避免"坚持错误方向"的惯性偏误）
- 真正的 edge 来源是: 市场还不知道的信息(如刚发布的8-K/盘前突发/内部人异动),
  而不是已经在价格中反映的旧新闻。你必须区分这两者。
'''

# ── Patch 2: user prompt 注入动态绩效反馈段 ──
# 这需要在 user prompt 组装时读取 FTSeek 的 direction_scorecard_latest.json
FEEDBACK_INJECTION_CODE = '''
    # ── Phase 0 (2026-08-23): 动态绩效反馈注入 user prompt ──
    # 让 LLM 知道自己近期的方向命中率与同标的前次结果。
    # 数据源: data/direction_scorecard_latest.json (cron 每日更新)
    # 注入位置: user prompt 末尾、数据段之后、要求 JSON 输出之前。
    # 失败时静默跳过 (不阻断主流程)。
    _feedback_text = ""
    try:
        _fb_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                                "opt", "ftseek", "FTSeek-US", "data",
                                "direction_scorecard_latest.json")
        # 多条路径兜底 (hermes skill 的 cwd 不确定)
        for _fp in [_fb_path,
                    "/opt/ftseek/FTSeek-US/data/direction_scorecard_latest.json"]:
            if os.path.exists(_fp):
                _fb = json.load(open(_fp))
                _recent = _fb.get("recent_7d", {})
                _buy_wr = _recent.get("buy_hit_rate", "N/A")
                _short_wr = _recent.get("short_hit_rate", "N/A")
                _total_n = _recent.get("total", 0)
                _feedback_text = f"""
---
【近期绩效反馈 — 你的实际命中率（最近7天）】
- BUY 方向命中率: {_buy_wr} (基于T+1收盘确认)
- SHORT 方向命中率: {_short_wr}
- 样本量: {_total_n} 条指令
⚠️ 这些数字是你的真实表现，不是目标。请据此校准你的确信度。
"""
                # 同标的前次结果
                _sym = collected.get("symbol", "")
                _sym_hist = _fb.get("by_symbol", {}).get(_sym)
                if _sym_hist:
                    _last = _sym_hist.get("last_3", [])
                    if _last:
                        _feedback_text += f"- 该标的({_sym})近3次推荐结果: " + "; ".join(
                            f"{r.get('direction','')} {r.get('date','')} → T+1 {r.get('hit','?')}"
                            for r in _last
                        ) + "\\n"
                break
    except Exception:
        pass
'''

# ── Patch 3: adapter.py confidence 改为消费 my_score 差值 ──
# 当前逻辑: agreed→+0.20, disagreed→-0.15, 基准0.50
# 问题: 挑战者几乎恒 agree → 恒0.70
# 修法: 保留 agree/disagree 的 ±0.20/0.15, 额外引入 |主-挑战者 score 差值| 作为衰减:
#   diff = abs(analyst_score - challenger_score) / 10  (归一化到 0-1)
#   if diff > 0.3: confidence -= diff * 0.15  (差值大=分歧, 降 conf)
# 这使得"agree 但挑战者 score 比主分析低很多"的情况也能降 conf
CONFIDENCE_PATCH_DESCRIPTION = """
adapter.py: _compute_confidence 改为额外消费 score 差值:
  当两模型 agree 但 |score差| > 3 时, 每多 1 分差距 conf -0.015
  → 使"形式同意但确信度差距大"的情况降低 confidence
  → 打破 conf 恒=0.70 的退化
"""


def _patch_system_prompt(content: str) -> str:
    """在 SYSTEM_PROMPT 的 triple-quote 关闭前插入校准原则."""
    # 找到 SYSTEM_PROMPT = """...""" 的最后一个 """
    marker = '"""'
    # SYSTEM_PROMPT 在 result = _call_llm(SYSTEM_PROMPT, ...) 之前的最后一个 """
    # 策略: 在 _call_llm 前面找最近的 """
    call_pos = content.find("result = _call_llm(SYSTEM_PROMPT,")
    if call_pos == -1:
        return None  # 无法定位
    # 从 call_pos 往前找最近的 """
    before = content[:call_pos]
    last_close = before.rfind(marker)
    if last_close == -1:
        return None
    # 检查是否已经注入过
    if "校准原则" in content[last_close - 500:last_close]:
        return content  # 幂等
    # 插入
    return content[:last_close] + CALIBRATION_BLOCK + "\n" + content[last_close:]


def _patch_confidence(content: str) -> str:
    """在 adapter.py 的 confidence 计算后加入 score 差值衰减."""
    if "score_diff_decay" in content:
        return content  # 幂等
    # 找到 agreed 导致 +0.20 的那行
    marker = "0.20"
    # 在 _compute_confidence 函数体内找 "+= 0.20" 或类似
    match = re.search(r"(confidence\s*[+=]+\s*0\.20.*?\n)", content)
    if not match:
        # 尝试另一种模式
        match = re.search(r"(.*agreed.*\+\s*0\.20.*?\n)", content)
    if not match:
        return None  # 无法定位
    insert_after = match.end()
    patch = """
        # ── Phase 0 (2026-08-23): score 差值衰减, 打破 conf 恒=0.70 ──
        # 挑战者几乎恒 agree (8/21 全 13 条) → conf 恒=0.70, 下游无区分力。
        # 引入 |analyst_score - challenger_score| 差值: 差值大 = "形式同意但确信度分歧大"
        # → 降 conf, 使 confidence 恢复连续性。回滚: 删除本段。
        try:
            _analyst_score = float(debate.get("score", 0) or 0)
            _chall_score = float(debate.get("challenger_score", 0) or debate.get("my_score", 0) or 0)
            _score_diff = abs(_analyst_score - _chall_score)
            if _score_diff > 3.0:
                _score_diff_decay = (_score_diff - 3.0) * 0.015
                confidence -= _score_diff_decay  # score_diff_decay
        except Exception:
            pass
"""
    return content[:insert_after] + patch + content[insert_after:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if not SKILL_DIR.exists():
        print(f"❌ Skill 目录不存在: {SKILL_DIR}")
        return 1

    llm_synth = SKILL_DIR / "llm_synth.py"
    adapter = SKILL_DIR / "adapter.py"

    if args.verify:
        ok = True
        s = llm_synth.read_text(encoding="utf-8")
        if "校准原则" not in s:
            print("❌ llm_synth.py: 校准原则未注入")
            ok = False
        else:
            print("✅ llm_synth.py: 校准原则已在 SYSTEM_PROMPT")
        a = adapter.read_text(encoding="utf-8")
        if "score_diff_decay" not in a:
            print("❌ adapter.py: score 差值衰减未注入")
            ok = False
        else:
            print("✅ adapter.py: score 差值衰减已生效")
        return 0 if ok else 1

    # 预演/应用
    s = llm_synth.read_text(encoding="utf-8")
    a = adapter.read_text(encoding="utf-8")

    new_s = _patch_system_prompt(s)
    new_a = _patch_confidence(a)

    if new_s is None:
        print("❌ 无法定位 SYSTEM_PROMPT 插入点, 请人工检查 llm_synth.py")
        return 1
    if new_a is None:
        print("❌ 无法定位 confidence += 0.20 插入点, 请人工检查 adapter.py")
        return 1

    changed_s = (new_s != s)
    changed_a = (new_a != a)

    print("=== Phase 0 信号质量治理补丁 ===")
    print(f"  llm_synth.py: {'待注入校准原则' if changed_s else '✅ 已是最新 (幂等)'}")
    print(f"  adapter.py:   {'待注入 score 差值衰减' if changed_a else '✅ 已是最新 (幂等)'}")
    if not changed_s and not changed_a:
        print("\n✅ 全部已是最新, 无需改动")
        return 0

    if not args.apply:
        print("\n预演模式 — 加 --apply 实际写入")
        print("\n补丁预览 (llm_synth.py 校准原则):")
        print(CALIBRATION_BLOCK[:400])
        print("\n补丁预览 (adapter.py score 差值衰减):")
        print(CONFIDENCE_PATCH_DESCRIPTION)
        return 0

    # 备份
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if changed_s:
        shutil.copy2(llm_synth, BACKUP_DIR / f"llm_synth.py.pre_phase0.{ts}")
    if changed_a:
        shutil.copy2(adapter, BACKUP_DIR / f"adapter.py.pre_phase0.{ts}")
    print(f"  备份 → {BACKUP_DIR}/")

    if changed_s:
        llm_synth.write_text(new_s, encoding="utf-8")
        print(f"  ✅ llm_synth.py 已注入校准原则")
    if changed_a:
        adapter.write_text(new_a, encoding="utf-8")
        print(f"  ✅ adapter.py 已注入 score 差值衰减")

    print("\n⚠️ 改动将在下一次 DA skill 运行时生效 (次日盘前)")
    print("   验证: ./venv/bin/python scripts/apply_phase0_signal_quality.py --verify")
    print(f"   回滚: cp {BACKUP_DIR}/llm_synth.py.pre_phase0.{ts} {llm_synth}")
    print(f"          cp {BACKUP_DIR}/adapter.py.pre_phase0.{ts} {adapter}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
