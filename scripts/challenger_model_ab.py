#!/usr/bin/env python3
"""DA 挑战者模型 A/B 对照: deepseek-v4-pro vs glm-5.2 (2026-07-24, 用户裁决"先对照再切").

设计:
  - 输入复刻: 用 opportunity_scan 当日候选重建 initial_analysis (剥离 debate/挑战者旧产物防泄漏),
    data_text 取 playbook_signal_context.raw_data_json (与生产同源素材)。
  - 调用复刻: 直接 import DA skill 的 llm_synth._call_llm (同 HTTP 路径/JSON 解析/key),
    prompt 逐字复刻 _run_challenger 的 system/user 构造 (若 skill 后续改 prompt 本脚本需同步)。
  - record-only: 结果落 data/shadow/challenger_model_ab.jsonl, 不触碰 live 配置。

判据 (预注册, 全部达标才建议切换):
  1. glm-5.2 JSON 成功率 ≥ deepseek 且无空响应 (model_call.py:137 已知 glm reasoning 吃配额风险)
  2. verdict 分布不退化为全 AGREE (挑战者失去独立性 = conf 合成失真)
  3. 平均延迟 < 120s (盘前 DA 单轮已 28min, 不能更慢)
  4. 单次成本可接受 (glm-5 注册价为 deepseek-pro 3 倍)

用法: ./venv/bin/python scripts/challenger_model_ab.py [date] [max_n]
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
OUT = ROOT / "data" / "shadow" / "challenger_model_ab.jsonl"

MODEL_A = "deepseek-v4-pro"   # 现任挑战者
MODEL_B = "glm-5.2"           # 候选替换

# ── 逐字复刻 llm_synth._run_challenger 的 prompt 构造 ──
CHALLENGER_SYSTEM = """你是独立的投资审查官。你的职责是校准另一位分析师的方向判断，而不是一味唱反调或系统性偏空。

审查原则:
1. 不做人身攻击，只质疑论据和数据
2. 找出initial_analysis中最薄弱的1-2条bull_case或bear_case论据
3. 对称质疑方向: 若分析师看多, 检验是否过度乐观(忽视下行风险); 若分析师看空, 同样检验是否过度悲观(忽视利好/超跌反弹/挤空风险)。不预设偏向, 不系统性偏空。
4. 检查direction是否与数据一致（看多但大盘+行业走跌=质疑; 看空但大盘+行业走强=同样质疑）
5. 给出你的独立方向判断——数据支持做空就SELL_SHORT, 支持做多就BUY, 证据不足才SKIP

返回纯JSON格式（不要markdown）:
{
    "agreed": true/false,
    "my_direction": "BUY/SELL_SHORT/SKIP",
    "my_score": 0-10,
    "skip_reason": "仅当 my_direction=SKIP 时必填: 具体观望理由 (缺哪条关键数据/哪个风险不可定价); 无理由的SKIP视为无效审查",
    "challenged_claims": [
        {"original_thesis": "被挑战的论点", "weakness": "薄弱原因", "counter_evidence": "反证"}
    ],
    "missed_risks": ["被忽视的风险1", "被忽视的风险2"],
    "verdict": "AGREE(同意) / PARTIAL_AGREE(部分同意) / DISAGREE(方向分歧)"
}"""


def build_user(data_text: str, initial_analysis: dict) -> str:
    return f"""原始数据:

{data_text[:4000]}

---

另一位分析师的初步分析结论:

{json.dumps(initial_analysis, ensure_ascii=False, indent=2)[:3000]}

---

请审查以上分析。重点检查:
1. direction={initial_analysis.get('direction', '?')} 是否与大盘环境/行业趋势一致?
2. bull_case和bear_case中各自最薄弱的论据是什么?
3. 是否有被忽视的重大风险或被低估的机会(双向对称检查)?
4. execution_plan的止损/止盈是否合理?
5. 你独立判断的方向是什么(做多/做空/观望均可, 由数据决定)?

