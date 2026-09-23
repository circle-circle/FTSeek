#!/usr/bin/env python3
"""每日 LLM token / 成本消耗统计推送 (2026-07-23)。

回答: 昨日各 LLM 任务 (feature) 与各模型烧了多少 token / 多少钱 / 调了多少次 /
      成功率与延迟如何 —— 供判断"哪个任务该优化 prompt / 换更便宜模型 / 关闭"。

数据源: data/llm_audit.db 表 llm_calls (SSOT; 由 app/llm/model_call.py 每次调用落库)。
  注: 2026-07-23 修复了 _record_audit 参数名 bug (cost/error → cost_usd/error_msg),
      此前审计写入被静默吞掉, token 全为 0; 修复后数据方才有效。

设计:
  - 只读 DB, 零 live 行为变更。
  - 盘后统计前一日 (LLM 调用跨 ET/CST 午夜, 用 --days-back 对齐)。
  - 落盘 data/llm_token_daily_YYYY-MM-DD.json (单日档) 供离线复盘。
  - 失败安全: DB / 推送异常都吞掉, 绝不抛错阻断 cron。
运行: PYTHONPATH=. ./venv/bin/python scripts/daily_llm_token_report.py [--date YYYY-MM-DD] [--push] [--dry]
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "llm_audit.db"

# 自含 dotenv 加载: 飞书 webhook 在 .env.us, 保证 cron 下也能拿到
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass


def _fmt_usd(v: float) -> str:
    if v >= 1:
        return f"${v:.2f}"
    if v >= 0.01:
        return f"${v:.3f}"
    return f"${v*1000:.2f}m"  # 毫美元, 避免一堆 $0.000


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _llm_model_costs() -> dict:
    """yaml llm.models → {model_name: (cost_in, cost_out)} ($/1k tok). 失败返回空."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
        out = {}
        for _alias, m in ((cfg or {}).get("llm", {}).get("models", {}) or {}).items():
            name = (m or {}).get("model_name", "")
            if name:
                out[name] = (float(m.get("cost_in", 0) or 0), float(m.get("cost_out", 0) or 0))
        return out
    except Exception:
        return {}


def _query_da_direct(day: str) -> dict:
    """DA skill 直连调用汇总 (不经网关, 不在 llm_audit) — 读 da_checkpoint/{day}/*.json.

    背景 (2026-09-01 PE 审阅): DA 是全链最大 LLM 消费方 (分析师+挑战者 ~14 次/日),
    但 skill 用 urllib 直连 DashScope, 调用不落 llm_audit.db → 本日报此前完全看不到
    DA 成本 (8/25 P1 已把 _usage 落 checkpoint, 此函数完成最后一公里归集)。
    口径: 分析师侧 token/成本来自 checkpoint _llm_output._usage (含缓存命中);
    挑战者 token 自 2026-09-09 (F1) 起落 checkpoint _debate.challenger_usage,
    有则计价归集 (ch_*), 无则维持只计次数的旧行为。只读, 失败安全 (异常返回空段)。"""
    out = {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0, "cached": 0,
           "cost": 0.0, "trunc": 0, "challenger_calls": 0,
           "ch_tok_in": 0, "ch_tok_out": 0, "ch_cost": 0.0}
    try:
        d = DATA / "da_checkpoint" / day
        if not d.is_dir():
            return out
        costs = _llm_model_costs()
        for f in sorted(d.glob("*.json")):
            if f.name.endswith(".raw.json"):
                continue  # 生料档 (da_raw_inputs/v1), 非分析产出
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                # R5 留痕: 单文件损坏跳过, 但必须可查 (棘轮纪律, 禁静默吞)
                print(f"[warn] DA checkpoint 解析失败 {f.name}: {e}", file=sys.stderr)
                continue
            lo = doc.get("_llm_output") or {}
            if not isinstance(lo, dict) or not lo:
                continue
            u = lo.get("_usage") or {}
            if not u:
                continue
            ti = int(u.get("prompt_tokens", 0) or 0)
            to = int(u.get("completion_tokens", 0) or 0)
            cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
            model = str(lo.get("_model_reported") or "qwen3.7-max")
            cin, cout = costs.get(model, (0.0, 0.0))
            out["calls"] += 1
            out["ok"] += 1  # 有 checkpoint = 该候选分析成功
            out["tok_in"] += ti
            out["tok_out"] += to
            out["cached"] += cached
            out["cost"] += ti / 1000.0 * cin + to / 1000.0 * cout
            if str(lo.get("_finish_reason") or "") == "length":
                out["trunc"] += 1
            deb = lo.get("_debate") or {}
            if deb.get("challenger_verdict"):
                out["challenger_calls"] += 1
                cu = deb.get("challenger_usage") or {}
                if isinstance(cu, dict) and cu:
                    cti = int(cu.get("prompt_tokens", 0) or 0)
                    cto = int(cu.get("completion_tokens", 0) or 0)
                    cmodel = str(deb.get("challenger_model") or "")
                    # 日期快照名归一: deepseek-v4-pro-0813 → deepseek-v4-pro
                    # (yaml 价目以基名为键, 8/27 裁决固化快照后原名在价目表查无)
                    cc = costs.get(cmodel) or costs.get(cmodel.rsplit("-", 1)[0],
                                                        (0.0, 0.0))
                    out["ch_tok_in"] += cti
                    out["ch_tok_out"] += cto
                    out["ch_cost"] += cti / 1000.0 * cc[0] + cto / 1000.0 * cc[1]
    except Exception as e:
        # R5 留痕: 失败安全 (本段置空不阻断日报), 但 warn 留痕禁静默吞
        print(f"[warn] DA 直连段归集异常, 本段按空输出: {e}", file=sys.stderr)
        return {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0, "cached": 0,
                "cost": 0.0, "trunc": 0, "challenger_calls": 0,
                "ch_tok_in": 0, "ch_tok_out": 0, "ch_cost": 0.0}
    return out


