#!/usr/bin/env python3
"""glm-5.2 thinking 模式探针 (2026-09-05, 用户令"要"跑探针+影子A/B).

问题: UE 主模型 glm-5.2 的 enable_thinking 被 model_call.py L159-160 全局关掉
(2026-07-06 P0, 针对 800-tok 投票/评分小调用, UE 16000-tok 大调用系误伤)。
路由级 extra_body.enable_thinking=true 基建已在 (2026-07-25 E3), 开前需实测:
  1. thinking on + json_mode + schema 校验链是否兼容 (schema 是 UE 生产硬门槛)
  2. thinking token 消耗量级 (是否挤占 16000 预算 → 9/1 触顶事故模式)
  3. thinking on 时延 (生产满载已 210-230s / timeout=300)
  4. DashScope 是否接受 max_tokens > 16000 (16000 系此前 probe 实收上限)
  5. fallback deepseek-v4-pro thinking on 行为 (路由 extra_body 对 fallback 同生效)

纯只读探针: 不改任何配置/不落库/不下单。调用走生产同路径 call_llm。
"""
import sys
import time

sys.path.insert(0, "/opt/ftseek/FTSeek-US")
import config  # noqa: F401  (加载 .env.us → DASHSCOPE_API_KEY)
from app.llm.model_call import _get_llm_config, _try_single_model, call_llm

_PROBE_SYS = "你是事件驱动型美股分析师。严格只输出 JSON。"
_PROBE_USER = (
    '材料: "AAPL 盘前财报营收超预期 +5%; TSLA 交付不及预期 -3%; '
    '半导体板块整体异动 +2.1%"。'
    "浮现受影响标的, 输出 JSON: {\"surfaced\": [{\"symbol\": str, "
    "\"direction\": \"LONG\"/\"SHORT\"/\"SKIP\", \"thesis\": str(≤60字), "
    "\"confidence\": 0~1小数}]}, 1-2 个即可。"
)


def _probe_via_route(tag: str, extra_body, max_tokens: int) -> dict:
    """走 call_llm 全链 (路由/schema校验/json解析) — 与生产完全同构。"""
    t0 = time.time()
    res = call_llm(
        "universe_expansion_reasoning", _PROBE_USER, system=_PROBE_SYS,
        temperature=0.2, max_tokens=max_tokens,
        schema_required={"surfaced": list}, json_mode=True,
        extra_body=extra_body or None,
    )
    dt = time.time() - t0
    row = {"tag": tag, "ok": bool(res and res.success), "latency_s": round(dt, 1)}
    if res and res.success:
        row.update({
            "model": res.model_used, "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out, "finish": res.finish_reason,
            "n_surfaced": len((res.response if isinstance(res.response, dict) else {}).get("surfaced", [])),
        })
    else:
        row["error"] = getattr(res, "error", "no_result") if res else "no_result"
    return row


def _probe_alias_direct(tag: str, alias: str, extra_body, max_tokens: int) -> dict:
    """直调指定别名 (绕过 feature 路由) — 用于探测 fallback 模型本体。"""
    cfg = _get_llm_config()
    t0 = time.time()
    res = _try_single_model(
        alias, cfg, _PROBE_SYS, _PROBE_USER,
        temperature=0.2, max_tokens=max_tokens,
        json_mode=True, extra_body=extra_body or None,
    )
    dt = time.time() - t0
    row = {"tag": tag, "ok": bool(res and res.success), "latency_s": round(dt, 1)}
    if res and res.success:
        import json as _json
        try:
            _json.loads(res.response if isinstance(res.response, str) else "")
            _jv = True
        except Exception:
            _jv = False
        row.update({
            "model": res.model_used, "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out, "finish": res.finish_reason,
            "json_valid": _jv,
        })
    else:
        row["error"] = getattr(res, "error", "no_result") if res else "no_result"
    return row


def main() -> int:
    results = []
    steps = [
        ("P1 glm OFF mt=16000 [生产基线]",
         lambda: _probe_via_route("P1", None, 16000)),
        ("P2 glm ON  mt=16000",
         lambda: _probe_via_route("P2", {"enable_thinking": True}, 16000)),
        ("P3 glm ON  mt=32000 [上限探测]",
         lambda: _probe_via_route("P3", {"enable_thinking": True}, 32000)),
        ("P4 deepseek ON mt=16000 [fallback 本体]",
         lambda: _probe_alias_direct("P4", "deepseek", {"enable_thinking": True}, 16000)),
    ]
    for tag, fn in steps:
        print(f"[probe] {tag} ...", flush=True)
        row = fn()
        results.append(row)
        if row["ok"]:
            extra = (f"json_valid={row.get('json_valid')}" if "json_valid" in row
                     else f"schema_pass=n_surfaced={row.get('n_surfaced')}")
            print(f"  -> OK {row['latency_s']}s model={row['model']} "
                  f"in={row['tokens_in']} out={row['tokens_out']} "
                  f"finish={row['finish']} {extra}", flush=True)
        else:
            print(f"  -> FAIL {row['latency_s']}s err={row['error']}", flush=True)

    print("\n== 判读要点 ==")
    p1 = next((r for r in results if r["tag"] == "P1"), {})
    p2 = next((r for r in results if r["tag"] == "P2"), {})
    if p1.get("ok") and p2.get("ok"):
        print(f"thinking token 增量 ≈ {p2['tokens_out'] - p1['tokens_out']} "
              f"(out {p1['tokens_out']} → {p2['tokens_out']})")
        print(f"时延增量 ≈ {p2['latency_s'] - p1['latency_s']:+.1f}s "
              f"({p1['latency_s']}s → {p2['latency_s']}s; 生产 timeout=300)")
    p3 = next((r for r in results if r["tag"] == "P3"), {})
    print(f"max_tokens=32000 DashScope {'接受' if p3.get('ok') else '拒绝: ' + str(p3.get('error'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
