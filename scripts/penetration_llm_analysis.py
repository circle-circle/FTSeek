#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""穿透影子 LLM 分析环 (2026-09-03, DEB-20260903 议题二后续, 用户裁"A"全量).

周频 (周六早 cron)。闭环纪律 = "LLM 出题, 代码判卷":
  1. 读 penetration_shadow.jsonl, join 事件×结局 → 带标签样本
  2. 确定性基线统计 (by 侧/原因/深度桶 的 V型率) — 不依赖 LLM, 永远可得
  3. n>=8 才调 LLM: 让模型看样本表, 按固定 JSON schema 提【判别假设】
     (conditions: 允许字段 × gt/lt/ge/le/eq × 阈值 → predict V|DOWN)
  3.5 边界校验 (validate_hypotheses): conditions 必须是 [{field,op,value}] 对象数组;
     形状不合法 (如 LLM 写了自然语言字符串) → 整条假设丢弃 + warn, 不让单条脏输出
     把整个周报 (含确定性基线) 打挂 — 2026-10-24 实测 AttributeError 事故
  4. 每个假设立刻用确定性 evaluator 在【全部】带标签样本上判分
     (命中率/precision=V型率/平均cf挽回) — LLM 自说自话不算数
  5. 毕业门控: n_labeled>=20 且某假设 matched>=8 且 precision>=0.7 且命中集内
     少数类 (未V回) >=GRAD_MINORITY=8 (2026-09-23 加; 类别失衡时 precision 门
     在基线上即过, 无鉴别力) → 自动开 DEB 草稿 (立案待裁) + 飞书推送呈用户裁决;
     否则只出观察报告
里程碑提醒: n_labeled 首达 10/20/30 → 飞书一次性推送 (state 文件去重)。

record-only 生态, 不碰任何交易路径。LLM 路由: llm.features.penetration_pattern_analysis。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SHADOW = REPO / "data" / "shadow" / "penetration_shadow.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "penetration_analysis_latest.json"
REPORT_MD = REPO / "data" / "shadow" / "penetration_analysis_report.md"
MILESTONE_STATE = REPO / "data" / "shadow" / "penetration_milestones.json"
DEB_DIR = REPO / "docs" / "debates"

MIN_LLM_SAMPLES = 8        # 低于此数不调 LLM (防止小样本过度解读)
GRAD_N = 20                # DEB 毕业样本门 (总量)
GRAD_MATCHED = 8
GRAD_PRECISION = 0.7
# 2026-09-23 (用户裁「按建议执行」): 少数类下限 — 判别器要能训/能验, 除总量外
#   还必须两类都有足够样本。实测 86 例 = V回84/未回2 (V型率 98%), 此时任何
#   "宽条件"假设都能在基线上拿到 precision≈0.98 ⇒ 只在总量上设门 = 数量误导。
#   未回 (recovered_5m=False) 是"快跌"类, 它不足则判别器无对照臂。
GRAD_MINORITY = 8
MILESTONES = (10, 20, 30)

NUM_FIELDS = ("depth_bps", "mfe_pct", "giveback_bps", "held_minutes",
              "minutes_since_open", "atr_pct", "depth_atr_ratio")
CAT_FIELDS = ("side", "exit_reason", "path", "quote_src", "fresh_extreme")
OPS = ("gt", "lt", "ge", "le", "eq")
_HYP_FIELDS = frozenset(NUM_FIELDS) | frozenset(CAT_FIELDS)

SYSTEM = """你是量化交易研究助理。给定"保本线穿透"事件样本表 (每行=一次穿透触发+离场后真实结局),
提出可判别的假设: 哪类穿透是"快速下跌趋势"(应即走, predict=DOWN), 哪类是"缓慢临时回撤/V型"(持有等回摆更优, predict=V)。
规则: 每个假设 = 1~3 个条件 (字段×比较符×阈值) + predict + rationale(中文,≤50字)。
只允许用这些字段: 数值 depth_bps(穿透深度bps,正=已击穿) mfe_pct giveback_bps held_minutes minutes_since_open atr_pct depth_atr_ratio;
类别 side(active/passive) exit_reason path quote_src fresh_extreme(true/false)。
比较符: gt lt ge le eq。阈值须落在样本可见范围内。至多提4个假设。宁缺毋滥: 样本看不出规律就返回空列表。

输出格式 (conditions 必须是对象数组, 每项恰好 {field, op, value} 三个键; 禁止把条件写成自然语言字符串):
{"hypotheses": [{"conditions": [{"field": "depth_bps", "op": "ge", "value": 30},
                               {"field": "fresh_extreme", "op": "eq", "value": true}],
                 "predict": "DOWN", "rationale": "刷新低且深穿透多为趋势延续"}]}"""

