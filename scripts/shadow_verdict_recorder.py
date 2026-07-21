#!/usr/bin/env python3
"""
shadow_verdict_recorder.py — P1 影子判决埋点 (零交易逻辑, 纯日志)

目的 (2026-07-12):
  把 DA 挑战者/校验环节已产出但从未持久化的"结构化溯源判决"逐日落库,
  为 3-4 周后回测 "挑战者的质疑/溯源缺口 是否与实测盈亏相关" 攒可检验样本。
  —— 即: 先造能测的数据, 再让数据决定要不要接 SKIP-gate (而非凭 n=8/单日 anecdote 就接线)。

数据源 (已核实持久化, 无需碰 pipeline):
  data/daily_battle_plan_us.json  —— DA 每日重写的作战计划, targets[] 每只已含:
    verdict           挑战者总判决 (VALID / ...)
    data_cited[]       论据列表; 带 [Futu]/[SEC] 等尾标=有溯源, 无标=溯源缺口(幻觉代理)
    factor_validation  逐因子审查 {factor: CONFIRMED/WEAK/FALSE_POSITIVE}; FALSE_POSITIVE=挑战者抓到的伪信号
    catalyst           催化 {type, score, strength, source_count}
    confidence/direction/model_used/discovery_source/risk_reward

方法 (与 llm_edge_scorecard 对齐): decision_id 不传播到 trade_records (0 条含 DEC-),
  故仍按 (symbol, plan_date) 模糊 join —— 这正是 n=211 scorecard 已验证可行的口径。
  clean trace_id 链路是独立的高风险改动 (需穿 signal_pipeline), 本埋点**不做**、不阻塞。

落地: data/shadow_verdicts.db 表 shadow_verdicts, 按 (plan_date, symbol) UPSERT 幂等。
用法:
  python scripts/shadow_verdict_recorder.py                 # 抽当前 battle plan 落库
  python scripts/shadow_verdict_recorder.py --plan <path>   # 指定文件 (可 backfill .bak)
  python scripts/shadow_verdict_recorder.py --report        # 看已攒样本 + 尝试 join 盈亏 (早期必噪声)
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = ROOT / "data" / "daily_battle_plan_us.json"
DB = ROOT / "data" / "shadow_verdicts.db"
TRADES_DB = ROOT / "data" / "ftseek_us.db"

_SOURCE_TAG = re.compile(r"\[[^\]]+\]")  # 论据尾标如 [Futu] / [SEC] → 视为有溯源


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_verdicts (
            plan_date            TEXT NOT NULL,
            symbol               TEXT NOT NULL,
            direction            TEXT,
            verdict              TEXT,
            confidence           REAL,
            model_used           TEXT,
            discovery_source     TEXT,
            risk_reward          REAL,
            regime               TEXT,
            vix                  REAL,
            evidence_count       INTEGER,
            evidence_sourced     INTEGER,
            evidence_unsourced   INTEGER,   -- 溯源缺口 = 幻觉风险代理
            factor_confirmed     INTEGER,
            factor_weak          INTEGER,
            factor_false_positive INTEGER,  -- 挑战者抓到的伪信号
            factor_total         INTEGER,
            catalyst_type        TEXT,
            catalyst_score       REAL,
            catalyst_strength    TEXT,
            catalyst_source_count INTEGER,
            raw_json             TEXT,       -- 完整 target, 供未来重抽新字段
            captured_at          TEXT NOT NULL,
            PRIMARY KEY (plan_date, symbol)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sv_symbol ON shadow_verdicts(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sv_date ON shadow_verdicts(plan_date)")
    conn.commit()


def _extract(target: dict, plan: dict) -> dict:
    """从单个 target 抽结构化判决 (对缺字段全部安全兜底)。"""
    data_cited = target.get("data_cited") or []
    if not isinstance(data_cited, list):
        data_cited = []
    sourced = sum(1 for e in data_cited if isinstance(e, str) and _SOURCE_TAG.search(e))

    fv = target.get("factor_validation") or {}
    if not isinstance(fv, dict):
        fv = {}
    vals = [str(v).upper() for v in fv.values()]
    confirmed = sum(1 for v in vals if v == "CONFIRMED")
    weak = sum(1 for v in vals if v == "WEAK")
    false_pos = sum(1 for v in vals if v == "FALSE_POSITIVE")

    cat = target.get("catalyst") or {}
    if not isinstance(cat, dict):
        cat = {}

    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "plan_date": plan.get("date"),
        "symbol": target.get("symbol"),
        "direction": target.get("direction"),
        "verdict": target.get("verdict"),
        "confidence": _num(target.get("confidence")),
        "model_used": target.get("model_used"),
        "discovery_source": target.get("discovery_source"),
        "risk_reward": _num(target.get("risk_reward")),
        "regime": plan.get("market_regime"),
        "vix": _num(plan.get("vix")),
        "evidence_count": len(data_cited),
        "evidence_sourced": sourced,
        "evidence_unsourced": len(data_cited) - sourced,
        "factor_confirmed": confirmed,
        "factor_weak": weak,
        "factor_false_positive": false_pos,
        "factor_total": len(vals),
        "catalyst_type": cat.get("type"),
        "catalyst_score": _num(cat.get("score")),
        "catalyst_strength": cat.get("strength"),
        "catalyst_source_count": cat.get("source_count"),
        "raw_json": json.dumps(target, ensure_ascii=False),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


def record_from_plan(plan_path: Path, db_path: Path = DB) -> int:
    """读 battle plan → UPSERT 影子判决。返回落库条数。幂等。"""
    plan = json.loads(Path(plan_path).read_text())
    targets = plan.get("targets") or []
    if isinstance(targets, dict):
        targets = list(targets.values())
    if not plan.get("date"):
        print("⚠️ plan 缺 date 字段, 跳过 (无 join key)", file=sys.stderr)
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        _init_db(conn)
        cols = [
            "plan_date", "symbol", "direction", "verdict", "confidence", "model_used",
            "discovery_source", "risk_reward", "regime", "vix", "evidence_count",
            "evidence_sourced", "evidence_unsourced", "factor_confirmed", "factor_weak",
            "factor_false_positive", "factor_total", "catalyst_type", "catalyst_score",
            "catalyst_strength", "catalyst_source_count", "raw_json", "captured_at",
        ]
        placeholders = ",".join("?" for _ in cols)
        sql = (f"INSERT OR REPLACE INTO shadow_verdicts ({','.join(cols)}) "
               f"VALUES ({placeholders})")
        n = 0
        for t in targets:
            if not isinstance(t, dict) or not t.get("symbol"):
                continue
            try:
                row = _extract(t, plan)
                conn.execute(sql, [row[c] for c in cols])
                n += 1
            except Exception as e:  # 单只失败不影响其余 (纯埋点, 绝不因此中断)
                print(f"⚠️ 抽取 {t.get('symbol')} 失败, 跳过: {e}", file=sys.stderr)
        conn.commit()
        return n
    finally:
        conn.close()


def report(db_path: Path = DB) -> None:
    """看当前已攒样本, 并尝试 (symbol, plan_date) join trade_records 盈亏 (早期必噪声)。"""
    if not Path(db_path).exists():
        print("尚无 shadow_verdicts.db, 先跑一次落库。")
        return
    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM shadow_verdicts").fetchall()
    conn.close()
    print(f"═══ 已攒影子判决: {len(rows)} 行, 覆盖日期 "
          f"{min((r['plan_date'] for r in rows), default='-')} → "
          f"{max((r['plan_date'] for r in rows), default='-')} ═══")
    if not rows:
        return
    fp = sum(1 for r in rows if (r["factor_false_positive"] or 0) > 0)
    unsourced = sum(1 for r in rows if (r["evidence_unsourced"] or 0) > 0)
    print(f"  含 FALSE_POSITIVE 因子: {fp}/{len(rows)}  |  含未溯源论据: {unsourced}/{len(rows)}")

    if not TRADES_DB.exists():
        return
    t = sqlite3.connect(str(TRADES_DB)); t.row_factory = sqlite3.Row
    tr = {}
    for r in t.execute("SELECT symbol, trade_date, realized_pnl FROM trade_records "
                       "WHERE exit_price>0 AND realized_pnl IS NOT NULL"):
        k = (r["symbol"].replace("US.", ""), r["trade_date"])
        tr.setdefault(k, []).append(float(r["realized_pnl"]))
    t.close()

    flagged, clean = [], []
    for r in rows:
        k = (str(r["symbol"]).replace("US.", ""), r["plan_date"])
        if k not in tr:
            continue
        pnl = mean(tr[k])
        is_flagged = (r["factor_false_positive"] or 0) > 0 or (r["evidence_unsourced"] or 0) > 0
        (flagged if is_flagged else clean).append(pnl)

    joined = len(flagged) + len(clean)
    print(f"  join 到实测盈亏: {joined} 行 "
          f"{'(样本太少, 仅占位, 勿下结论)' if joined < 30 else ''}")
    if flagged:
        print(f"    被标记(伪信号/未溯源) n={len(flagged)} 期望=${mean(flagged):.2f}")
    if clean:
        print(f"    未被标记            n={len(clean)} 期望=${mean(clean):.2f}")
    if joined < 30:
        print("  → 需 ~3-4 周 go-forward 攒够 (≥30 闭环) 才能判 'SKIP-gate 是否有效'。")


def main() -> int:
    ap = argparse.ArgumentParser(description="P1 影子判决埋点 (只读交易路径)")
    ap.add_argument("--plan", default=str(DEFAULT_PLAN), help="battle plan json 路径")
    ap.add_argument("--report", action="store_true", help="只看已攒样本 + join 盈亏, 不落库")
    args = ap.parse_args()

    if args.report:
        report()
        return 0

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"找不到 plan: {plan_path}", file=sys.stderr)
        return 1
    n = record_from_plan(plan_path)
    print(f"✅ 落库 {n} 条影子判决 → {DB}")
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
