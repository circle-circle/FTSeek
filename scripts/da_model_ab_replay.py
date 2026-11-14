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
  - 基线模型的 thinking/max_tokens 从生产 config 实读，禁止再用 3000 截断当前输出；
  - 每模型同一样本集；样本按日分层，避免单日主导；可用 --repeats 量化同输入波动；
  - 结论只看"三率"（有效/方向/命中），不看个别样本的漂亮论点；
  - 默认 dry-run，只有显式 --execute 才允许付费 API 调用；输出文件不得覆盖。

冻结 NVDA 案例（仅回放，不写生产）:
  venv/bin/python scripts/da_model_ab_replay.py --n 8 --per-day 8 \
    --dates 2026-11-12 --symbols NVDA,AMD,AVGO,TSM,MU,SNDK,ARM,NBIS \
    --models qwen3.7-max,deepseek-v4-pro --repeats 1 --allow-missing-bars --execute \
    --out data/shadow/da_model_ab_nvda_20261112_v1.jsonl
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
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
# 输出预算: GLM 系思考 token 计入 completion 预算, 3000 会被思考吃光 (实测 finish=length,
# content 仅 "{\n") ⇒ 按其官方上限给 16000 (与 config 里 UE 对 glm-5.3 的 16000 一致)。
# 这是**平台计费模型的约束**, 不是能力差异; 若真要用 GLM 当 DA, 必须同步抬 max_tokens。
# 实测补充 (2026-11-07 n=12): deepseek-v4-pro 同样把 reasoning 算进 completion 预算
#   (3000 全烧, finish=length, 内容空) ⇒ 与 GLM 同处置; qwen3.8-max 单次 >240s 超时 ⇒ 抬超时.
MAXTOK = {"glm-5.3": 16000, "glm-5.2": 16000, "glm-5.3-prime": 16000,
          "deepseek-v4-pro": 16000, "qwen3.8-max": 16000, "kimi-k3.1-pro": 32768,
          "kimi-k3": 32768, "deepseek-v4.1-flash": 32768}


def production_call_settings() -> Dict[str, Any]:
    """实读 DA 生产调用口径；回放基线不得沿用已漂移的 3000 文档值。"""
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    import config as da_config  # type: ignore
    thinking = bool(getattr(da_config, "DA_THINKING_ENABLED", False))
    budget = int(getattr(da_config, "DA_THINKING_BUDGET", 0) or 0)
    max_tokens = 3000
    if thinking:
        max_tokens = budget + 8000 if budget > 0 else 16000
    return {
        "model": getattr(da_config, "LLM_MODEL", "qwen3.7-max"),
        "temperature": float(getattr(da_config, "LLM_TEMPERATURE", 0.0)),
        "thinking": thinking,
        "thinking_budget": budget,
        "max_tokens": max_tokens,
    }


def model_max_tokens(model: str, thinking: bool = True) -> int:
    settings = production_call_settings()
    if model == settings["model"]:
        return settings["max_tokens"] if thinking else 3000
    return MAXTOK.get(model, 3000)


def load_dashscope_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        return key
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    import config as da_config  # type: ignore
    return str(getattr(da_config, "DASHSCOPE_KEY", "") or "")


def load_system_prompt() -> str:
    """从生产 skill 取静态 SYSTEM_PROMPT（不做任何改写）。"""
    sys.path.insert(0, str(SKILL_SCRIPTS))
    import llm_synth  # type: ignore
    return llm_synth.SYSTEM_PROMPT


SKIP_CONTRACT = """
【方向—执行计划一致性补充契约】
当 direction=SKIP 时，只允许输出研究观察与失效条件；execution_plan 中的
entry_price/entry_archetype/stop_loss/stop_loss_price/take_profit/take_profit_price/
confirmation/chase_cap_price 必须全部为 null，不得同时给出可执行 BUY/SELL_SHORT 计划。
若需记录右侧观察条件，写入 entry_strategy 的研究文本、invalidation_triggers 或 tripwires，
但不得填写上述方向性执行字段。当 direction=BUY/SELL_SHORT 时，最终动作不得再写成 SKIP。
"""


def build_prompt_variant(prompt: str, skip_contract: bool = False) -> str:
    return prompt + SKIP_CONTRACT if skip_contract else prompt


