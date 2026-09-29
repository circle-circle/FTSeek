"""UE 模型 bakeoff: glm-5.3 (现役 primary) vs deepseek-v4.1-flash (现役 fallback)
用户裁 2026-09-29: 只对比这两个模型, 同材料 3 轮 (RFC-105 prime bakeoff 同款纪律)。
纯 shadow: FTSEEK_SHADOW_RUN=1, 不落主 scan、不 inject、不下单。
生产同构: 两臂用 call_llm 同款参数 (route extra_body 合并 / temp 0.2 / max_tokens 24000 /
json_mode / schema_required={"surfaced": list}) — 唯一变量 = 模型 alias。
输出: data/shadow/ue_model_bakeoff_20260929.jsonl (逐轮一行, 含 text 供质量评分)
"""
import os, sys, json, time, hashlib
os.environ["FTSEEK_SHADOW_RUN"] = "1"
sys.path.insert(0, "/opt/ftseek/FTSeek-US")

from scripts import llm_universe_scan as ue   # 与 tests/ 同一导入方式

from app.llm.model_call import _try_single_model, _get_llm_config
from app.llm.features.json_schema_guard import parse_and_validate

OUT = "/opt/ftseek/FTSeek-US/data/shadow/ue_model_bakeoff_20260929.jsonl"
ARMS = ["glm-5.3", "deepseek-v41-flash"]   # yaml alias
ROUNDS = 3

cfg = _get_llm_config()
route = (cfg.get("features") or {}).get("universe_expansion_reasoning") or {}
route_eb = route.get("extra_body") if isinstance(route.get("extra_body"), dict) else None
print(f"route extra_body = {route_eb}", flush=True)

# ── 同材料: 构建一次 brief, 两臂三轮共用 ──
print("构建 clue brief (同材料) ...", flush=True)
t0 = time.time()
brief = ue._build_clue_brief("")
brief_sha1 = hashlib.sha1(brief.encode("utf-8")).hexdigest()[:12]
print(f"brief {len(brief)} chars sha1={brief_sha1} ({time.time()-t0:.0f}s)", flush=True)
# 留档 brief 供复现
bf = "/root/.hermes/cache/scratch/ue_brief_20260929.txt"
open(bf, "w").write(brief)

system = ue.SYSTEM_PROMPT_V2
prompt_hash = ue.PROMPT_HASH_V2 if hasattr(ue, "PROMPT_HASH_V2") else ""

def run_arm(alias: str, rnd: int):
    t0 = time.time()
    res = _try_single_model(
        alias, cfg, system, brief,
        temperature=0.2, max_tokens=24000, json_mode=True,
        extra_body=dict(route_eb) if route_eb else None,
        prompt_cache=bool(route.get("prompt_cache", False)),
    )
    dt = time.time() - t0
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "arm": "A" if alias == "glm-5.3" else "B",
        "model": alias, "round": rnd,
        "brief_file": bf, "brief_sha1": brief_sha1,
        "brief_template_version": ue.BRIEF_TEMPLATE_VERSION,
        "prompt_version": ue.PROMPT_VERSION, "prompt_hash": prompt_hash,
        "temperature": 0.2, "max_tokens": 24000,
        "enable_thinking": (route_eb or {}).get("enable_thinking"),
        "route_extra_body": dict(route_eb) if route_eb else None,
        "latency_s": round(dt, 1),
        "success": bool(res and res.success),
        "model_reported": getattr(res, "model_used", ""),
        "finish": getattr(res, "finish_reason", ""),
        "tokens_in": getattr(res, "tokens_in", 0),
        "tokens_out": getattr(res, "tokens_out", 0),
        "cost_usd": round(float(getattr(res, "cost_usd", 0) or 0), 5),
        "error": getattr(res, "error", "") or "",
    }
    text = getattr(res, "response", "") or ""
    schema_ok = False
    parsed = None
    if res and res.success:
        try:
            parsed = parse_and_validate(text, {"surfaced": list}, None, None,
                                        feature="universe_expansion_reasoning")
            schema_ok = parsed is not None
        except Exception as e:
            row["error"] = f"schema_exc:{type(e).__name__}:{e}"
    row["schema_ok"] = schema_ok
    if schema_ok and isinstance(parsed, dict):
        raw_items = parsed.get("surfaced")
        try:
            norm = ue._normalize_surfaced(raw_items, with_tier=True)
            stats = ue._v29_contract_stats(raw_items)
        except Exception as e:
            norm, stats = [], {"error": str(e)}
        row["surfaced_n"] = len(norm)
        row["contract_stats"] = stats
        row["surfaced_brief"] = [
            {"symbol": s.get("symbol"), "direction": s.get("direction"),
             "confidence": s.get("confidence"), "self_tier": s.get("self_tier", ""),
             "trig_filled": bool(s.get("invalidation_triggers")),
             "evidence_ref": str(s.get("evidence_ref", ""))[:120]}
            for s in norm]
    row["text_head"] = str(text)[:400]
    with open(OUT, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  [{row['arm']}/{alias} r{rnd}] ok={row['success']} schema={schema_ok} "
          f"{row['latency_s']}s out={row['tokens_out']} finish={row['finish']} "
          f"n={row.get('surfaced_n','-')} err={row['error'][:60]}", flush=True)
    return row

# ── 交替执行 (A,B,A,B,A,B) 消除时段偏差 ──
print(f"\n═══ bakeoff 开始: {ARMS} × {ROUNDS} 轮交替 ═══", flush=True)
for rnd in range(1, ROUNDS + 1):
    print(f"── round {rnd} ──", flush=True)
    for alias in ARMS:
        run_arm(alias, rnd)

# ── 汇总 ──
print("\n═══ 汇总 ═══", flush=True)
rows = [json.loads(l) for l in open(OUT)]
from collections import defaultdict
agg = defaultdict(lambda: {"n": 0, "schema_ok": 0, "lat": [], "out": [], "cost": 0.0,
                           "surfaced": 0, "trig_filled": 0, "trig_total": 0})
for r in rows:
    a = agg[r["model"]]
    a["n"] += 1
    a["schema_ok"] += 1 if r.get("schema_ok") else 0
    a["lat"].append(r["latency_s"])
    a["out"].append(r["tokens_out"])
    a["cost"] += r.get("cost_usd", 0)
    for s in r.get("surfaced_brief", []):
        a["surfaced"] += 1
        a["trig_total"] += 1
        a["trig_filled"] += 1 if s.get("trig_filled") else 0
for m, a in agg.items():
    import statistics
    print(f"{m}: 轮={a['n']} schema过={a['schema_ok']}/{a['n']} "
          f"延迟p50={statistics.median(a['lat']):.0f}s max={max(a['lat']):.0f}s "
          f"tokens_out中位={statistics.median(a['out']):.0f} "
          f"成本合计=${a['cost']:.4f} surfaced={a['surfaced']} "
          f"trig_filled={a['trig_filled']}/{a['trig_total']}")
print(f"\n产物: {OUT}")