def _query_shadow(day: str) -> dict:
    """影子/实验消耗归集 (2026-09-09 F2): 在跑影子实验的 jsonl → {label: {calls, tok_in, tok_out, cost}}.

    背景: 9/8 复盘发现日报低估真实消耗 >50% — 蒸馏影子 ($1.32/日) / glm 影子
    ($0.37/日) / thinking_eval ($0.30/日) 均不在网关也不归 DA 直连段。
    口径: 蒸馏影子只计 v2 增量 (v1 是生产 checkpoint 复述, 非新消耗);
    glm 影子 9/9 起有 glm_tokens_in (此前行无, 按 0 计 → 成本偏低估, 留痕不补);
    thinking_eval off/on 两臂都计。只读, 失败安全 (单文件异常跳过并 stderr 留痕)。"""
    out: dict = {}
    costs = _llm_model_costs()

    def _add(label: str, tin: int, tout: int, model: str) -> None:
        d = out.setdefault(label, {"calls": 0, "tok_in": 0, "tok_out": 0, "cost": 0.0})
        cin, cout = costs.get(model, (0.0, 0.0))
        d["calls"] += 1
        d["tok_in"] += tin
        d["tok_out"] += tout
        d["cost"] += tin / 1000.0 * cin + tout / 1000.0 * cout

    def _rows(fname: str):
        try:
            p = DATA / "shadow" / fname
            if not p.exists():
                return
            n_bad = 0
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception as e:
                    n_bad += 1
                    if n_bad <= 3:  # R5 留痕: 坏行不静默吞, 封顶防爆屏
                        print(f"[warn] 影子段坏行跳过 {fname}: {e}", file=sys.stderr)
                    continue
                if str(rec.get("date") or "") == day:
                    yield rec
        except Exception as e:
            print(f"[warn] 影子段读取失败 {fname}: {e}", file=sys.stderr)

    for rec in _rows("da_prompt_distill_shadow.jsonl"):
        _add("distill_v2增量", int(rec.get("v2_tokens_in") or 0),
             int(rec.get("v2_tokens_out") or 0), str(rec.get("model") or "qwen3.7-max"))
    for rec in _rows("da_glm_shadow.jsonl"):
        _add("da_glm_shadow", int(rec.get("glm_tokens_in") or 0),
             int(rec.get("glm_tokens_out") or 0), str(rec.get("glm_model") or "glm-5.2"))
    for rec in _rows("thinking_eval.jsonl"):
        for arm in ("off", "on"):
            a = rec.get(arm) or {}
            if not isinstance(a, dict) or a.get("tokens_out") is None:
                continue
            _add(f"thinking_eval_{arm}", int(a.get("tokens_in") or 0),
                 int(a.get("tokens_out") or 0),
                 str(a.get("model") or rec.get("model") or "glm-5.2"))
    return out


