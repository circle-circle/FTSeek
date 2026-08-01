#!/usr/bin/env python3
"""rubric 一次性历史回填 (2026-08-01, 用户批 ~380 次 LLM 调用 token 预算).

背景: rubric_score (0-4, evidence_checklist 四布尔代码计数) 仅 UE v2.2 链落库
  (31 行), DA 链 188 行 + UE v2.2 前 26 行无 → 事件质量轴 n=13 未达 n≥30 纪律,
  BUY 事件质量门 (议题二B) 无法裁决. 本脚本用决策当时生成的已存文本回填:
  - DA 行: data/opportunity_scan_{date}.json 的 candidates[].thesis (桥直写前的完整分析)
  - UE 旧行: data/shadow/llm_universe_scan.jsonl 的 surfaced[].causal_chain+thesis
  文本均生成于决策当时、不含结局 → 无前视偏差. 评分提示词复用 v2.2 rule 7 原文定义
  (scripts/llm_universe_scan.py:93-95), 保持与在线口径一致.

幂等: 已含 rubric_score 的行跳过; 每行写完即 commit, 中断可重入.
成本: 逐行累加 CallResult.cost_usd, 结束打印; 全程走 call_llm 审计 (llm_audit.db).

用法: venv/bin/python scripts/rubric_backfill.py [--dry-run] [--limit N]
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"

# 与 scripts/llm_universe_scan.py SYSTEM_PROMPT_V2 rule 7 (93-95行) 原文一致
_SYSTEM = """你是事件质量评分员。给定一段交易决策当时生成的事件分析文本, 仅依据文本内容判断四个证据项:
1. catalyst_specific: 存在具体催化事件 (非泛情绪/板块轮动叙述)
2. catalyst_within_24h: 催化发生在文本生成前24小时内
3. price_volume_confirmed: 文本中有量价异动印证 (具体涨跌幅/成交量/异动描述)
4. crowding_low: 非已被充分炒作2日以上的拥挤交易
严格输出 JSON, 不知道就判 false。"""

# --enriched: 富文本+可观测性规则 (首轮 thesis-only 退化教训: within_24h/crowding
#   从纯 thesis 不可观测 → 恒 false → 97% ≤2 常量退化)
_SYSTEM_ENRICHED = """你是事件质量评分员。给定交易决策当时生成的完整候选分析 (含论点/条件/风险/入场计划),
仅依据文本判断四个证据项:
1. catalyst_specific: 存在具体催化事件 (非泛情绪/板块轮动叙述)
2. catalyst_within_24h: 催化发生在文本生成前24小时内。文本头部日期为决策日;
   "今日/昨晚/最新/盘前"类表述或提及决策日当天/前一天日期的事件视为24h内。
3. price_volume_confirmed: 文本中有量价异动印证 (具体涨跌幅/成交量/异动描述/
   corroborated_by_market=true 标记)
4. crowding_low: 非已被充分炒作2日以上的拥挤交易。文本提及"已连续多日上涨/拥挤/
   连板/炒作多日"或 emergence_streak_days≥2 → false; 明确为新浮现事件 → true。
