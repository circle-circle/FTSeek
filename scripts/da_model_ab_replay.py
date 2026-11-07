#!/usr/bin/env python3
"""DA 分析师模型 A/B 回放：同一 prompt，只换模型 (DEB-20261106 §36)。

为什么可行：`data/da_checkpoint/<date>/<SYM>.raw.json` 里 **存了当年真实发给模型的
`user_prompt` 原文**（16–19k 字符），加上 skill 侧静态 SYSTEM_PROMPT ⇒ 可以做到
**输入逐字节一致、只有 model 变**，这是唯一干净的换模型对比方式。

指标（全部客观可判，预注册）:
  valid_card   输出能解析出 JSON 且 direction ∈ {BUY,SELL_SHORT,SKIP}
  dir_rate     给出方向（非 SKIP）的比例 —— 这是"有效卡"口径的核心
  reachable    给出入场价且当日价格真的触及该价位（否则无法成交）
  hit          可达样本里 方向 × (close−entry)/entry > 0 的比例
  median_fwd   可达样本的 方向调整后 到收盘收益 中位
  rr           中位 风险回报比 |tp−entry|/|entry−stop|
  cost/latency 由 dashscope usage 与墙钟得出

纪律:
  - temperature=0.0（与生产 config 一致），max_tokens=3000；
  - 每模型同一样本集；样本按日分层，避免单日主导；
  - 结论只看"三率"（有效/方向/命中），不看个别样本的漂亮论点。

用法:
  venv/bin/python scripts/da_model_ab_replay.py --n 9 --models qwen3.7-max,deepseek-v4-pro,glm-5.3
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
CKPT = ROOT / "data" / "da_checkpoint"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
OUT_DIR = ROOT / "data" / "shadow"
URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 预注册成本表 ($/1M token)，仅用于报表，不参与判定
PRICE = {
    "qwen3.7-max": (9.79, 15.12),
    "deepseek-v4-pro": (1.89, 2.13),
    "deepseek-v4.1-flash": (0.64, 0.21),
    "qwen3.8-flash": (0.39, 0.39),
    "glm-5.3": (6.96, 2.13),
    "glm-5.2": (19.16, 7.68),
}
# 需要显式开启思考的模型 (dashscope 对 glm-5.3 缺参数会 400)
THINKING = {"glm-5.3", "glm-5.2"}


def load_system_prompt() -> str:
    """从生产 skill 取静态 SYSTEM_PROMPT（不做任何改写）。"""
    sys.path.insert(0, str(SKILL_SCRIPTS))
    import llm_synth  # type: ignore
    return llm_synth.SYSTEM_PROMPT


def day_bars(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def collect_samples(n: int, seed: int = 20261107) -> List[dict]:
    """按日分层抽样：每天最多 2 只，确保有 bar、有 prompt。"""
    files = sorted(glob.glob(str(CKPT / "*" / "*.raw.json")))
    by_day: Dict[str, List[str]] = {}
    for f in files:
        d = Path(f).parent.name
        by_day.setdefault(d, []).append(f)
    rng = random.Random(seed)
    picked: List[dict] = []
    days = sorted(by_day, reverse=True)
    rng.shuffle(days)
    for d in days:
        cand = [f for f in by_day[d] if day_bars(json.loads(Path(f).read_text())["symbol"], d)]
        rng.shuffle(cand)
        for f in cand[:2]:
            j = json.loads(Path(f).read_text())
            if not j.get("user_prompt"):
                continue
            picked.append({"day": d, "symbol": j["symbol"], "prompt": j["user_prompt"]})
            if len(picked) >= n:
                return picked
    return picked


def call_model(model: str, system: str, user: str, timeout: int = 240,
               thinking: bool = True) -> dict:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
        # 生产基准 (2026-10 DA checkpoint) 实测 _thinking_enabled=True、约 6k reasoning token,
        # 故默认对齐"开思考"; --no-thinking 可切回。
        "enable_thinking": bool(thinking),
    }
    if model in THINKING and thinking:
        body["reasoning_effort"] = "high"
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception as e:  # 含 HTTPError
        msg = ""
        try:
            msg = e.read().decode()[:300]  # type: ignore[attr-defined]
        except Exception:
            msg = str(e)[:300]
        return {"ok": False, "error": msg, "latency": round(time.time() - t0, 1)}
    lat = round(time.time() - t0, 1)
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return {"ok": True, "content": content, "latency": lat,
            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
            "model_reported": data.get("model"), "finish": choice.get("finish_reason")}


def parse_card(content: str) -> Optional[dict]:
    s = content.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s else s
        s = s.lstrip("json").strip()
    try:
        return json.loads(s)
    except Exception:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                return None
        return None


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


def score(card: Optional[dict], bars: Optional[List[list]]) -> dict:
    out = {"valid_card": bool(card), "direction": None, "dir_rate": 0,
           "reachable": None, "hit": None, "fwd": None, "rr": None}
    if not card:
        return out
    d = str(card.get("direction") or "").upper()
    out["direction"] = d
    if d not in ("BUY", "SELL_SHORT", "SKIP"):
        return out
    if d == "SKIP":
        return out
    out["dir_rate"] = 1
    ep = card.get("execution_plan") or {}
    entry = (_num(ep.get("entry_price")) or _num(card.get("entry_price"))
             or _num((ep.get("confirmation") or {}).get("price_level")))
    stop = (_num(ep.get("stop_loss_price")) or _num(ep.get("stop_loss"))
            or _num(card.get("stop_loss_price")) or _num(card.get("stop_loss")))
    tp = (_num(ep.get("take_profit_price")) or _num(ep.get("take_profit"))
          or _num(card.get("take_profit_price")) or _num(card.get("take_profit")))
    if not (entry and stop and tp):
        return out
    sign = 1 if d == "BUY" else -1
    if abs(tp - entry) > 0 and abs(entry - stop) > 0:
        out["rr"] = round(abs(tp - entry) / abs(entry - stop), 2)
    if bars:
        lo = min(b[3] for b in bars)
        hi = max(b[2] for b in bars)
        out["reachable"] = bool(lo <= entry <= hi)
        if out["reachable"]:
            close = bars[-1][4]
            out["fwd"] = round(sign * (close - entry) / entry * 100, 3)
            out["hit"] = bool(out["fwd"] > 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--models", default="qwen3.7-max,deepseek-v4-pro,glm-5.3")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-thinking", action="store_true", help="关闭思考 (默认开, 对齐生产)")
    a = ap.parse_args()

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    samples = collect_samples(a.n)
    system = load_system_prompt()
    print("样本 %d 只（%d 天）| system prompt %d 字符" % (
        len(samples), len({s["day"] for s in samples}), len(system)))
    print("模型: %s\n" % ", ".join(models))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    path = Path(a.out or OUT_DIR / f"da_model_ab_{stamp}.jsonl")
    recs = []
    for m in models:
        for s in samples:
            r = call_model(m, system, s["prompt"], thinking=not a.no_thinking)
            bars = day_bars(s["symbol"], s["day"])
            if r.get("ok"):
                card = parse_card(r["content"])
                sc = score(card, bars)
                cost = None
                if m in PRICE and r.get("tokens_in"):
                    pin, pout = PRICE[m]
                    cost = round((r["tokens_in"] * pin + (r["tokens_out"] or 0) * pout) / 1e6, 5)
            else:
                sc, card, cost = score(None, bars), None, None
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": m, "day": s["day"],
                   "symbol": s["symbol"], "ok": bool(r.get("ok")), "error": r.get("error"),
                   "latency": r.get("latency"), "tokens_in": r.get("tokens_in"),
                   "tokens_out": r.get("tokens_out"), "cost_usd": cost,
                   "model_reported": r.get("model_reported"), "finish": r.get("finish"),
                   **sc, "card_confidence": (card or {}).get("confidence"),
                   "card_rating": (card or {}).get("rating")}
            recs.append(rec)
            print("  %-20s %s %-6s valid=%s dir=%-10s reach=%-5s hit=%-5s fwd=%s %ss" % (
                m, s["day"], s["symbol"].replace("US.", ""), rec["valid_card"],
                rec["direction"], rec["reachable"], rec["hit"], rec["fwd"], rec["latency"]))

    with path.open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n== 汇总 (n=%d/模型) ==" % len(samples))
    print("%-20s %7s %8s %8s %8s %8s %9s %8s %9s" % (
        "模型", "有效%", "方向%", "可达%", "命中%", "中位fwd", "中位RR", "延迟s", "成本$"))
    for m in models:
        v = [r for r in recs if r["model"] == m]
        if not v:
            continue
        ok = sum(1 for r in v if r["ok"]) / len(v) * 100
        dr = sum(1 for r in v if r["dir_rate"]) / len(v) * 100
        rc = [r for r in v if r["reachable"]]
        hit = (sum(1 for r in rc if r["hit"]) / len(rc) * 100) if rc else 0
        fwd = [r["fwd"] for r in rc if r["fwd"] is not None]
        rr = [r["rr"] for r in v if r["rr"]]
        lat = [r["latency"] for r in v if r["latency"]]
        cost = sum(r["cost_usd"] or 0 for r in v)
        print("%-20s %6.0f%% %7.0f%% %7.0f%% %7.0f%% %8s %8s %8.1f %9.3f" % (
            m, ok, dr, (len(rc) / len(v) * 100) if v else 0, hit,
            ("%.2f%%" % statistics.median(fwd)) if fwd else "n/a",
            ("%.2f" % statistics.median(rr)) if rr else "n/a",
            statistics.mean(lat) if lat else 0, cost))
    print("\n明细: %s" % path)
    errs = [r for r in recs if not r["ok"]]
    if errs:
        print("失败 %d 条，示例: %s" % (len(errs), errs[0]["error"][:160]))
    return 0


if __name__ == "__main__":
    sys.exit(main())