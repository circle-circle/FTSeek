"""方向轴统一读数模块 (2026-08-01, 用户裁"同意"四议题方案·议题四尺子固化).

单一事实源: direction_hit_scorecard.py (周六 cron) 与 weekly_review.py (周五推送)
都从这里取 方向×基准 / 方向×regime / 周度方向稳定性 / 事件质量轴 读数,
防两处各写 SQL 口径漂移 (口径七律: 同读数同源).

口径戒律 (2026-08-01 新增): 任何 hit-rate 必须标注价格基准 (entry/open).
  entry 基准 = 指令价(成交价或限价或 open 代理), 未执行指令的 entry 基准含
  "纸面 edge"幻觉 (限价不可成交); open 基准 = 当日开盘价, 最接近"市价单可成交"
  反事实. 两基准成对输出, 禁止混基比较.
"""
import sqlite3
from typing import Any, Dict, Optional

_DIR_IN = "('BUY','SELL_SHORT')"


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _agg(r) -> Dict[str, Any]:
    return {
        "n": r["n"],
        "hit_t1": r["hit_t1"], "mean_t1": r["mean_t1"],
        "hit_t3": r["hit_t3"], "mean_t3": r["mean_t3"],
    }


def basis_quadrant(conn: sqlite3.Connection, since: str,
                   executed_only: bool = False) -> Dict[str, Any]:
    """方向 × 价格基准 四象限 (T+1/T+3).

    executed_only=True 时仅 executed=1 (成交口径 — 2026-08-01 复核修正:
    原用 triggered=1 (盘中条件穿越) 漏 17 笔已成交 DA 单, 且与 scorecard
    已执行孪生行 executed=1 口径不一致; try_claim_execution 原子抢占语义).
    open 基准列为 NULL 的旧样本自动不计 (AVG 忽略 NULL, n_open 单列).
    """
    trig = "AND COALESCE(executed,0)=1" if executed_only else ""
    out: Dict[str, Any] = {}
    for r in _rows(conn, f"""
        SELECT direction,
               COUNT(*) n,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t1>0 THEN 1 ELSE 0 END),1) hit_t1,
               ROUND(100.0*AVG(fwd_return_t1),3) mean_t1,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t3>0 THEN 1 ELSE 0 END),1) hit_t3,
               ROUND(100.0*AVG(fwd_return_t3),3) mean_t3
        FROM playbook_instructions
        WHERE date >= ? AND direction IN {_DIR_IN} AND fwd_return_t1 IS NOT NULL {trig}
        GROUP BY direction
    """, (since,)):
        out.setdefault(r["direction"], {})["entry"] = _agg(r)
    for r in _rows(conn, f"""
        SELECT direction,
               COUNT(fwd_return_t1_open) n,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t1_open>0 THEN 1 ELSE 0 END),1) hit_t1,
               ROUND(100.0*AVG(fwd_return_t1_open),3) mean_t1,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t3_open>0 THEN 1 ELSE 0 END),1) hit_t3,
               ROUND(100.0*AVG(fwd_return_t3_open),3) mean_t3
        FROM playbook_instructions
        WHERE date >= ? AND direction IN {_DIR_IN} AND fwd_return_t1_open IS NOT NULL {trig}
        GROUP BY direction
    """, (since,)):
        out.setdefault(r["direction"], {})["open"] = _agg(r)
    return out


def direction_regime_cross(conn: sqlite3.Connection, since: str,
                           executed_only: bool = True) -> Dict[str, Dict[str, Any]]:
    """方向 × regime 交叉表 (默认已执行口径 — 07-31 起 regime 列由盘前补写).

    返回 {direction: {regime: agg}}; regime 缺失归入 "未记录".
    """
    trig = "AND COALESCE(executed,0)=1" if executed_only else ""
    out: Dict[str, Dict[str, Any]] = {}
    for r in _rows(conn, f"""
        SELECT direction,
               COALESCE(NULLIF(market_regime,''),'未记录') regime,
               COUNT(*) n,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t1>0 THEN 1 ELSE 0 END),1) hit_t1,
               ROUND(100.0*AVG(fwd_return_t1),3) mean_t1,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t3>0 THEN 1 ELSE 0 END),1) hit_t3,
               ROUND(100.0*AVG(fwd_return_t3),3) mean_t3
        FROM playbook_instructions
        WHERE date >= ? AND direction IN {_DIR_IN} AND fwd_return_t1 IS NOT NULL {trig}
        GROUP BY direction, regime
    """, (since,)):
        out.setdefault(r["direction"], {})[r["regime"]] = _agg(r)
    return out