def day_bars(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def collect_samples(n: int, seed: int = 20261107, dates=None, symbols=None,
                    per_day: int = 2, require_bars: bool = True) -> List[dict]:
    """按日分层抽样；dates/symbols 可冻结指定案例，输入哈希随样本留档。"""
    dates = {str(item) for item in (dates or [])}
    symbols = {str(item).upper().removeprefix("US.") for item in (symbols or [])}
    files = sorted(glob.glob(str(CKPT / "*" / "*.raw.json")))
    by_day: Dict[str, List[str]] = {}
    for f in files:
        d = Path(f).parent.name
        if dates and d not in dates:
            continue
        by_day.setdefault(d, []).append(f)
    rng = random.Random(seed)
    picked: List[dict] = []
    days = sorted(by_day, reverse=True)
    rng.shuffle(days)
    for d in days:
        cand = []
        for f in by_day[d]:
            j = json.loads(Path(f).read_text())
            symbol = str(j.get("symbol") or "")
            if symbols and symbol.upper().removeprefix("US.") not in symbols:
                continue
            if j.get("user_prompt") and (not require_bars or day_bars(symbol, d)):
                cand.append((f, j))
        rng.shuffle(cand)
        for f, j in cand[:per_day]:
            prompt = j["user_prompt"]
            try:
                raw_path = str(Path(f).relative_to(ROOT))
            except ValueError:
                raw_path = str(Path(f))
            picked.append({
                "day": d, "symbol": j["symbol"], "prompt": prompt,
                "raw_path": raw_path,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            })
            if len(picked) >= n:
                return picked
    return picked


def call_model(model: str, system: str, user: str, timeout: int = 900,
               thinking: bool = True) -> dict:
    key = load_dashscope_key()
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    settings = production_call_settings()
    max_tokens = model_max_tokens(model, thinking)
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": settings["temperature"],
        "max_tokens": max_tokens,
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
        return {"ok": False, "error": msg, "latency": round(time.time() - t0, 1),
                "max_tokens": max_tokens}
    lat = round(time.time() - t0, 1)
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {"ok": True, "content": content, "latency": lat,
            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "max_tokens": max_tokens,
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
    out = {"valid_card": False, "direction": None, "dir_rate": 0,
           "research_complete": False, "price_geometry_valid": None,
           "direction_plan_consistent": None,
           "reachable": None, "hit": None, "fwd": None, "rr": None}
    if not card:
        return out
    d = str(card.get("direction") or "").upper()
    out["direction"] = d or None
    if d not in ("BUY", "SELL_SHORT", "SKIP"):
        return out
    out["valid_card"] = True
    out["research_complete"] = bool(
        str(card.get("executive_summary") or "").strip()
        and str(card.get("causal_chain") or "").strip()
        and isinstance(card.get("bull_case"), list) and card.get("bull_case")
        and isinstance(card.get("bear_case"), list) and card.get("bear_case")
    )
    if d == "SKIP":
        ep = card.get("execution_plan") or {}
        out["direction_plan_consistent"] = not any(
            ep.get(key) not in (None, "")
            for key in ("entry_price", "entry_archetype", "stop_loss", "stop_loss_price",
                        "take_profit", "take_profit_price", "chase_cap_price")
        ) and not (ep.get("confirmation") or {}).get("price_level")
        return out
    out["direction_plan_consistent"] = True
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
    if d == "BUY":
        out["price_geometry_valid"] = bool(stop < entry < tp)
    else:
        out["price_geometry_valid"] = bool(tp < entry < stop)
    if out["price_geometry_valid"]:
        out["rr"] = round(abs(tp - entry) / abs(entry - stop), 2)
    sign = 1 if d == "BUY" else -1
    if bars:
        lo = min(b[3] for b in bars)
        hi = max(b[2] for b in bars)
        out["reachable"] = bool(lo <= entry <= hi)
        if out["reachable"]:
            close = bars[-1][4]
            out["fwd"] = round(sign * (close - entry) / entry * 100, 3)
            out["hit"] = bool(out["fwd"] > 0)
    return out


def _csv(value: str) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def summarize_records(recs: List[dict], models: List[str], repeats: int) -> Dict[str, dict]:
    summary: Dict[str, dict] = {}
    for model in models:
        rows = [r for r in recs if r["model"] == model]
        if not rows:
            continue
        reachable = [r for r in rows if r["reachable"]]
        fwd = [r["fwd"] for r in reachable if r["fwd"] is not None]
        rr = [r["rr"] for r in rows if r["rr"] is not None]
        latency = [r["latency"] for r in rows if r["latency"] is not None]
        priced = [r for r in rows if r.get("cost_usd") is not None]
        groups: Dict[tuple, List[dict]] = {}
        for row in rows:
            groups.setdefault((row["day"], row["symbol"]), []).append(row)
        stable = [len({r["direction"] for r in group}) == 1 for group in groups.values()]
        summary[model] = {
            "n": len(rows),
            "api_ok_rate": sum(bool(r["ok"]) for r in rows) / len(rows),
            "valid_card_rate": sum(bool(r["valid_card"]) for r in rows) / len(rows),
            "research_complete_rate": sum(bool(r["research_complete"]) for r in rows) / len(rows),
            "direction_plan_consistent_rate": (
                sum(r["direction_plan_consistent"] is True for r in rows if r["valid_card"])
                / max(1, sum(bool(r["valid_card"]) for r in rows))
            ),
            "direction_rate": sum(bool(r["dir_rate"]) for r in rows) / len(rows),
            "price_geometry_rate": (
                sum(r["price_geometry_valid"] is True for r in rows if r["dir_rate"])
                / max(1, sum(bool(r["dir_rate"]) for r in rows))
            ),
            "reachable_rate": len(reachable) / len(rows),
            "hit_rate_reachable": (sum(bool(r["hit"]) for r in reachable) / len(reachable)) if reachable else None,
            "median_fwd_pct": statistics.median(fwd) if fwd else None,
            "median_rr": statistics.median(rr) if rr else None,
            "mean_latency_s": statistics.mean(latency) if latency else None,
            "cost_usd": round(sum(r["cost_usd"] for r in priced), 5) if len(priced) == len(rows) else None,
            "cost_coverage": len(priced) / len(rows),
            "direction_counts": dict(collections.Counter(r["direction"] for r in rows)),
            "finish_counts": dict(collections.Counter(r["finish"] for r in rows)),
            "repeat_groups": len(groups) if repeats > 1 else 0,
            "stable_direction_groups": sum(stable) if repeats > 1 else None,
            "stable_direction_rate": (sum(stable) / len(stable)) if repeats > 1 and stable else None,
        }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--models", default="qwen3.7-max,deepseek-v4-pro,glm-5.3")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dates", default="", help="逗号分隔；默认从全部 checkpoint 分层抽样")
    ap.add_argument("--symbols", default="", help="逗号分隔，US. 前缀可省")
    ap.add_argument("--per-day", type=int, default=2)
    ap.add_argument("--allow-missing-bars", action="store_true",
                    help="保留无本地 bars 的标的；价格/收益指标记 null，不伪造")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-thinking", action="store_true", help="关闭思考 (默认开, 对齐生产)")
    ap.add_argument("--skip-contract", action="store_true",
                    help="独立 Prompt 臂：SKIP 时禁止输出方向性执行字段")
    ap.add_argument("--execute", action="store_true", help="显式授权付费 API；缺省仅 dry-run")
    a = ap.parse_args()
    if a.n < 1 or a.per_day < 1 or a.repeats < 1 or a.workers < 1:
        raise SystemExit("n/per-day/repeats/workers 必须 >=1")

    models = _csv(a.models)
    samples = collect_samples(a.n, seed=a.seed, dates=_csv(a.dates),
                              symbols=_csv(a.symbols), per_day=a.per_day,
                              require_bars=not a.allow_missing_bars)
    if not samples:
        raise SystemExit("无匹配样本（检查 dates/symbols 及当日 bars）")
    settings = production_call_settings()
    call_count = len(samples) * len(models) * a.repeats
    print("样本 %d 只（%d 天）| 模型 %d | repeats %d | API 调用 %d" % (
        len(samples), len({s["day"] for s in samples}), len(models), a.repeats, call_count))
    print("生产基线: %s" % json.dumps(settings, ensure_ascii=False))
    print("模型: %s | skip_contract=%s" % (", ".join(models), a.skip_contract))
    if not a.execute:
        print("DRY-RUN：未调用模型、未写文件；加 --execute 才执行付费回放")
        return 0
    if not load_dashscope_key():
        raise SystemExit("DASHSCOPE_API_KEY 未设置")

    system = load_system_prompt()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = Path(a.out or OUT_DIR / f"da_model_ab_{stamp}.jsonl")
    manifest_path = path.with_suffix(".manifest.json")
    if path.exists() or manifest_path.exists():
        raise SystemExit(f"输出已存在，拒绝覆盖: {path} / {manifest_path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    recs: List[dict] = []
    from concurrent.futures import ThreadPoolExecutor
    jobs = [(m, s, repeat) for repeat in range(1, a.repeats + 1) for m in models for s in samples]

    def _run(job):
        m, s, repeat = job
        prompt = build_prompt_variant(s["prompt"], a.skip_contract)
        result = call_model(m, system, prompt, thinking=not a.no_thinking)
        result["effective_prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return m, s, repeat, result

    def _handle(res):
        m, s, repeat, r = res
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
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": m,
               "day": s["day"], "symbol": s["symbol"], "repeat": repeat,
               "raw_path": s["raw_path"], "prompt_sha256": r.get("effective_prompt_sha256"),
               "original_prompt_sha256": s["prompt_sha256"],
               "ok": bool(r.get("ok")), "error": r.get("error"),
               "latency": r.get("latency"), "tokens_in": r.get("tokens_in"),
               "tokens_out": r.get("tokens_out"), "reasoning_tokens": r.get("reasoning_tokens"),
               "max_tokens": r.get("max_tokens"), "cost_usd": cost,
               "model_reported": r.get("model_reported"), "finish": r.get("finish"),
               "content_sha256": hashlib.sha256((r.get("content") or "").encode()).hexdigest(),
               "raw_head": (r.get("content") or "")[:600], "card": card,
               **sc, "card_confidence": (card or {}).get("confidence"),
               "card_rating": (card or {}).get("rating"),
               "card_score": (card or {}).get("score", (card or {}).get("score_band"))}
        recs.append(rec)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        print("  %-20s %s %-6s r%d valid=%s dir=%-10s geometry=%-5s reach=%-5s hit=%-5s fwd=%s %ss" % (
            m, s["day"], s["symbol"].replace("US.", ""), repeat, rec["valid_card"],
            rec["direction"], rec["price_geometry_valid"], rec["reachable"], rec["hit"],
            rec["fwd"], rec["latency"]))

    with path.open("x") as fh:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for res in ex.map(_run, jobs):
                _handle(res)

    summary = summarize_records(recs, models, a.repeats)
    manifest = {
        "schema": "da_model_ab_replay_v2",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "units": {
            "rates": "ratio_0_to_1", "cost_usd": "USD_estimated_from_PRICE_table",
            "latency_s": "seconds", "fwd_pct": "percent", "tokens": "count",
        },
        "production_settings": settings,
        "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "models": models, "repeats": a.repeats, "thinking": not a.no_thinking,
        "skip_contract": a.skip_contract,
        "sample_count": len(samples), "api_call_count": len(recs),
        "samples": [{k: v for k, v in s.items() if k != "prompt"} for s in samples],
        "summary": summary, "production_write": False,
        "status": "COMPLETE_WITH_API_ERRORS" if any(not r["ok"] for r in recs) else "COMPLETE",
    }
    with manifest_path.open("x") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    print("\n== 汇总（单例/单次只作回归，不作换模型裁决） ==")
    for model, row in summary.items():
        print("%s: valid=%.0f%% research=%.0f%% consistent=%.0f%% geometry=%.0f%% reach=%.0f%% hit=%s stable=%s cost=%s" % (
            model, row["valid_card_rate"] * 100, row["research_complete_rate"] * 100,
            row["direction_plan_consistent_rate"] * 100, row["price_geometry_rate"] * 100, row["reachable_rate"] * 100,
            ("%.0f%%" % (row["hit_rate_reachable"] * 100)) if row["hit_rate_reachable"] is not None else "n/a",
            ("%.0f%%" % (row["stable_direction_rate"] * 100)) if row["stable_direction_rate"] is not None else "n/a",
            ("$%.3f" % row["cost_usd"]) if row["cost_usd"] is not None else "n/a"))
    print("明细: %s\n清单: %s" % (path, manifest_path))
    errs = [r for r in recs if not r["ok"]]
    if errs:
        print("失败 %d 条，示例: %s" % (len(errs), errs[0]["error"][:160]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())