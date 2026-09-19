#!/usr/bin/env python3
"""UE 思考预算后验读数 (只读) — 复核 `reasoning_effort: high` 是否真的压低触顶率。

改前基线 (2026-09-15..17, is_test=0, feature=universe_expansion_reasoning, model=glm-5.3):
  n=9, 触顶(>=16000)=6, max_out=24000, 错误串全含 finish_reason=length
判据: 窗口内触顶率 < 1/3 且样本 n>=3 ⇒ 改善; >= 1/2 ⇒ 未改善; n<3 ⇒ 样本不足(等下一轮)。
输出为纯文本, 由 Hermes cron (no_agent) 原样投递。
"""
import sqlite3
from pathlib import Path

DB = Path("/opt/ftseek/FTSeek-US/data/llm_audit.db")
FEAT = "universe_expansion_reasoning"


def main() -> int:
    if not DB.exists():
        print(f"[UE budget] 读不到审计库: {DB}")
        return 0
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT date(created_at) d, model, tokens_out, is_test, success, "
        "substr(COALESCE(error_msg,''),1,48) err, created_at "
        "FROM llm_calls WHERE feature=? AND COALESCE(is_test,0)=0 "
        "AND date(created_at) >= date('now','-2 day') ORDER BY id DESC",
        (FEAT,),
    ).fetchall()
    con.close()

    if not rows:
        print(f"[UE budget] 近 2 日无生产调用 (feature={FEAT}) — 扫描可能未跑或全为测试流量。")
        return 0

    last = rows[0]
    print(f"[UE 思考预算后验] 最近生产调用: {last['created_at']} model={last['model']}")
    by_day: dict = {}
    for r in rows:
        b = by_day.setdefault(r["d"], {"n": 0, "cap": 0, "fail": 0, "max": 0, "models": set()})
        b["n"] += 1
        b["cap"] += 1 if (r["tokens_out"] or 0) >= 16000 else 0
        b["fail"] += 1 if not r["success"] else 0
        b["max"] = max(b["max"], r["tokens_out"] or 0)
        b["models"].add(r["model"])
    gl = 0
    cap = 0
    for d in sorted(by_day, reverse=True):
        b = by_day[d]
        print(f"  {d}: n={b['n']} 触顶={b['cap']} 失败={b['fail']} max_out={b['max']} | {','.join(sorted(m for m in b['models'] if m))}")

    # 只看改动后的窗口: created_at >= 2026-09-18 16:00 CST (配置落库/重启之后)
    post = [r for r in rows if (r["created_at"] or "") >= "2026-09-18 16:00:00"]
    for r in post:
        if "glm" in (r["model"] or ""):
            gl += 1
            cap += 1 if (r["tokens_out"] or 0) >= 16000 else 0
    if gl == 0:
        print("  判定: **样本不足** — 配置生效后尚无 glm 生产调用, 等下一轮 (改前基线 9 笔 6 触顶)。")
    elif cap / gl < 1 / 3:
        print(f"  判定: **改善** — 生效后 glm n={gl}, 触顶={cap} ({cap/gl*100:.0f}%) < 1/3 (改前 67%)。")
    elif cap / gl >= 1 / 2:
        print(f"  判定: **未改善/证伪** — 生效后 glm n={gl}, 触顶={cap} ({cap/gl*100:.0f}%) >= 1/2; 回到 max_tokens/思考预算复核。")
    else:
        print(f"  判定: 临界 — 生效后 glm n={gl}, 触顶={cap} ({cap/gl*100:.0f}%), 继续攒样。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())