def _load_budget() -> dict:
    """读 yaml llm_budget 配置 (daily_max_usd / warn_pct / halt_pct). 失败返回默认."""
    default = {"daily_max_usd": 5.0, "warn_pct": 0.7, "halt_pct": 1.0}
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
        b = (cfg or {}).get("llm_budget", {}) or {}
        alerts = b.get("cost_alerts", {}) or {}
        return {
            "daily_max_usd": float(b.get("daily_max_usd", default["daily_max_usd"])),
            "warn_pct": float(alerts.get("warn_pct", default["warn_pct"])),
            "halt_pct": float(alerts.get("halt_pct", default["halt_pct"])),
        }
    except Exception:
        return default


def _query_day(conn: sqlite3.Connection, day: str) -> dict:
    """聚合某一自然日 (localtime) 的 LLM 调用. 返回 {total, by_feature, by_model}.

    仅生产流量: 排除 is_test=1 测试行 (2026-08-27 测试流量隔离)."""
    where = "date(timestamp,'unixepoch','localtime')=? AND COALESCE(is_test,0)=0"
    rows = conn.execute(
        f"SELECT feature, model, tokens_in, tokens_out, cost_usd, latency_ms, success, error_msg "
        f"FROM llm_calls WHERE {where}",
        (day,),
    ).fetchall()

    total = {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0, "cost": 0.0, "lat_sum": 0.0, "trunc": 0}
    by_feature = defaultdict(lambda: {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0,
                                      "cost": 0.0, "lat_sum": 0.0, "trunc": 0})
    by_model = defaultdict(lambda: {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0, "cost": 0.0, "trunc": 0})

    for feat, model, ti, to, cost, lat, ok, err in rows:
        ti = int(ti or 0); to = int(to or 0); cost = float(cost or 0)
        lat = float(lat or 0); ok = int(ok or 0)
        # 2026-08-01 (failure DEB Q4 验收): 截断可查询 — B① 落审计的 error_msg 标记
        trunc = 1 if "finish_reason=length" in (err or "") else 0
        for bucket in (total, by_feature[feat or "?"], by_model[model or "?"]):
            bucket["calls"] += 1
            bucket["ok"] += ok
            bucket["tok_in"] += ti
            bucket["tok_out"] += to
            bucket["cost"] += cost
            bucket["trunc"] += trunc
        total["lat_sum"] += lat
        by_feature[feat or "?"]["lat_sum"] += lat

    return {
        "date": day,
        "total": total,
        "by_feature": {k: dict(v) for k, v in by_feature.items()},
        "by_model": {k: dict(v) for k, v in by_model.items()},
    }


def _query_trunc_7d(conn: sqlite3.Connection, day: str) -> list:
    """7 日窗口 per-feature 截断率 (failure DEB Q4 预注册验收: >5% 且 n≥10 → max_tokens 复核)."""
    rows = conn.execute(
        "SELECT feature, COUNT(*), SUM(CASE WHEN error_msg LIKE '%finish_reason=length%' THEN 1 ELSE 0 END) "
        "FROM llm_calls WHERE date(timestamp,'unixepoch','localtime') BETWEEN date(?, '-6 days') AND date(?) "
        "AND COALESCE(is_test,0)=0 "
        "GROUP BY feature",
        (day, day),
    ).fetchall()
    out = []
    for feat, n, tr in rows:
        n = int(n or 0); tr = int(tr or 0)
        if n >= 10 and tr > 0:
            out.append({"feature": feat or "?", "calls": n, "trunc": tr, "rate": tr / n})
    return sorted(out, key=lambda x: x["rate"], reverse=True)