严格输出 JSON, 不知道就判 false。"""

_SCHEMA = {"catalyst_specific": bool, "catalyst_within_24h": bool,
           "price_volume_confirmed": bool, "crowding_low": bool}


def _load_da_texts(enriched: bool = False) -> dict:
    """{(date, symbol): text} 来自 opportunity_scan 桥文件.

    enriched=True: 全候选 blob (thesis+条件+风险+入场计划+失效条件), 防 thesis-only 退化.
    """
    out = {}
    for p in sorted(ROOT.glob("data/opportunity_scan_*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        date = d.get("trade_date") or p.stem.replace("opportunity_scan_", "")
        for c in d.get("candidates") or []:
            sym = c.get("symbol")
            if not sym:
                continue
            if enriched:
                parts = [f"thesis: {c.get('thesis', '')}"]
                for k in ("conditions", "risks"):
                    v = c.get(k) or []
                    if v:
                        parts.append(f"{k}: {'; '.join(str(x)[:80] for x in v[:4])}")
                for k in ("entry_strategy", "invalidation"):
                    if c.get(k):
                        parts.append(f"{k}: {str(c[k])[:200]}")
                text = "\n".join(parts)
            else:
                text = (c.get("thesis") or "").strip()
            if text:
                out[(date, sym)] = text
    return out


def _load_ue_texts(enriched: bool = False) -> dict:
    """{(date, symbol): text} 来自 shadow jsonl surfaced 记录.

    enriched=True: 补 corroborated_by_market / winner_loser / emergence_streak_days
    (crowding 与量价印证的直接证据), 防 thesis-only 退化.
    """
    out = {}
    if not SHADOW.exists():
        return out
    for line in SHADOW.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") != "scan":
            continue
        date = r.get("date", "")
        for s in r.get("surfaced") or []:
            sym = s.get("symbol")
            if not sym:
                continue
            parts = [t for t in (s.get("causal_chain"), s.get("thesis")) if t]
            if enriched:
                if s.get("corroborated_by_market"):
                    parts.append("corroborated_by_market=true (扫描时已有市场异动印证)")
                if s.get("winner_loser"):
                    parts.append(f"winner_loser={s['winner_loser']}")
                if s.get("emergence_streak_days") is not None:
                    parts.append(f"emergence_streak_days={s['emergence_streak_days']}")
            text = " | ".join(str(p) for p in parts if p)
            if text:
                out[(date, sym)] = text
    return out


def _reset_backfilled(conn) -> int:
    """清除 rubric_backfill=1 行的回填字段 (供换富文本重打). 在线 v2.2 行不动."""
    n = 0
    rows = conn.execute(
        "SELECT id, raw_instruction_json FROM playbook_instructions "
        "WHERE raw_instruction_json LIKE '%rubric_backfill%'").fetchall()
    for iid, raw_str in rows:
        raw = json.loads(raw_str)
        raw.pop("rubric_score", None)
        raw.pop("evidence_checklist", None)
        raw.pop("rubric_backfill", None)
        origin = raw.get("origin")
        if isinstance(origin, dict):
            for k in ("rubric_score", "evidence_checklist", "rubric_backfill"):
                origin.pop(k, None)
        conn.execute("UPDATE playbook_instructions SET raw_instruction_json=? WHERE id=?",
                     (json.dumps(raw, ensure_ascii=False), iid))
        n += 1
    conn.commit()
    return n


def main() -> int:
    dry = "--dry-run" in sys.argv
    enriched = "--enriched" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10 ** 9

    conn = sqlite3.connect(str(DB))
    if "--reset" in sys.argv:
        n = _reset_backfilled(conn)
        print(f"已清除 {n} 行回填字段 (在线 v2.2 行未动)")
        conn.close()
        return 0

    da_texts = _load_da_texts(enriched)
    ue_texts = _load_ue_texts(enriched)
    print(f"文本源: DA {len(da_texts)} 条 / UE {len(ue_texts)} 条 (enriched={enriched})")

    rows = conn.execute("""
        SELECT id, date, symbol, raw_instruction_json
        FROM playbook_instructions
        WHERE direction IN ('BUY','SELL_SHORT')
          AND raw_instruction_json NOT LIKE '%rubric_score%'
          AND (raw_instruction_json LIKE '%deep_analysis%'
               OR raw_instruction_json LIKE '%universe_expansion%')
        ORDER BY date
    """).fetchall()
    print(f"待回填指令: {len(rows)} 行")

    from app.llm.model_call import call_llm

    done = skipped_no_text = failed = 0
    cost_total = 0.0
    for iid, date, symbol, raw_str in rows:
        if done >= limit:
            break
        raw = json.loads(raw_str) if raw_str else {}
        is_da = "deep_analysis" in (raw_str or "")
        text = da_texts.get((date, symbol)) if is_da else ue_texts.get((date, symbol))
        if not text:
            skipped_no_text += 1
            continue
        if dry:
            done += 1
            continue

        res = call_llm("universe_expansion_reasoning",
                       f"事件分析文本 ({date} {symbol}):\n{text[:2500 if enriched else 1500]}",
                       system=_SYSTEM_ENRICHED if enriched else _SYSTEM,
                       json_mode=True,
                       schema_required=_SCHEMA, max_tokens=300)
        cost_total += float(getattr(res, "cost_usd", 0) or 0)
        if not res.success or not isinstance(res.response, dict):
            failed += 1
            print(f"  ✗ {date} {symbol}: {getattr(res, 'error', '?')}")
            continue
        cl = {k: bool(res.response.get(k)) for k in _SCHEMA}
        score = sum(cl.values())

        if is_da:
            raw["rubric_score"] = score
            raw["evidence_checklist"] = cl
            raw["rubric_backfill"] = 1
        else:
            origin = raw.setdefault("origin", {})
            origin["rubric_score"] = score
            origin["evidence_checklist"] = cl
            origin["rubric_backfill"] = 1
        conn.execute("UPDATE playbook_instructions SET raw_instruction_json=? WHERE id=?",
                     (json.dumps(raw, ensure_ascii=False), iid))
        conn.commit()
        done += 1
        if done % 20 == 0:
            print(f"  … {done} 行已回填, 累计成本 ${cost_total:.3f}")
        time.sleep(0.3)  # 轻限流, 防触发 DashScope QPS 上限

    print(f"\n完成: 回填 {done} / 无文本跳过 {skipped_no_text} / LLM 失败 {failed}")
    print(f"总成本 ${cost_total:.3f} (审计: llm_audit.db feature=universe_expansion_reasoning)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
