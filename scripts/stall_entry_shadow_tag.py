#!/usr/bin/env python3
"""MFE_STALL 入场侧影子打标 (2026-08-12, record-only, 零执行路径改动).

背景 (8/11 盘后诊断): MFE_STALL_EXIT 近7日10笔0胜率-$532, 是最大失血点.
对照诊断 (DA指令 STALL n=6 vs OTHER n=18) 发现三个入场侧嫌疑特征:
  1. news_positive 触发: STALL 100% (6/6) vs OTHER 72%
  2. insider_net_buy 触发: STALL 50% vs OTHER 22%
  3. thesis 含"等回调/回踩"措辞: STALL 67% vs OTHER 44%
但 n 小 (Fisher 不显著) → 攒样验证, 暂不动门禁. 本脚本给**全部已平仓成交**
打上述特征标 (含 stall 与非 stall, 留分母), n≥30 再裁是否加门禁.

挂载: scheduler_core AFTER_HOURS (隔夜shadow结算之后), 异常隔离.
输出: data/stall_entry_shadow.jsonl, 按 decision_id 去重 (重启不清空).
回滚: playbook.stall_entry_shadow_enabled 置 false + 重启.
"""
import json
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("ftseek.stall_entry_shadow")

_BASE = Path(__file__).resolve().parents[1]
_OUT = _BASE / "data" / "stall_entry_shadow.jsonl"
_DB = _BASE / "data" / "ftseek_us.db"

_PULLBACK_RE = re.compile(r"等待回调|回调至|回踩|分批承接|勿追|不追|安全区")


def _enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.stall_entry_shadow_enabled", True))
    except Exception:
        return True


def _seen_ids() -> set:
    """已打标 decision_id 集合 (文件持久化, 重启不丢)."""
    seen = set()
    if _OUT.exists():
        for line in _OUT.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("decision_id"):
                    seen.add(d["decision_id"])
            except Exception:
                continue
    return seen


def _tag_row(r) -> dict:
    """单条成交 → 特征标签 + 结局."""
    thesis = r["thesis"] or ""
    try:
        conds = json.loads(r["conditions_json"] or "[]")
        if not isinstance(conds, list):
            conds = []
    except Exception:
        conds = []
    return {
        "ts": r["exit_time"],
        "trade_date": r["d"],
        "symbol": r["symbol"],
        "direction": r["direction"],
        "decision_id": r["decision_id"],
        "src": r["src"],
        # ── 入场侧特征 (诊断三嫌疑) ──
        "conditions": conds,
        "has_news_positive": "news_positive" in conds,
        "has_insider_buy": "insider_net_buy" in conds,
        "thesis_pullback_lang": bool(_PULLBACK_RE.search(thesis)),
        "confidence": r["confidence"],
        "atr_pct": r["atr_pct"],
        "risk_reward": r["risk_reward"],
        # ── 结局 ──
        "exit_reason": r["exit_reason"],
        "is_stall": "MFE_STALL" in (r["exit_reason"] or ""),
        "is_dead": "DEAD_SIGNAL" in (r["exit_reason"] or ""),
        "mfe_pct": r["mfe_pct"],
        "realized_pnl": r["realized_pnl"],
        "pnl_net": r["pnl_net"],
    }


def run_check(scheduler=None) -> None:
    """盘后打标: 全部已平仓且未打标的成交 → jsonl."""
    if not _enabled():
        return
    seen = _seen_ids()
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT t.decision_id, t.symbol, t.direction, t.exit_reason,
               t.exit_time, t.mfe_pct, t.realized_pnl, t.pnl_net,
               date(t.exit_time, '-4 hours') d,
               CASE WHEN t.decision_id LIKE 'DA-%' THEN 'DA'
                    WHEN t.decision_id LIKE 'TFB-%' THEN 'TFB'
                    WHEN t.decision_id LIKE 'intraday_reentry%' THEN 'REENTRY'
                    WHEN t.decision_id LIKE 'universe%' THEN 'UE'
                    ELSE 'other' END src,
               i.thesis, i.conditions_json, i.confidence, i.atr_pct, i.risk_reward
        FROM trade_records t
        LEFT JOIN playbook_instructions i ON i.decision_id = t.decision_id
        WHERE t.exit_time IS NOT NULL AND t.exit_time != ''
          AND t.decision_id IS NOT NULL AND t.decision_id != ''
        ORDER BY t.exit_time
    """).fetchall()
    conn.close()

    new_rows = [r for r in rows if r["decision_id"] not in seen]
    if not new_rows:
        return

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    n_stall = 0
    with _OUT.open("a", encoding="utf-8") as f:
        for r in new_rows:
            tag = _tag_row(r)
            f.write(json.dumps(tag, ensure_ascii=False) + "\n")
            if tag["is_stall"]:
                n_stall += 1
    logger.info(
        f"[stall影子] 打标 {len(new_rows)} 笔 (其中 MFE_STALL {n_stall} 笔) → {_OUT.name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_check()
    print(f"done → {_OUT}")