# 2026-09-24 (RFC-105): UE 思考预算**常驻**读数。原一次性 job `96c8e404f55a`
#   (载体 ops/hermes/ue_truncation_readout.py, 9/18 21:45 跑完) `delivery_outcome=failed`
#   且 repeat=1 已消耗 ⇒ 那次「改善/未改善」结论从未送达 (executions.db 实证), 验证链断。
#   用户裁「并入每日 LLM 日报常驻一行, 不再依赖一次性 job」。
#   ⚠ 判据与锚点与那次预注册**逐字相同** (改判据＝改尺子须用户裁):
#     锚 = `reasoning_effort: high` 落库时刻 (commit 982895f21, CST 2026-09-18 15:51) 的保守整点;
#     判定 n<3 样本不足 / 触顶率 <1/3 改善 / ≥1/2 未改善 / 其余临界;
#     改前基线 = CST 9/15–9/18 15:51 glm-5.3 主调用 6/9 触顶 (llm_audit id 11686/11719/11721/
#     11751/11779/11782 — 其中 11779/11782 为 24000 上限时代, 即「抬上限」单独无效).
#   ⚠ created_at 是 **UTC** (CST-8): 该锚在 CST 口径 ≈ 9/19 00:00, 比修复时刻晚 8h ⇒ 偏保守
#     (漏计 9/18 20:01/21:10 两次修复后干净调用), 与 ue_truncation_readout.py 同源不漂移
#     (守卫: tests/test_daily_llm_token_report_ue_budget.py::test_anchor_matches_standalone_readout)。
UE_BUDGET_ANCHOR = "2026-09-18 16:00:00"
UE_FEATURE = "universe_expansion_reasoning"
UE_BUDGET_BASELINE = "改前基线 6/9=67%"