def weekly_direction_stability(conn: sqlite3.Connection, weeks: int = 5,
                               executed_only: bool = True) -> Dict[str, Any]:
    """周度方向稳定性: 最近 N 个 ISO 周每方向 hit_t1 序列 + 连续强于/弱于 50% 周数.

    2026-08-01 判例: BUY 5/5 周 <50% + SHORT 5/5 周 >50% 是多空分门裁决的
    真正依据 (比汇总 p 值更抗单周 regime 混杂). 连续同向周数 >=4 视为"稳定".
    """
    trig = "AND COALESCE(executed,0)=1" if executed_only else ""
    out: Dict[str, Any] = {}
    for r in _rows(conn, f"""
        SELECT direction, strftime('%W', date) wk, MIN(date) wk_start,
               COUNT(*) n,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t1>0 THEN 1 ELSE 0 END),1) hit_t1
        FROM playbook_instructions
        WHERE direction IN {_DIR_IN} AND fwd_return_t1 IS NOT NULL {trig}
        GROUP BY direction, wk ORDER BY wk_start DESC
    """):
        d = out.setdefault(r["direction"], {"weeks": [], "streak": 0, "verdict": "样本不足"})
        if len(d["weeks"]) < weeks:
            d["weeks"].append({"wk": r["wk"], "start": r["wk_start"],
                               "n": r["n"], "hit_t1": r["hit_t1"]})
    for d in out.values():
        hits = [w["hit_t1"] for w in d["weeks"] if w["n"] >= 5]
        if not hits:
            continue
        above = hits[0] > 50
        streak = 0
        for h in hits:
            if (h > 50) == above:
                streak += 1
            else:
                break
        d["streak"] = streak if above else -streak
        if abs(streak) >= 4:
            d["verdict"] = "稳定强于掷币" if above else "稳定弱于掷币"
        elif len(hits) >= 4:
            d["verdict"] = "方向不稳"
    return out


def quality_axis_status(conn: sqlite3.Connection, since: str,
                        min_n: int = 30) -> Dict[str, Any]:
    """事件质量轴计数器 + n>=min_n 自动标线 (UE rubric / critique, 含回填).

    rubric_score 落在 raw_instruction_json (UE v2.2 起 + 2026-08-01 回填批,
    回填行带 rubric_backfill:1 标记). n 达标才允许用于设门 (口径七律 n≥30).
    """
    out: Dict[str, Any] = {"min_n": min_n, "rubric": {}, "critique": {}}
    sql = f"""
        SELECT {{bucket}} bucket, COALESCE(executed,0) trig, COUNT(*) n,
               ROUND(100.0*AVG(CASE WHEN fwd_return_t1>0 THEN 1 ELSE 0 END),1) hit_t1,
               ROUND(100.0*AVG(fwd_return_t1),3) mean_t1
        FROM playbook_instructions
        WHERE date >= ? AND direction IN {_DIR_IN} AND fwd_return_t1 IS NOT NULL
        GROUP BY 1,2
    """
    rubric_bucket = """
        CASE WHEN raw_instruction_json LIKE '%"rubric_score": 4%'
               OR raw_instruction_json LIKE '%"rubric_score": 3%' THEN 'rubric≥3'
             WHEN raw_instruction_json LIKE '%rubric_score%' THEN 'rubric≤2'
             ELSE 'rubric未落' END
    """
    for r in _rows(conn, sql.format(bucket=rubric_bucket), (since,)):
        b = out["rubric"].setdefault(r["bucket"], {})
        b["exec" if r["trig"] else "unexec"] = {"n": r["n"], "hit_t1": r["hit_t1"],
                                                "mean_t1": r["mean_t1"]}
    critique_bucket = """
        CASE WHEN raw_instruction_json LIKE '%"verdict": "DROP"%' THEN 'DROP'
             WHEN raw_instruction_json LIKE '%"verdict": "DOWNGRADE"%' THEN 'DOWNGRADE'
             WHEN raw_instruction_json LIKE '%"verdict": "KEEP"%' THEN 'KEEP'
             ELSE 'critique未落' END
    """
    for r in _rows(conn, sql.format(bucket=critique_bucket), (since,)):
        b = out["critique"].setdefault(r["bucket"], {})
        b["exec" if r["trig"] else "unexec"] = {"n": r["n"], "hit_t1": r["hit_t1"],
                                                "mean_t1": r["mean_t1"]}
    for axis in ("rubric", "critique"):
        for bucket, d in out[axis].items():
            ex = d.get("exec", {})
            d["gate_ready"] = ("未落" not in bucket) and bool(ex.get("n", 0) >= min_n)
    return out
