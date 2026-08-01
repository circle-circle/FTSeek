#!/usr/bin/env python3
"""rubric 影子日更 (2026-08-01, 议题三A+B, 用户裁"同意"四议题方案).

为什么需要: skill 目录 (/root/.hermes) 在自动化边界外不可改, DA 链自身永不落
  rubric → 每日批后在仓库侧补评分, 口径与 UE v2.2 / 2026-08-01 回填批一致
  (四布尔 evidence_checklist → rubric_score=命中数, enriched 提示词).

两处落点:
  ① playbook_instructions (DA/UE 当日指令, 文本源=opportunity_scan/UE shadow jsonl)
     — 议题三A 落库强制化: 指令级事件质量分不再缺席;
  ② signal_snapshots (DA 当日全部候选含 BLOCKED/SKIP, 文本源=obsidian symbols 归档
     当日小节) — 议题三B 候选级影子: 不需成交即攒"质量分→结局"样本 (快照 fwd_ret
     回填链已在跑), 攒样速度 ×5.

幂等: 已含 rubric_score 的行跳过. 独立开关: unified_config diagnostics.rubric_shadow_daily
  .enabled=false 或环境变量 RUBRIC_SHADOW_DAILY=off. 回滚=删 cron 行+本文件.
成本: ~50 候选+~10 指令/日 × $0.003 ≈ $0.2/日, 走 call_llm 审计.

用法: venv/bin/python scripts/rubric_shadow_daily.py [--date 2026-08-01] [--dry-run]
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))  # 复用回填器的文本加载器与提示词

SNAP_DB = ROOT / "data" / "signal_snapshots.db"
INST_DB = ROOT / "data" / "ftseek_us.db"
OBS_SYMBOLS = ROOT / "obsidian_vault" / "symbols"


def _obsidian_section(symbol: str, trade_date: str) -> str:
    """obsidian_vault/symbols/US_<SYM>.md 中 '## <date> |' 当节正文 (截 2500)."""
    p = OBS_SYMBOLS / f"{symbol.replace('.', '_')}.md"
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(rf"^## {re.escape(trade_date)} \|.*?(?=^## |\Z)",
                  text, flags=re.M | re.S)
    return m.group(0)[:2500] if m else ""


def _score(call_llm, date: str, symbol: str, text: str):
    from rubric_backfill import _SCHEMA, _SYSTEM_ENRICHED
    res = call_llm("universe_expansion_reasoning",
                   f"事件分析文本 ({date} {symbol}):\n{text[:2500]}",
                   system=_SYSTEM_ENRICHED, json_mode=True,
                   schema_required=_SCHEMA, max_tokens=300)
    if not res.success or not isinstance(res.response, dict):
        return None, float(getattr(res, "cost_usd", 0) or 0)
    cl = {k: bool(res.response.get(k)) for k in _SCHEMA}
    return {"rubric_score": sum(cl.values()), "evidence_checklist": cl}, \
        float(getattr(res, "cost_usd", 0) or 0)


def main() -> int:
    if os.environ.get("RUBRIC_SHADOW_DAILY", "").lower() == "off":
        print("RUBRIC_SHADOW_DAILY=off — 退出")
        return 0
    dry = "--dry-run" in sys.argv
    if "--date" in sys.argv:
        trade_date = sys.argv[sys.argv.index("--date") + 1]
    else:
        from app.utils.holiday_calendar import now_et
        trade_date = now_et().strftime("%Y-%m-%d")

    from rubric_backfill import _load_da_texts, _load_ue_texts
    da_texts, ue_texts = _load_da_texts(True), _load_ue_texts(True)

    from app.llm.model_call import call_llm
    done_i = done_s = failed = 0
    cost = 0.0

    # ① 指令级 (议题三A)
    conn = sqlite3.connect(str(INST_DB))
    rows = conn.execute("""
        SELECT id, symbol, raw_instruction_json FROM playbook_instructions
        WHERE date=? AND direction IN ('BUY','SELL_SHORT')
          AND raw_instruction_json NOT LIKE '%rubric_score%'
          AND (raw_instruction_json LIKE '%deep_analysis%'
               OR raw_instruction_json LIKE '%universe_expansion%')
    """, (trade_date,)).fetchall()
    for iid, symbol, raw_str in rows:
        is_da = "deep_analysis" in (raw_str or "")
        text = da_texts.get((trade_date, symbol)) if is_da else ue_texts.get((trade_date, symbol))
        if not text:
            continue
        if dry:
            done_i += 1
            continue
        sc, c = _score(call_llm, trade_date, symbol, text)
        cost += c
        if not sc:
            failed += 1
            continue
        raw = json.loads(raw_str)
        target = raw if is_da else raw.setdefault("origin", {})
        target.update(sc, rubric_shadow=1)
        conn.execute("UPDATE playbook_instructions SET raw_instruction_json=? WHERE id=?",
                     (json.dumps(raw, ensure_ascii=False), iid))
        conn.commit()
        done_i += 1
    conn.close()

    # ② 候选级 (议题三B) — signal_snapshots 当日 DA 候选 (含 BLOCKED/SKIP)
    sconn = sqlite3.connect(str(SNAP_DB))
    srows = sconn.execute("""
        SELECT decision_id, symbol, factors_json FROM signal_snapshots
        WHERE trade_date=? AND strategy_id='deep_analysis'
          AND (factors_json IS NULL OR factors_json NOT LIKE '%rubric_score%')
    """, (trade_date,)).fetchall()
    for did, symbol, fj in srows:
        text = _obsidian_section(symbol, trade_date)
        if len(text) < 30:
            continue
        if dry:
            done_s += 1
            continue
        sc, c = _score(call_llm, trade_date, symbol, text)
        cost += c
        if not sc:
            failed += 1
            continue
        try:
            factors = json.loads(fj) if fj else {}
            if not isinstance(factors, dict):
                factors = {"claims": factors}
        except Exception:
            factors = {}
        factors.update(sc, rubric_shadow=1)
        sconn.execute("UPDATE signal_snapshots SET factors_json=? WHERE decision_id=?",
                      (json.dumps(factors, ensure_ascii=False), did))
        sconn.commit()
        done_s += 1
    sconn.close()

    print(f"[{trade_date}] 指令级 {done_i}/{len(rows)} · 候选级 {done_s}/{len(srows)}"
          f" · 失败 {failed} · 成本 ${cost:.3f}{' (dry)' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