def _query_ue_budget(conn: sqlite3.Connection) -> dict:
    """UE 修复后**累计**读数 (特征唯一大额 LLM 消费方, glm-5.3 强制思考).

    口径 (2026-09-24, RFC-105):
      · primary = glm 主调用; `downshift_retry` 行是同一轮的重试臂 ⇒ 单独计数不入分母
        (否则一次触顶被记两次, 7 日截断率虚增 — 实测 5 行只对应 3 次事件);
      · 兜底行 (非 glm 模型) 单独计数 —— 主模型触顶/失败后由 fallback 作答, 对下游不可见;
      · 触顶 = error_msg 含 finish_reason=length; 延迟取主调用中位数 (触顶行延迟含烧穿预算, 更慢).
    """
    rows = conn.execute(
        "SELECT model, tokens_out, latency_ms, success, COALESCE(error_msg,'') err "
        "FROM llm_calls WHERE feature=? AND COALESCE(is_test,0)=0 AND created_at >= ?",
        (UE_FEATURE, UE_BUDGET_ANCHOR),
    ).fetchall()
    b = {"n": 0, "trunc": 0, "fail": 0, "max_out": 0, "retry": 0, "fallback": 0,
         "lat": [], "models": set()}
    for model, tok, lat, ok, err in rows:
        model = model or "?"
        if "downshift_retry" in err:
            b["retry"] += 1
            continue
        if "glm" not in model:
            b["fallback"] += 1
            continue
        b["n"] += 1
        b["models"].add(model)
        b["trunc"] += 1 if "finish_reason=length" in err else 0
        b["fail"] += 1 if not ok else 0
        b["max_out"] = max(b["max_out"], int(tok or 0))
        if ok and lat:
            b["lat"].append(float(lat) / 1000.0)
    b["lat"].sort()
    b["p50_lat"] = b["lat"][len(b["lat"]) // 2] if b["lat"] else 0.0
    b["models"] = sorted(b["models"])
    b["rate"] = (b["trunc"] / b["n"]) if b["n"] else None
    if b["n"] < 3:
        b["verdict"] = "样本不足"
    elif b["rate"] < 1 / 3:
        b["verdict"] = "改善"
    elif b["rate"] >= 1 / 2:
        b["verdict"] = "未改善/证伪"
    else:
        b["verdict"] = "临界"
    return b


def _build_message(today: dict, prev: dict, trunc_7d: list | None = None,
                   da_direct: dict | None = None, da_prev: dict | None = None,
                   shadow: dict | None = None, shadow_prev: dict | None = None,
                   ue_budget: dict | None = None) -> str:
    t = today["total"]
    calls = t["calls"]
    da = da_direct or {}
    # 2026-09-01 (PE 审阅): 全链路口径 = 网关 + DA 直连 — 此前 DA (全链最大 LLM
    #   消费方) 完全不在日报内, 预算告警/环比皆失真。
    # 2026-09-09 (F1+F2): 口径再扩 — DA 侧挑战者 (F1 起 token 落 checkpoint) 与
    #   影子/实验 (F2, 独立 jsonl 归集) 计入 total/预算/环比; 9/8 复盘发现旧口径
    #   低估真实消耗 >50% ($1.68 报表 vs ~$3.8 真实)。
    da_analyst_cost = float(da.get("cost", 0) or 0)
    ch_cost = float(da.get("ch_cost", 0) or 0)
    da_total = da_analyst_cost + ch_cost
    shadow_cost = sum(float(s.get("cost", 0) or 0) for s in (shadow or {}).values())
    full_cost = t["cost"] + da_total + shadow_cost
    if calls == 0 and not da.get("calls") and not shadow:
        return f"🪙 LLM 消耗日报 {today['date']}\n(当日无 LLM 调用记录)"

    tok_total = t["tok_in"] + t["tok_out"]
    ok_rate = (t["ok"] / calls * 100) if calls else 0
    avg_lat = (t["lat_sum"] / calls / 1000) if calls else 0  # 秒

    # 环比昨日成本 (扩展口径: 网关 + DA 直连 + 影子/实验)
    pv = ((prev["total"]["cost"] if prev else 0)
          + float((da_prev or {}).get("cost", 0) or 0)
          + float((da_prev or {}).get("ch_cost", 0) or 0)
          + sum(float(s.get("cost", 0) or 0) for s in (shadow_prev or {}).values()))
    delta = ""
    if pv > 0:
        pct = (full_cost - pv) / pv * 100
        arrow = "▲" if pct >= 0 else "▼"
        delta = f" ({arrow}{abs(pct):.0f}% vs 昨, 扩展口径)"

    # 预算告警 (只提示不拦截): 当日成本占 daily_max_usd 的比例
    budget = _load_budget()
    cap = budget["daily_max_usd"]
    alert_line = None
    if cap > 0:
        used_pct = full_cost / cap
        if used_pct >= budget["halt_pct"]:
            alert_line = f"🔴 超预算! 当日 {_fmt_usd(full_cost)} / 上限 {_fmt_usd(cap)} ({used_pct*100:.0f}%)"
        elif used_pct >= budget["warn_pct"]:
            alert_line = f"🟡 接近预算 当日 {_fmt_usd(full_cost)} / 上限 {_fmt_usd(cap)} ({used_pct*100:.0f}%)"

    lines = []
    if alert_line:
        lines.append(alert_line)
    _total_line = f"总计: {calls} 次调用 | {_fmt_tok(tok_total)} tok | {_fmt_usd(full_cost)}{delta}"
    _extras = []
    if da.get("calls"):
        _extras.append(f"DA 直连 {da['calls']} 次 {_fmt_usd(da_total)}")
    if shadow_cost > 0:
        _extras.append(f"影子/实验 {_fmt_usd(shadow_cost)}")
    if _extras:
        _total_line += " (含 " + "; ".join(_extras) + ")"
    lines += [
        f"🪙 LLM 消耗日报 {today['date']}",
        _total_line,
        f"成功率 {ok_rate:.0f}% | 均延迟 {avg_lat:.1f}s (网关口径)",
        "",
        "── 按任务 (Top 成本) ──",
    ]

    feats = sorted(today["by_feature"].items(), key=lambda kv: kv[1]["cost"], reverse=True)
    for feat, s in feats[:10]:
        tok = s["tok_in"] + s["tok_out"]
        okr = (s["ok"] / s["calls"] * 100) if s["calls"] else 0
        flag = " ⚠️" if okr < 80 else ""
        trunc_s = f" 截断{s['trunc']/s['calls']*100:.0f}%" if s["calls"] and s["trunc"] else ""
        lines.append(f"{feat}: {s['calls']}次 {_fmt_tok(tok)}tok {_fmt_usd(s['cost'])} 成功{okr:.0f}%{trunc_s}{flag}")

    lines.append("")
    lines.append("── 按模型 ──")
    models = sorted(today["by_model"].items(), key=lambda kv: kv[1]["cost"], reverse=True)
    for model, s in models:
        tok = s["tok_in"] + s["tok_out"]
        lines.append(f"{model}: {s['calls']}次 {_fmt_tok(tok)}tok {_fmt_usd(s['cost'])}")

    if da.get("calls"):
        lines.append("")
        lines.append("── DA 直连 (skill 直连 DashScope, 不经网关) ──")
        _da_tok = int(da.get("tok_in", 0)) + int(da.get("tok_out", 0))
        _cache_s = f" 缓存命中{_fmt_tok(int(da.get('cached', 0)))}tok" if da.get("cached") else ""
        lines.append(f"分析师: {da['calls']}次 {_fmt_tok(_da_tok)}tok {_fmt_usd(da_analyst_cost)}{_cache_s} 截断{da.get('trunc', 0)}")
        if da.get("challenger_calls"):
            if da.get("ch_tok_in") or da.get("ch_tok_out"):
                _ch_tok = int(da.get("ch_tok_in", 0)) + int(da.get("ch_tok_out", 0))
                lines.append(f"挑战者: {da['challenger_calls']}次 {_fmt_tok(_ch_tok)}tok {_fmt_usd(ch_cost)}")
            else:
                lines.append(f"挑战者: {da['challenger_calls']}次 (token 不落 checkpoint, 成本未含)")

    if shadow:
        lines.append("")
        lines.append("── 影子/实验 (独立 jsonl 归集, 已计入总计与预算口径) ──")
        for label, s in sorted(shadow.items(), key=lambda kv: kv[1]["cost"], reverse=True):
            _stok = int(s["tok_in"]) + int(s["tok_out"])
            lines.append(f"{label}: {s['calls']}次 {_fmt_tok(_stok)}tok {_fmt_usd(s['cost'])}")

    if trunc_7d:
        lines.append("")
        lines.append("── 截断率 (近7日, finish_reason=length) ──")
        for r in trunc_7d:
            mark = " 🔴>5% 复核 max_tokens" if r["rate"] > 0.05 else ""
            lines.append(f"{r['feature']}: {r['trunc']}/{r['calls']} ({r['rate']*100:.1f}%){mark}")

    # 2026-09-24 (RFC-105): 常驻一行 — 结论由本行实数推导, 不写死方向; 均值/率旁边永远带 n。
    if ue_budget is not None:
        lines.append("")
        lines.append("── UE 思考预算 (glm-5.3 强制思考, 修复锚 9/18 起累计) ──")
        ub = ue_budget
        if ub["n"] == 0 and ub["fallback"] == 0 and ub["retry"] == 0:
            lines.append("修复锚后无生产调用 — 扫描未跑或全为测试流量 (判定: 样本不足)")
        else:
            _rate = f"{ub['rate']*100:.0f}%" if ub["rate"] is not None else "n/a"
            lines.append(
                f"主调用 {ub['n']} 次 | 触顶 {ub['trunc']} ({_rate}) | 最大 out={ub['max_out']} | "
                f"中位延迟 {ub['p50_lat']:.0f}s | 失败 {ub['fail']} | 兜底 {ub['fallback']} 次 | "
                f"降档重试 {ub['retry']} 次 → 判定: {ub['verdict']} ({UE_BUDGET_BASELINE})")

    return "\n".join(lines)


def main(date=None, days_back=1, push=False, dry=False):
    if not DB_PATH.exists():
        print(f"[warn] 审计库不存在: {DB_PATH}")
        return

    if date:
        target = date
    else:
        target = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    prev_day = (datetime.strptime(target, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        today = _query_day(conn, target)
        prev = _query_day(conn, prev_day)
        trunc_7d = _query_trunc_7d(conn, target)
        ue_budget = _query_ue_budget(conn)   # [RFC-105] UE 思考预算常驻读数 (累计, 非当日)
    finally:
        conn.close()

    # DA 直连段 (不经网关, 独立归集; 前一日同口径供环比)
    da_direct = _query_da_direct(target)
    da_prev = _query_da_direct(prev_day)
    # 影子/实验段 (F2, 独立 jsonl 归集; 前一日同口径供环比)
    shadow = _query_shadow(target)
    shadow_prev = _query_shadow(prev_day)

    msg = _build_message(today, prev, trunc_7d, da_direct, da_prev, shadow, shadow_prev,
                         ue_budget)
    print("=" * 60)
    print(msg)
    print("=" * 60)

    # 落盘单日档
    try:
        today["da_direct"] = da_direct
        today["shadow"] = shadow
        today["ue_budget"] = ue_budget
        out = DATA / f"llm_token_daily_{target}.json"
        out.write_text(json.dumps(today, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {out}")
    except Exception as e:
        print(f"[warn] 落盘失败: {e}")

    if push and not dry:
        try:
            from app.notifier.feishu import send_text
            send_text(msg)
            print("[pushed] 飞书")
        except Exception as e:
            print(f"[warn] 推送失败: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="每日 LLM token/成本统计推送 (只读)")
    ap.add_argument("--date", help="指定统计日 YYYY-MM-DD (默认: 今日往前 days-back 天)")
    ap.add_argument("--days-back", type=int, default=1, help="盘后统计前 N 日 (默认1)")
    ap.add_argument("--push", action="store_true", help="推送飞书")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    a = ap.parse_args()
    main(date=a.date, days_back=a.days_back, push=a.push, dry=a.dry)
