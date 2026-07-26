#!/usr/bin/env python3
"""离线双臂反事实: claim IC 注入是否影响挑战者方向 (record-only, 绝不交易).

设计 (隔离 claim IC 单一变量, 两臂共用同一份采集数据):
  1. collect_all 采集当前数据 (两臂共用)
  2. data_text = _format_data_for_prompt(collected)
  3. 验证截断: 挑战者只见 data_text[:4000], claim IC 追加在末尾 → 是否被截断
  4. 全链双臂:
     Arm A: analyst(data_text + claim_IC) -> ia_A -> challenger(ia_A, data_text[:4000]) -> dir_A
     Arm B: analyst(data_text)            -> ia_B -> challenger(ia_B, data_text[:4000]) -> dir_B
  5. 记录方向 + 实际模型版本 (用于裁决 claim IC vs 上游模型漂移)
"""
import sys, json, urllib.request
SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
sys.path.insert(0, SKILL)
import config, llm_synth, data_collector

URL = config.DASHSCOPE_URL
KEY = config.DASHSCOPE_KEY

# ── 捕获 _call_llm 真实模型版本 (monkey-patch, 不改变行为) ──
_last_model = {"analyst": None, "challenger": None}
_orig_call = llm_synth._call_llm
def _patched_call(system, user, temperature=0.3, max_tokens=3000, model=None):
    payload = {"model": model or llm_synth.MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"}, "enable_thinking": False}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        _last_model["tag"] = data.get("model", "?")
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)}
llm_synth._call_llm = _patched_call

claim_ic = llm_synth._load_claim_ic_for_prompt()
print("=" * 70)
print("claim_IC 注入文本长度:", len(claim_ic), "字符")
print("claim_IC 预览:\n" + claim_ic[:400] + "\n...(截断)")
print("=" * 70)

IC_SUFFIX = "\n\n## 论据有效性反馈（基于历史IC验证）\n" + claim_ic
SYMBOLS = ["US.TTE", "US.E"]   # 07-20 挑战者 SKIP 的样例
results = []

for sym in SYMBOLS:
    print(f"\n{'#' * 70}\n# {sym} 采集数据 (两臂共用)...")
    try:
        collected = data_collector.collect_all(sym)
    except Exception as e:
        print(f"  采集失败: {e}")
        continue
    data_text = llm_synth._format_data_for_prompt(collected)
    L = len(data_text)
    ic_start = L  # claim IC 追加位置
    print(f"  len(data_text) = {L} 字符")
    print(f"  挑战者视野 = data_text[:4000]; claim IC 起始位置 = {ic_start}")
    print(f"  >>> 挑战者能否直接看到 claim IC? {'能' if ic_start < 4000 else '不能 (被 [:4000] 截断)'}")

    up_base = llm_synth.USER_PROMPT_TEMPLATE.format(data=data_text)
    up_ic = llm_synth.USER_PROMPT_TEMPLATE.format(data=data_text + IC_SUFFIX)

    for arm, up in [("A_with_IC", up_ic), ("B_no_IC", up_base)]:
        # 分析师 (Pass 1)
        ia = llm_synth._call_llm(llm_synth.SYSTEM_PROMPT, up, temperature=0.3, max_tokens=3000)
        analyst_model = _last_model.get("tag", "?")
        if not ia or "_error" in ia:
            print(f"  [{arm}] 分析师错误: {ia}")
            continue
        adir = ia.get("direction", "?")
        # 挑战者 (Pass 2, 真实代码路径, 读 data_text[:4000])
        chall = llm_synth._run_challenger(ia, data_text)
        chall_model = _last_model.get("tag", "?")
        cdir = (chall or {}).get("my_direction", "None")
        cscore = (chall or {}).get("my_score", "?")
        print(f"  [{arm}] 分析师={adir}(model={analyst_model}) | 挑战者={cdir} score={cscore} (model={chall_model})")
        results.append({"symbol": sym, "arm": arm, "analyst_dir": adir,
                        "analyst_model": analyst_model, "challenger_dir": cdir,
                        "challenger_score": cscore, "challenger_model": chall_model})

print("\n" + "=" * 70)
print("汇总 (record-only, 无任何交易):")
print("=" * 70)
for r in results:
    print(json.dumps(r, ensure_ascii=False))
# 裁决提示
a = [r for r in results if r["arm"] == "A_with_IC"]
b = [r for r in results if r["arm"] == "B_no_IC"]
print(f"\nA臂(含claim IC) 挑战者方向: {[r['challenger_dir'] for r in a]}")
print(f"B臂(无claim IC) 挑战者方向: {[r['challenger_dir'] for r in b]}")