SCHEMA_REQ = {"hypotheses": list}


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_samples():
    """join 事件×结局 → 带标签样本列表。"""
    events, outcomes = {}, {}
    if not SHADOW.exists():
        return []
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        r = _loads(line)
        if not r:
            continue
        eid = r.get("event_id")
        if r.get("type") == "penetration" and eid:
            events[eid] = r
        elif r.get("type") == "outcome" and eid and r.get("status") == "labeled":
            outcomes[eid] = r
    out = []
    for eid, ev in events.items():
        if ev.get("path") == "early_tight_initial":
            continue
        oc = outcomes.get(eid)
        if not oc:
            continue
        row = {k: ev.get(k) for k in (*NUM_FIELDS, *CAT_FIELDS)}
        row.update({"symbol": ev.get("symbol"), "recovered_5m": oc.get("recovered_5m"),
                    "recovered_10m": oc.get("recovered_10m"),
                    "continued_bps_10m": oc.get("continued_bps_10m"),
                    "cf_floor_fill_saved_bps": oc.get("cf_floor_fill_saved_bps")})
        out.append(row)
    return out


def _match(hyp, s):
    """确定性 evaluator: 假设条件是否命中样本 (None 值 → 不命中)。"""
    for c in hyp.get("conditions") or []:
        field, op, val = c.get("field"), c.get("op"), c.get("value")
        sv = s.get(field)
        if sv is None:
            return False
        if field in CAT_FIELDS:
            if op != "eq" or str(sv).lower() != str(val).lower():
                return False
            continue
        try:
            sv, val = float(sv), float(val)
        except (TypeError, ValueError):
            return False
        if op == "gt" and not sv > val: return False
        if op == "lt" and not sv < val: return False
        if op == "ge" and not sv >= val: return False
        if op == "le" and not sv <= val: return False
        if op == "eq" and not sv == val: return False
        if op not in ("gt", "lt", "ge", "le", "eq"): return False
    return True


def _cond_ok(c):
    """单个条件形状校验: 必须恰好是 {field, op, value} 且字段/比较符在白名单内。"""
    if not isinstance(c, dict):
        return False
    field, op, val = c.get("field"), c.get("op"), c.get("value")
    if field not in _HYP_FIELDS or op not in OPS or val is None:
        return False
    if field in CAT_FIELDS:
        return op == "eq"
    try:
        float(val)
    except (TypeError, ValueError):
        return False
    return True


def validate_hypotheses(hyps):
    """LLM 输出边界校验 → (合格假设, 被丢弃假设).

    2026-10-24 实测: schema_required={"hypotheses": list} 只校验顶层类型, 嵌套
    conditions 无约束; LLM 偶尔把条件写成自然语言字符串 (如 "深度>300bps 且刷新低")
    ⇒ _match 的 c.get() 抛 AttributeError ⇒ 整个周报 (含确定性基线) 一并归零。
    纪律: 任一条件形状不合法 → **整条假设丢弃**, 不删单个条件 —— 删条件会放宽命中集,
    在毕业门 (precision/少数类) 里等价于把门变松; 丢弃必须 warn 留证 + 进报告计数。
    """
    good, dropped = [], []
    for h in hyps or []:
        conds = h.get("conditions") if isinstance(h, dict) else None
        if isinstance(conds, list) and conds and all(_cond_ok(c) for c in conds):
            good.append(h)
        else:
            dropped.append(h)
    if dropped:
        print(f"[warn] 丢弃 {len(dropped)}/{len(dropped) + len(good)} 条形状不合法的假设: "
              + "; ".join(str(d)[:160] for d in dropped))
    return good, dropped


def score_hypothesis(hyp, samples):
    matched = [s for s in samples if _match(hyp, s)]
    if not matched:
        return {"matched": 0}
    v = [s for s in matched if s.get("recovered_5m") is True]
    cfs = [s["cf_floor_fill_saved_bps"] for s in matched
           if isinstance(s.get("cf_floor_fill_saved_bps"), (int, float))]
    return {"matched": len(matched),
            "v_count": len(v), "nonv_count": len(matched) - len(v),
            "v_rate": round(len(v) / len(matched), 3),
            "avg_cf_saved_bps": round(sum(cfs) / len(cfs), 1) if cfs else None}


