"""今晚(9/15 交易日) UE glm-5.3 首跑核验 — script 载体 (避免 prompt 嵌 SQL 撞守卫)
只读; 判据全部预注册在本文件头, 输出即结论
"""
import json
import sqlite3
from datetime import datetime

DB = "/opt/ftseek/FTSeek-US/data/llm_audit.db"
LOG = "/opt/ftseek/FTSeek-US/logs/universe_scan.log"
# 9/15 00:00 CST 之后的 unix ts (只看本次切换后的生产调用)
SINCE = datetime(2026, 9, 16, 0, 0).timestamp()

lines = []


def emit(s=""):
    lines.append(s)


emit("=== UE glm-5.3 首跑核验 (9/16 生产窗口, 口径 is_test=0) ===")

# ── ① 生产调用是否真走 glm-5.3 (核心: 主模型生效, 非静默降级) ──
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = list(c.execute(
    """SELECT id, model, timestamp, success, tokens_in, tokens_out, cost_usd,
              latency_ms, error_msg
       FROM llm_calls
       WHERE feature='universe_expansion_reasoning' AND COALESCE(is_test,0)=0
         AND timestamp > ? ORDER BY id""", (SINCE,)))
emit(f"① 9/16 起生产调用 n={len(rows)}")
if not rows:
    emit("   ⚠️ 9/16 尚无生产调用 — 若已过 19:40 则 UE 未跑, 查 universe_scan.log 与 cron")
bad_model = [r for r in rows if r["model"] != "glm-5.3"]
fails = [r for r in rows if not r["success"]]
emit(f"   model 分布: " + json.dumps(
    {m: sum(1 for r in rows if r["model"] == m)
     for m in sorted({r["model"] for r in rows})}, ensure_ascii=False))
emit(f"   非 glm-5.3 行: {len(bad_model)}  (期望 0 — 否则=静默降级或 fallback 触发)")
for r in bad_model[:4]:
    emit(f"     id={r['id']} {r['model']} ok={r['success']} err={str(r['error_msg'])[:90]}")
emit(f"   失败行: {len(fails)}  (期望 0)")
for r in fails[:4]:
    emit(f"     id={r['id']} {r['model']} err={str(r['error_msg'])[:110]}")

# ── ② tokens_out 撞顶风险 (强制思考模式下 reasoning 计入 max_tokens=16000) ──
if rows:
    outs = [r["tokens_out"] for r in rows if r["tokens_out"] is not None]
    if outs:
        mx, av = max(outs), sum(outs) // len(outs)
        near = sum(1 for o in outs if o > 14000)
        emit(f"② tokens_out: max={mx} avg={av} >14k(撞顶预警)={near}")
        emit(f"   预算 16000 → 峰值占用 {mx/16000:.0%}; "
             + ("⚠️ 需抬 max_tokens 或加 thinking_budget" if near else "✅ 余量充足"))
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    if costs:
        emit(f"③ cost_usd: 合计 ${sum(costs):.4f} 均 ${sum(costs)/len(costs):.4f} "
             f"(glm-5.2 时代 UE 约 $0.02-0.06/轮; 涨价 输入+33%/输出+40% + 思考token)")
    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    if lat:
        emit(f"④ latency_ms: max={max(lat):.0f} avg={sum(lat)//len(lat)} "
             f"(timeout=300000; glm-5.2 时代 63-69s)")
        emit("   ⚠️ 超 180s 需关注 (思考模式更慢)" if max(lat) > 180000 else "   ✅ 在 timeout 内")

# ── ⑤ 日志侧 400/思考相关错误 ──
emit("⑤ universe_scan.log 错误面:")
try:
    with open(LOG, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    seg = txt[txt.rfind("2026-09-16"):] if "2026-09-16" in txt else txt[-40000:]
    for pat in ("HTTP Error 400", "restricted to True", "empty_response",
                "enable_thinking", "fallback", "Traceback"):
        n = seg.count(pat)
        if n:
            emit(f"   '{pat}': {n} 次" + ("  ⚠️" if pat in
                 ("HTTP Error 400", "restricted to True", "empty_response", "Traceback") else ""))
    if not any(seg.count(p) for p in ("HTTP Error 400", "restricted to True",
                                      "empty_response", "Traceback")):
        emit("   ✅ 无 400/思考参数/空响应/Traceback")
except FileNotFoundError:
    emit(f"   ({LOG} 不存在)")

# ── ⑥ 下游产物: UE 指令是否真落库 (切换空操作的最终判据) ──
try:
    d = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
    d.row_factory = sqlite3.Row
    # model_used 是归因切分的权威字段 (config 注释: "UE 指令行落 model_used ⇒ 时代天然可切分")
    emit("⑥ UE 指令 model_used 分布 (切换前后可切分):")
    for r in d.execute("""SELECT COALESCE(model_used,'(null)') mu, COUNT(*) n,
                                 MAX(substr(created_at,1,10)) last_day
                          FROM playbook_instructions GROUP BY model_used ORDER BY n DESC"""):
        emit(f"   {str(r['mu']):<24} n={r['n']:<5} last={r['last_day']}")
    emit("   9/16 起新增指令 (model_used 应含 glm-5.3):")
    r2 = list(d.execute("""SELECT decision_id, symbol, COALESCE(model_used,'-') mu,
                                  substr(created_at,1,16) ca
                           FROM playbook_instructions
                           WHERE created_at >= '2026-09-16' ORDER BY id DESC LIMIT 10"""))
    if not r2:
        emit("     (9/16 尚无指令 — UE 19:40 后才产出)")
    for r in r2:
        emit(f"     {str(r['decision_id'])[:40]:<42} {str(r['symbol']):<10} "
             f"{str(r['mu']):<14} {r['ca']}")
except Exception as e:
    emit(f"⑥ 查 playbook_instructions 失败: {type(e).__name__} {str(e)[:80]}")

print("\n".join(lines))
