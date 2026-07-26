#!/usr/bin/env python3
"""稳健性检验: TTE 双臂各 3 轮, 确认 claim IC 对挑战者方向的影响是否稳定 (record-only)."""
import sys, json, urllib.request
SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
sys.path.insert(0, SKILL)
import config, llm_synth, data_collector

URL, KEY = config.DASHSCOPE_URL, config.DASHSCOPE_KEY
def call(system, user, temperature=0.3, max_tokens=3000, model=None):
    payload = {"model": model or llm_synth.MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"}, "enable_thinking": False}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)}
llm_synth._call_llm = call

claim_ic = llm_synth._load_claim_ic_for_prompt()
IC_SUFFIX = "\n\n## 论据有效性反馈（基于历史IC验证）\n" + claim_ic
N = 3
sym = "US.TTE"
print(f"采集 {sym} 数据一次, 双臂各跑 {N} 轮 (温度0.3)...")
collected = data_collector.collect_all(sym)
data_text = llm_synth._format_data_for_prompt(collected)
print(f"len(data_text)={len(data_text)} (claim IC 可见? {len(data_text)<4000})")
up_base = llm_synth.USER_PROMPT_TEMPLATE.format(data=data_text)
up_ic = llm_synth.USER_PROMPT_TEMPLATE.format(data=data_text + IC_SUFFIX)

tally = {"A_with_IC": [], "B_no_IC": []}
for arm, up in [("A_with_IC", up_ic), ("B_no_IC", up_base)]:
    for i in range(N):
        ia = call(llm_synth.SYSTEM_PROMPT, up)
        if not ia or "_error" in ia:
            print(f"  [{arm}#{i}] analyst err {ia}"); continue
        chall = llm_synth._run_challenger(ia, data_text)
        cdir = (chall or {}).get("my_direction", "None")
        cs = (chall or {}).get("my_score", "?")
        tally[arm].append(cdir)
        print(f"  [{arm}#{i+1}] analyst={ia.get('direction')} -> challenger={cdir} score={cs}")

print("\n=== 稳健性汇总 ===")
for arm, dirs in tally.items():
    from collections import Counter
    print(f"{arm}: {dirs}  分布={dict(Counter(dirs))}")