def baseline_stats(samples):
    """确定性基线: 整体 + by 侧/原因 的 V型率。"""
    def agg(rows):
        v = [r for r in rows if r.get("recovered_5m") is True]
        n = len(rows)
        return {"n": n, "v_rate_5m": round(len(v) / n, 3) if n else None}
    by_side, by_reason = {}, {}
    for s in samples:
        by_side.setdefault(str(s.get("side")), []).append(s)
        by_reason.setdefault(str(s.get("exit_reason")), []).append(s)
    return {"overall": agg(samples),
            "by_side": {k: agg(v) for k, v in by_side.items()},
            "by_reason": {k: agg(v) for k, v in by_reason.items()}}


def _push(msg):
    # 2026-10-15 (审计 P2-4): 直连 notifier 是军规旁路 ⇒ hub-first (sync=True 立即送达,
    # cron 进程短命; hub.stop() 收线程池防 cron 挂住); hub 异常 → 回退直连 (不使告警链断在这)。
    try:
        from app.push import get_push_hub
        from app.push.message import Category, Level, PushMessage
        hub = get_push_hub()
        try:
            ok = bool(hub.push(PushMessage(
                title="穿透影子", body=msg, level=Level.NORMAL,
                category=Category.SHADOW_COMPARE), sync=True))
        finally:
            hub.stop()
        if ok:
            return True
    except Exception as e:  # noqa: BLE001 — 兜底路径接管, 不让推送链在此断掉
        print(f"[warn] hub 推送受理失败, 回退直连: {e}")
    try:
        from app.notifier.feishu import send_text
        send_text(msg)
        return True
    except Exception as e:
        print(f"[warn] 飞书推送失败: {e}")
        return False


def check_milestones(n_labeled):
    """n_labeled 首达 10/20/30 → 飞书一次性推送 (state 文件去重)。"""
    fired = []
    try:
        if MILESTONE_STATE.exists():
            fired = json.loads(MILESTONE_STATE.read_text()).get("fired", [])
    except Exception:
        fired = []
    for m in MILESTONES:
        if n_labeled >= m and m not in fired:
            _push(f"📈 穿透影子攒样里程碑: 带标签样本已达 {n_labeled} (≥{m})。"
                  f"{'样本量达总量门 (是否毕业还看少数类是否达标/判据实测)' if m >= GRAD_N else '继续攒样'}。")
            fired.append(m)
    try:
        MILESTONE_STATE.write_text(json.dumps({"fired": fired}, ensure_ascii=False))
    except Exception as e:
        print(f"[warn] milestone state 写入失败: {e}")


def ask_llm(samples):
    """LLM 提假设 (schema 强约束); 失败/样本不足 → None。"""
    if len(samples) < MIN_LLM_SAMPLES:
        print(f"样本 {len(samples)} < {MIN_LLM_SAMPLES}, 跳过 LLM (防小样本过度解读)")
        return None
    try:
        from app.llm.model_call import call_llm
    except Exception as e:
        print(f"[warn] model_call 不可用: {e}")
        return None
    header = ("side exit_reason path depth_bps mfe_pct giveback_bps fresh_extreme "
              "held_minutes minutes_since_open atr_pct depth_atr_ratio | "
              "recovered_5m recovered_10m continued_bps_10m cf_saved_bps")
    lines = [header]
    for s in samples[-60:]:
        lines.append(" ".join(str(s.get(k)) for k in
                              ("side", "exit_reason", "path", "depth_bps", "mfe_pct",
                               "giveback_bps", "fresh_extreme", "held_minutes",
                               "minutes_since_open", "atr_pct", "depth_atr_ratio"))
                     + " | " + " ".join(str(s.get(k)) for k in
                                        ("recovered_5m", "recovered_10m",
                                         "continued_bps_10m", "cf_floor_fill_saved_bps")))
    prompt = ("以下是保本线穿透事件的采集特征与离场后真实结局 (recovered_5m=true=V型回摆)。\n"
              "请按 system 规则提出判别假设 (JSON: {\"hypotheses\": [...]}):\n\n" + "\n".join(lines))
    res = call_llm(feature="penetration_pattern_analysis", prompt=prompt, system=SYSTEM,
                   temperature=0.2, max_tokens=2000, json_mode=True,
                   schema_required=SCHEMA_REQ)
    if not getattr(res, "success", False):
        print(f"[warn] LLM 调用失败: {getattr(res, 'error', '?')}")
        return None
    resp = res.response
    if not isinstance(resp, dict):
        print(f"[warn] LLM 响应非 dict: {type(resp)}")
        return None
    hyps = resp.get("hypotheses")
    return hyps if isinstance(hyps, list) else None