返回JSON。"""


def load_candidates(date: str, max_n: int):
    scan = ROOT / "data" / f"opportunity_scan_{date}.json"
    if not scan.exists():
        print(f"[AB] scan 文件不存在: {scan}", file=sys.stderr)
        return []
    cands = json.loads(scan.read_text()).get("candidates", [])[:max_n]
    conn = sqlite3.connect(str(ROOT / "data" / "ftseek_us.db"))
    conn.row_factory = sqlite3.Row
    out = []
    for c in cands:
        sym = c.get("symbol", "")
        # 剥离旧挑战者产物 (防泄漏: initial_analysis 应只含 Pass1 视角)
        initial = {k: v for k, v in c.items()
                   if k not in ("debate", "debate_outcome", "challenger_agreed",
                                "challenger_direction", "final_confidence")}
        r = conn.execute(
            "SELECT raw_data_json FROM playbook_signal_context WHERE date=? AND symbol=?",
            (date, sym)).fetchone()
        raw = ""
        if r and r["raw_data_json"]:
            raw = str(r["raw_data_json"])
        if not raw:
            # 兜底: 用候选自身的叙事字段拼 data_text (标记降级)
            raw = json.dumps({k: c.get(k) for k in ("thesis", "sector", "catalyst", "risks")
                              if k in c}, ensure_ascii=False)
        out.append({"symbol": sym, "initial": initial, "data_text": raw})
    conn.close()
    return out


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else None
    max_n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    if not date:
        scans = sorted(ROOT.glob("data/opportunity_scan_*.json"))
        date = scans[-1].stem.replace("opportunity_scan_", "") if scans else ""
    cands = load_candidates(date, max_n)
    if not cands:
        return 1
    print(f"[AB] date={date} 候选 {len(cands)} 只 | A={MODEL_A} B={MODEL_B}")

    sys.path.insert(0, str(SKILL_SCRIPTS))
    import llm_synth  # noqa: E402  (复用 skill 真实调用层)

    stats = {m: {"n": 0, "ok": 0, "empty": 0, "verdicts": {}, "dirs": {}, "flips": 0,
                 "lat": []} for m in (MODEL_A, MODEL_B)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        for c in cands:
            user = build_user(c["data_text"], c["initial"])
            analyst_dir = str(c["initial"].get("direction", "?")).upper()
            row = {"ts": datetime.now().isoformat(), "date": date, "symbol": c["symbol"],
                   "analyst_direction": analyst_dir, "arms": {}}
            for m in (MODEL_A, MODEL_B):
                st = stats[m]
                st["n"] += 1
                t0 = time.time()
                try:
                    res = llm_synth._call_llm(CHALLENGER_SYSTEM, user,
                                              temperature=0.4, max_tokens=2000, model=m)
                except Exception as e:
                    res = {"_error": str(e)}
                lat = round(time.time() - t0, 1)
                st["lat"].append(lat)
                ok = bool(res) and "_error" not in res and res.get("my_direction")
                if ok:
                    st["ok"] += 1
                    v = str(res.get("verdict", "?")).split("(")[0].strip()
                    d = str(res.get("my_direction", "?")).upper()
                    st["verdicts"][v] = st["verdicts"].get(v, 0) + 1
                    st["dirs"][d] = st["dirs"].get(d, 0) + 1
                    if d != analyst_dir:
                        st["flips"] += 1
                elif not res or (isinstance(res, dict) and not res.get("my_direction")
                                 and "_error" not in (res or {})):
                    st["empty"] += 1
                row["arms"][m] = {"ok": bool(ok), "latency_s": lat,
                                  "verdict": (res or {}).get("verdict"),
                                  "my_direction": (res or {}).get("my_direction"),
                                  "error": (res or {}).get("_error")}
                print(f"  {c['symbol']:<10} {m:<16} ok={bool(ok)} {lat:>5.1f}s "
                      f"verdict={(res or {}).get('verdict', '-')} dir={(res or {}).get('my_direction', '-')}")
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\n═══ A/B 汇总 (n={len(cands)}) ═══")
    for m in (MODEL_A, MODEL_B):
        st = stats[m]
        avg_lat = sum(st["lat"]) / len(st["lat"]) if st["lat"] else 0
        print(f"{m}: 成功 {st['ok']}/{st['n']} 空响应 {st['empty']} "
              f"均延迟 {avg_lat:.1f}s 翻方向 {st['flips']} "
              f"verdicts={st['verdicts']} dirs={st['dirs']}")
    print(f"\n明细已追加 → {OUT.relative_to(ROOT)}")
    print("判据: ①glm成功率≥deepseek且0空响应 ②verdict非全AGREE ③均延迟<120s ④成本可接受")
    return 0


if __name__ == "__main__":
    sys.exit(main())
