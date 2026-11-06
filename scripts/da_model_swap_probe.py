#!/usr/bin/env python3
"""模型能力对拍探针 (DEB-20261106 §十九; 只读 + 隔离输出).

回答两件事, **不看模型名, 只看实测**:
  1. `repeat`  : 同一 DA 控制输入 (无事件简报) 在指定主模型下重复 N 次 → 方向/score/契约/时延/token 分布。
                 (与 scripts/da_repeatability_probe.py 同法, 但允许换主模型)
  2. `battery` : 一组**可判对错**的客观小项 (JSON 契约 / 算术与单位 / 多约束排序 / 数值抽取 / 信息不足拒答),
                 两模型同题同参 → 逐项对错 + 总命中率。

为什么需要它: 「max 比 flash 强」是名字直觉, 不是测量。若 flash 在同题上不劣, 降档才是可用选项;
若劣, 劣在哪一类 (契约 / 算术 / 拒答) 也要能点名。

零副作用: 不写生产库 / 无 cron / 输出目录必须不存在。
用法:
  cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python3.11 scripts/da_model_swap_probe.py \
      --mode repeat --main-model deepseek-v4.1-flash --runs 6 \
      --raw-root data/da_checkpoint/2026-11-05 --symbol AMD \
      --output-dir data/shadow/event_intelligence/model_swap_flash_v1 --execute
  ... --mode battery --main-model deepseek-v4.1-flash --output-dir ..._battery_flash_v1 --execute

回滚: 删本脚本 + 对应输出目录。
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# ── 客观能力小项 ───────────────────────────────────────────────────────────
# 每题: id / system / user / 判分函数(对 answer_text 判 True/False) / 说明
def _json_ok(text: str) -> bool:
    try:
        obj = json.loads(text)
    except Exception:
        # 允许 ```json 包裹
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return False
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return False
    return (
        isinstance(obj, dict)
        and obj.get("direction") in ("BUY", "SELL_SHORT", "SKIP")
        and isinstance(obj.get("entry"), (int, float))
        and isinstance(obj.get("stop"), (int, float))
        and isinstance(obj.get("confidence"), (int, float))
        and 0 <= obj["confidence"] <= 1
    )


def _arith_ok(text: str) -> bool:
    # 名义 = 1200 * 45.5 = 54600; 费用 = 54600 * 0.0005 * 2 = 54.6; 净额 = -54.6 相关
    # 千分位必须能解析: "54,600" 原实现会被切成 "54"/"600" ⇒ 假红 (2026-11-06 修)
    nums = re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)
    return any(abs(float(n.replace(",", "")) - 54600) < 1 for n in nums)


def _order_ok(text: str) -> bool:
    # 正解 = 先排除 B(97.8 < 98), 余下按价降序 = C(101.5) > A(99.2) ⇒ 只应出现 C,A
    # (原实现要求 "C A B" 三字母 = 判分错误, 2026-11-06 修)
    m = re.findall(r"[ABC]", text.upper())
    return m[:2] == ["C", "A"] and "B" not in m


def _extract_ok(text: str) -> bool:
    # 文中 "净额 -1,234.50 美元" 与 "滑点 12.5 bps" 两个数都要出现
    nums = re.findall(r"-?[\d,]+(?:\.\d+)?", text)
    flat = {n.replace(",", "").lstrip("-") for n in nums}   # 符号不参与比对(净额本身就是负)
    return "1234.50" in flat and "12.5" in flat


def _abstain_ok(text: str) -> bool:
    # 信息不足必须说 UNKNOWN/无法判断, 不得编数字
    up = text.upper()
    return ("UNKNOWN" in up or "无法判断" in text or "信息不足" in text) and not re.search(
        r"\d{2,}\s*(bps|美元|USD)", text
    )


BATTERY = [
    {
        "id": "json_contract",
        "system": "你是交易指令生成器, 只输出严格 JSON, 不要任何解释文字。",
        "user": (
            '输出一个 JSON 对象, 键恰好为 direction/entry/stop/target/confidence。'
            'direction 取 BUY|SELL_SHORT|SKIP; entry/stop/target 为数字; confidence 为 0-1 的小数。'
            '内容: 某股现价 100, 计划 99.5 买入, 止损 97.0, 目标 104.0, 信心 0.62。'
        ),
        "check": _json_ok,
        "note": "严格 JSON + 枚举 + 数值区间 (契约合规)",
    },
    {
        "id": "arith_notional",
        "system": "你是交易成本核算器, 只给数字与一行算式。",
        "user": (
            '1200 股以 45.5 美元成交, 单边费率 5bps。'
            '问: 这笔的**名义成交额**是多少美元? 只答名义额(不要答费用)。'
        ),
        "check": _arith_ok,
        "note": "名义额 = 1200*45.5 = 54600 (单位与对象识别)",
    },
    {
        "id": "multi_constraint_order",
        "system": "你只输出排序结果。",
        "user": (
            '三只票: A 价 99.2, B 价 97.8, C 价 101.5。'
            '要求: 排除价格低于 98 的; 其余按价格从高到低排序。只输出字母序列。'
        ),
        "check": _order_ok,
        "note": "先过滤再排序 (多约束不能漏条件)",
    },
    {
        "id": "numeric_extract",
        "system": "你是记账员, 只答抽取到的两个数字。",
        "user": (
            '纪要: "本笔净额 -1,234.50 美元, 相对开盘价滑点 12.5 bps, 手续费另计。"'
            '问: 净额与滑点分别是多少? 按 "净额=..., 滑点=..." 回答。'
        ),
        "check": _extract_ok,
        "note": "千分位与小数位不失真",
    },
    {
        "id": "abstain_on_insufficient",
        "system": "你只输出结论。",
        "user": (
            '某标的昨天涨停, 今天盘前无任何价格与成交量数据。'
            '问: 今天开盘 5 分钟内相对昨收的预期涨跌幅是多少 bps? 给一个数。'
        ),
        "check": _abstain_ok,
        "note": "信息不足必须拒答 (反幻觉; 给了数字即失败)",
    },
]


SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")


def _resolve_llm(main_model: str | None):
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    import llm_synth  # noqa: F401  (延迟导入: 允许先设环境/再改模块属性)

    if main_model:
        llm_synth.MODEL = main_model
    return llm_synth


def _http_call(model: str, system: str, user: str, max_tokens: int = 600):
    """直连 DashScope 兼容端点, 不经 DA wrapper: 隔离"模型能力"与"wrapper 约束(如 json_mode)"。"""
    import json as _json
    import urllib.error
    import urllib.request

    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    import config  # type: ignore

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        config.DASHSCOPE_URL, data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.DASHSCOPE_KEY},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0]
        return {"text": (choice.get("message") or {}).get("content") or "",
                "model_reported": data.get("model"), "usage": data.get("usage") or {},
                "error": None}
    except urllib.error.HTTPError as exc:
        return {"text": "", "model_reported": None, "usage": {},
                "error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"text": "", "model_reported": None, "usage": {}, "error": f"{type(exc).__name__}: {exc}"}


def run_battery(main_model: str, out_dir: Path) -> dict:
    llm = None
    out_dir.mkdir(parents=True)
    results = []
    for item in BATTERY:
        t0 = time.time()
        res = _http_call(main_model, item["system"], item["user"], max_tokens=600)
        elapsed = time.time() - t0
        text = res["text"]
        reported = res["model_reported"]
        usage = res["usage"]
        ok = bool(item["check"](text)) if not res["error"] else False
        if res["error"]:
            print(f"[battery] {item['id']} transport error: {res['error']}", file=sys.stderr)
        results.append({
            "id": item["id"], "passed": ok, "elapsed_s": round(elapsed, 2),
            "model_requested": main_model, "model_reported": reported,
            "usage": usage, "error": res["error"], "answer_excerpt": text[:400], "note": item["note"],
        })
        print(f"[battery] {item['id']:26s} {'PASS' if ok else 'FAIL'} {elapsed:.1f}s "
              f"model={reported}", file=sys.stderr)
    summary = {
        "schema": "da_model_capability_battery_v1",
        "model_requested": main_model,
        "items_total": len(results),
        "items_passed": sum(1 for r in results if r["passed"]),
        "by_item": {r["id"]: r["passed"] for r in results},
        "model_reported_set": sorted({str(r["model_reported"]) for r in results}),
        "production_write": False,
        "results": results,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("model_requested", "items_passed", "items_total",
                                              "by_item", "model_reported_set")}, ensure_ascii=False))
    return summary


def run_repeat(main_model: str, runs: int, raw_root: Path, symbol: str, session_date: str,
               valid_until: str, out_dir: Path,
               force_max_tokens: int | None = None, force_timeout: int | None = None) -> dict:
    llm = _resolve_llm(main_model)
    if force_max_tokens or force_timeout:
        # 只在本进程内覆盖预算: 目的是把"配置不匹配"与"模型能力"分离
        _orig_call = llm._call_llm

        def _budget_forced(system, user, *a, **kw):
            if force_max_tokens:
                kw["max_tokens"] = force_max_tokens
            if force_timeout:
                kw["timeout"] = force_timeout
            return _orig_call(system, user, *a, **kw)

        llm._call_llm = _budget_forced
        print(f"[repeat] budget override: max_tokens={force_max_tokens} timeout={force_timeout}",
              file=sys.stderr)
    from da_event_instruction_replay import run_arm

    raw = json.loads((raw_root / f"US.{symbol}.raw.json").read_text(encoding="utf-8"))
    if raw.get("trade_date") != session_date:
        raise SystemExit(f"session mismatch: raw={raw.get('trade_date')}")
    collected = raw["collected"]
    out_dir.mkdir(parents=True)
    rows = []
    for i in range(runs):
        t0 = time.time()
        out = run_arm(collected, symbol=f"US.{symbol}", session_date=session_date,
                      valid_until=valid_until, brief=None, arm="control")
        card = out.get("card") or {}
        rec = {
            "run": i + 1,
            "direction": card.get("direction"),
            "score": card.get("score"),
            "status": card.get("status"),
            "event_role": card.get("event_role"),
            "validation_errors": card.get("validation_errors"),
            "main_model_requested": main_model,
            "main_model_reported": (out.get("result") or {}).get("_model_reported"),
            "challenger_model_reported": (out.get("challenger_raw") or {}).get("_model_reported"),
            "elapsed_s": round(time.time() - t0, 1),
            "llm_calls": len(out.get("calls") or []),
        }
        # token/截断证据: 换档成本必须可核, 不只看方向
        for idx, call in enumerate(out.get("calls") or []):
            raw_meta = call.get("raw") or {}
            usage = raw_meta.get("_usage") or {}
            rec[f"call{idx}_{call.get('role')}_usage"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            }
            rec[f"call{idx}_finish_reason"] = raw_meta.get("_finish_reason")
            rec[f"call{idx}_error"] = raw_meta.get("_error")
        rows.append(rec)
        (out_dir / f"US.{symbol}.run{i + 1}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8")
        print(f"[repeat] run {i + 1}/{runs} dir={rec['direction']} score={rec['score']} "
              f"status={rec['status']} {rec['elapsed_s']}s model={rec['main_model_reported']}",
              file=sys.stderr)
    dirs = collections.Counter(r["direction"] for r in rows)
    summary = {
        "schema": "da_model_swap_repeat_v1",
        "symbol": f"US.{symbol}", "session_date": session_date, "arm": "control",
        "main_model_requested": main_model,
        "main_model_reported_set": sorted({str(r["main_model_reported"]) for r in rows}),
        "runs": len(rows),
        "direction_counts": dict(dirs),
        "score_values": sorted({r["score"] for r in rows}),
        "status_counts": dict(collections.Counter(r["status"] for r in rows)),
        "elapsed_s_median": sorted(r["elapsed_s"] for r in rows)[len(rows) // 2],
        "production_write": False,
        "rows": rows,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("main_model_requested", "main_model_reported_set",
                                              "runs", "direction_counts", "score_values",
                                              "status_counts", "elapsed_s_median")}, ensure_ascii=False))
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("repeat", "battery"), required=True)
    ap.add_argument("--main-model", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--raw-root", type=Path, default=Path("data/da_checkpoint/2026-11-05"))
    ap.add_argument("--symbol", default="AMD")
    ap.add_argument("--session-date", default="2026-11-05")
    ap.add_argument("--valid-until", default="2026-11-05T16:00:00-05:00")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--force-max-tokens", type=int, default=None)
    ap.add_argument("--force-timeout", type=int, default=None)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)
    if not args.execute:
        raise SystemExit("dry-run 不调用模型; 加 --execute 才实跑")
    if args.output_dir.exists():
        raise SystemExit(f"输出目录已存在, 拒绝覆盖: {args.output_dir}")
    if args.mode == "battery":
        run_battery(args.main_model, args.output_dir)
    else:
        if args.runs < 2:
            raise SystemExit("runs 至少为 2")
        run_repeat(args.main_model, args.runs, args.raw_root, args.symbol.strip().upper(),
                   args.session_date, args.valid_until, args.output_dir,
                   force_max_tokens=args.force_max_tokens, force_timeout=args.force_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())