def maybe_open_deb_draft(scored, samples):
    """毕业门控: 总量 n>=GRAD_N 且最佳假设过门 (matched/精度/少数类) → DEB 草稿
    (立案待裁) + 推送。2026-09-23: 加 GRAD_MINORITY 少数类下限 — 样本总量达门但
    类别失衡 (实测 84:2) 时 precision 门在基线上即过, 判据无鉴别力, 不开草稿。"""
    if len(samples) < GRAD_N:
        return None
    cands = [h for h in scored
             if h.get("matched", 0) >= GRAD_MATCHED
             and (h.get("v_rate") or 0) >= GRAD_PRECISION
             and (h.get("nonv_count") or 0) >= GRAD_MINORITY]
    if not cands:
        return None
    best = max(cands, key=lambda h: (h.get("v_rate") or 0) * h["matched"])
    today = datetime.now().strftime("%Y%m%d")
    path = DEB_DIR / f"DEB-{today}-penetration-discriminator.md"
    if path.exists():
        return str(path)
    body = f"""# DEB-{today}-penetration-discriminator — 穿透性质判别器毕业评审 (自动草稿)

状态: **立案待裁** (由 penetration_llm_analysis.py 毕业门控自动生成, 待用户裁决)
样本: 带标签 {len(samples)} 条 (总量门 ≥{GRAD_N}) | 未V回(快跌类)
{sum(1 for s in samples if s.get("recovered_5m") is not True)} 条 (少数类门 ≥{GRAD_MINORITY})

## 过门假设 (LLM 提出, 确定性 replay 判分)

```json
{json.dumps(best, ensure_ascii=False, indent=1)}
```

## 待裁决议题

是否将上列假设实单化为"高置信V型穿透 → 延迟一个确认周期再挂 marketable limit"
第三执行路径 (阶段2)。实单化前需: ① 用户裁决 ② KR2 版本快照 ③ 回滚开关。

## 全部候选判分

```json
{json.dumps(scored, ensure_ascii=False, indent=1)}
```
"""
    path.write_text(body, encoding="utf-8")
    _push(f"🎓 穿透判别器毕业门触发: 最佳假设 v_rate={best.get('v_rate')} "
          f"matched={best.get('matched')} 少数类={best.get('nonv_count')} → "
          f"DEB 草稿 {path.name} 已立案待裁")
    return str(path)


def main():
    samples = load_samples()
    n = len(samples)
    print(f"带标签样本: {n}")
    check_milestones(n)
    stats = baseline_stats(samples)
    scored = []
    hyps = ask_llm(samples)
    hyps_ok, hyps_dropped = validate_hypotheses(hyps or [])
    for h in hyps_ok[:4]:
        scored.append({**h, **score_hypothesis(h, samples)})
    deb = maybe_open_deb_draft(scored, samples)
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "n_labeled": n, "baseline": stats,
              "llm_called": hyps is not None, "hypotheses_scored": scored,
              "hypotheses_dropped": len(hyps_dropped),
              "dropped_samples": [str(d)[:200] for d in hyps_dropped[:4]],
              "deb_draft": deb,
              "graduation_gate": {"n": GRAD_N, "matched": GRAD_MATCHED,
                                  "precision": GRAD_PRECISION,
                                  "minority": GRAD_MINORITY}}
    OUT_LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    md = [f"# 穿透影子分析报告 ({report['generated_at']})", "",
          f"带标签样本: **{n}** | LLM已调: {hyps is not None} | "
          f"丢弃形状不合法假设: {len(hyps_dropped)} | DEB草稿: {deb or '无'}", "",
          "## 确定性基线", "```json", json.dumps(stats, ensure_ascii=False, indent=1), "```",
          "", "## 假设判分 (LLM出题, 代码判卷)", "```json",
          json.dumps(scored, ensure_ascii=False, indent=1), "```"]
    REPORT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"n_labeled": n, "baseline_overall": stats["overall"],
                      "n_hypotheses": len(scored),
                      "n_dropped": len(hyps_dropped), "deb_draft": deb},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
