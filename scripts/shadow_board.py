#!/usr/bin/env python3
"""影子统一看板 (只读, 2026-07-12)。

把散落的影子任务聚合成"一条信息看全局": 每个活影子并列显示 [单日 / 近7天]
(低频影子如 divergence 附近30天)。纯只读、零 live 行为改动; 每段独立 try,
任一段异常不影响其余段 (沿用 edge_weekly_report 的安全模式)。

覆盖 (2026-07-12 活影子):
  1. 分歧仲裁      data/shadow/divergence_arbitration_shadow.jsonl  (反向拦截+pnl+模型)
  2. 技术vsLLM方向 data/shadow/shadow_technical_direction.jsonl     (方向一致率+outcome)
  3. SL/TP影子     复用 sltp_shadow_pnl_report (现无样本, 产出后自动显示)
  4. Edge闸门      复用 edge_weekly_report.build_edge_weekly_summary
  5. 挑战者判决    data/shadow_verdicts.db (verdict 分布, 按 model_used)
不含: playbook_direction_audit (已死), shadow_eod_replay (已废弃)。

策略: 先与现有 sltp日报/edge周报并存, 稳定后再收敛掉分散推送。
运行: PYTHONPATH=. ./venv/bin/python scripts/shadow_board.py [--push]
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ET = timezone(timedelta(hours=-4))
ROOT = Path(__file__).resolve().parents[1]
SHADOW_DIR = ROOT / "data" / "shadow"
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(ROOT))

LONG = {"BUY", "LONG"}
SHORT = {"SELL_SHORT", "SHORT"}


def _today_et():
    return datetime.now(ET).date()


def _within(dstr, days):
    """dstr(YYYY-MM-DD...) 是否落在最近 days 天内 (days=1 → 仅今日)。"""
    try:
        d = date.fromisoformat(str(dstr)[:10])
    except Exception:
        return False
    delta = (_today_et() - d).days
    return 0 <= delta <= days - 1


def _read_jsonl(path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


# ── 段1: 分歧仲裁 ─────────────────────────────────────────────
def _sec_divergence():
    recs = _read_jsonl(SHADOW_DIR / "divergence_arbitration_shadow.jsonl")

    def blk(sub):
        n = len(sub)
        opp = [r for r in sub if r.get("type") == "OPPOSITE"]
        diff = [r for r in sub if r.get("differs")]
        oe = [r for r in opp if r.get("current_executed") and r.get("pnl_pct") is not None]
        s = f"分歧{n} 反向{len(opp)}(→拦) 差异{len(diff)}"
        if oe:
            pv = [r["pnl_pct"] for r in oe]
            wr = sum(1 for v in pv if v > 0) / len(pv) * 100
            s += f" | 已成交反向{len(oe)}笔 pnl均{sum(pv)/len(pv):+.4f} 胜{wr:.0f}%"
        return s

    lines = ["【分歧仲裁 (反向→SKIP / 单方SKIP→跟随)】"]
    lines.append(f"  今日 : {blk([r for r in recs if _within(r.get('date'), 1)])}")
    lines.append(f"  近7天: {blk([r for r in recs if _within(r.get('date'), 7)])}")
    lines.append(f"  近30天:{blk([r for r in recs if _within(r.get('date'), 30)])}")
    return lines


# ── 段2: 技术 vs LLM 方向 ─────────────────────────────────────
def _sec_technical():
    recs = _read_jsonl(SHADOW_DIR / "shadow_technical_direction.jsonl")

    def blk(sub):
        n = len(sub)
        if not n:
            return "无样本"
        agree = sum(1 for r in sub
                    if (r.get("technical_direction") or "").upper() == (r.get("llm_direction") or "").upper())
        oc = Counter(r.get("outcome") for r in sub if r.get("outcome"))
        oc_s = " ".join(f"{k}:{v}" for k, v in oc.most_common(3)) or "outcome未回填"
        return f"n={n} 方向一致{agree}/{n}({agree/n*100:.0f}%) | {oc_s}"

    lines = ["【技术 vs LLM 方向】"]
    lines.append(f"  今日 : {blk([r for r in recs if _within(r.get('timestamp'), 1)])}")
    lines.append(f"  近7天: {blk([r for r in recs if _within(r.get('timestamp'), 7)])}")
    return lines


# ── 段3: SL/TP 影子 (复用 sltp_shadow_pnl_report) ─────────────
def _sec_sltp():
    lines = ["【SL/TP 影子 pnl】"]
    try:
        import sltp_shadow_pnl_report as sl
        rows = sl._load_shadow()
        if not rows:
            lines.append("  暂无样本 (reprice 未触发 / flag 未开) — 产出后自动显示")
            return lines
        ids = [r.get("inst_id") or r.get("id") for r in rows]
        pnl = sl._pnl_by_inst(ids)
        vals = [v["pnl_pct"] for v in pnl.values() if v.get("pnl_pct") is not None]
        lines.append(f"  累计: {sl._fmt(sl._stats(vals))}")
    except Exception as e:
        lines.append(f"  ⚠️ 段异常: {e}")
    return lines


# ── 段4: Edge 闸门 (复用 edge_weekly_report) ──────────────────
def _sec_edge():
    lines = ["【Edge 闸门影子】"]
    try:
        from app.decision.edge_weekly_report import build_edge_weekly_summary
        s = build_edge_weekly_summary(7)
        ar = s.get("agree_rate")
        ar_s = f"{ar*100:.0f}%" if ar is not None else "NA"
        lines.append(f"  近7天: 影子{s.get('shadow_total', 0)} edge拦{s.get('edge_block', 0)} "
                     f"一致率{ar_s} (更严{s.get('edge_stricter', 0)}/更松{s.get('edge_looser', 0)})")
    except Exception as e:
        lines.append(f"  ⚠️ 段异常: {e}")
    return lines


# ── 段5: 挑战者判决 (shadow_verdicts.db) ──────────────────────
def _sec_verdict():
    lines = ["【挑战者判决 (DA 计划埋点)】"]
    try:
        p = ROOT / "data" / "shadow_verdicts.db"
        if not p.exists():
            lines.append("  无库")
            return lines
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        rows = c.execute("SELECT plan_date, verdict, model_used FROM shadow_verdicts").fetchall()
        c.close()

        def blk(days):
            sub = [r for r in rows if _within(r[0], days)]
            if not sub:
                return "无样本"
            vd = Counter(r[1] for r in sub)
            md = Counter(r[2] for r in sub if r[2])
            vd_s = " ".join(f"{k}:{v}" for k, v in vd.most_common())
            md_s = " ".join(f"{k}:{v}" for k, v in md.most_common(2))
            return f"n={len(sub)} [{vd_s}]" + (f" | 模型 {md_s}" if md_s else "")

        lines.append(f"  今日 : {blk(1)}")
        lines.append(f"  近7天: {blk(7)}")
    except Exception as e:
        lines.append(f"  ⚠️ 段异常: {e}")
    return lines


# ── 段6: 盘前存储峰群 (premarket_peer_shadow) ───────────
def _sec_premarket_peer():
    recs = _read_jsonl(SHADOW_DIR / "premarket_peer_shadow.jsonl")
    lines = ["【盘前存储峰群 (MU/SNDK/WDC/STX 同向+放量)】"]
    if not recs:
        lines.append("  暂无样本 (盘前采集未跑) — 产出后自动显示")
        return lines
    today = [r for r in recs if _within(r.get("date"), 1)]
    if today:
        r = today[0]
        lines.append(f"  今日 : {r.get('group_verdict')} (源={r.get('source')})")
        ins = r.get("instructions") or {}
        if ins:
            tag = {True: "一致", False: "背离", None: "—"}
            brief = [f"{s}:{v.get('direction')}/{tag[v.get('agree_with_peer')]}" for s, v in ins.items()]
            lines.append(f"        DA对照 {' '.join(brief)}")
    else:
        lines.append("  今日 : 无采集")

    def stat(days):
        sub = [r for r in recs if _within(r.get("date"), days)]
        conf = sum(1 for r in sub if str(r.get("group_verdict", "")).startswith("CONFIRM_"))
        ap = [i["pnl_pct"] for r in sub for i in (r.get("instructions") or {}).values()
              if i.get("agree_with_peer") is True and i.get("pnl_pct") is not None]
        cp = [i["pnl_pct"] for r in sub for i in (r.get("instructions") or {}).values()
              if i.get("agree_with_peer") is False and i.get("pnl_pct") is not None]
        gh = sum(1 for r in sub for i in (r.get("instructions") or {}).values() if i.get("gate_would_help"))

        def av(x):
            return f"{sum(x)/len(x):+.4f}(n={len(x)})" if x else "无"
        return f"{len(sub)}日 确认{conf} | 一致pnl{av(ap)} 背离pnl{av(cp)} 闸门可避亏{gh}"

    lines.append(f"  近7天: {stat(7)}")
    lines.append(f"  近30天:{stat(30)}")
    return lines


# ── 段7: 退出参数影子 (capture) ───────────────────────────────
def _sec_exit_param():
    """退出参数影子: 更早保本/更紧锁利变体 vs 实盘 capture (2026-07-13)。

    capture=已实现有利%/最大浮盈%; 仅统计 max_fav>0 且有 live.capture 的单。
    只测【比现行更紧】变体 (更松骑过实盘退出点不可观测)。
    """
    import statistics
    recs = _read_jsonl(SHADOW_DIR / "exit_param_shadow.jsonl")
    lines = ["【退出参数影子 (capture: 已实现有利%/最大浮盈%)】"]

    def _med(xs):
        xs = [x for x in xs if x is not None]
        return statistics.median(xs) if xs else None

    def blk(days):
        sub = [r for r in recs if _within(r.get("ts"), days)
               and (r.get("max_fav_pct") or 0) > 0
               and r.get("live", {}).get("capture") is not None]
        if not sub:
            return "无可用样本"
        live_med = _med([r["live"]["capture"] for r in sub])
        names = []
        for r in sub:
            for k in r.get("variants", {}):
                if k not in names:
                    names.append(k)
        parts = [f"n={len(sub)} 实盘cap中位{live_med:+.3f}" if live_med is not None else f"n={len(sub)}"]
        for name in names:
            if name.startswith("baseline"):
                continue
            caps = [r["variants"][name].get("capture") for r in sub if name in r.get("variants", {})]
            ntrig = sum(1 for r in sub if r.get("variants", {}).get(name, {}).get("triggered"))
            cm = _med(caps)
            parts.append(f"{name} cap中位{cm:+.3f}(触发{ntrig})" if cm is not None else f"{name} n/a")
        return " | ".join(parts)

    if not recs:
        lines.append("  暂无样本 (影子刚上线/无平仓单) — 产出后自动显示")
        return lines
    lines.append(f"  近7天: {blk(7)}")
    lines.append(f"  近30天:{blk(30)}")
    return lines


# ── 段8: 选股宇宙扩张影子 (板块轮动→宇宙外异动) ──────────────
def _sec_universe_expansion():
    """板块轮动→自动扩池影子 (2026-07-14)。

    capture=当日轮入板块的宇宙外异动成分; score=事后前瞻收益/命中率。
    验证'看得见能源轮入却打不着'缺口: 扩池能否稳定浮现宇宙外的获利标的。
    """
    recs = _read_jsonl(SHADOW_DIR / "universe_expansion_shadow.jsonl")
    lines = ["【选股宇宙扩张影子 (板块轮动→宇宙外异动)】"]
    if not recs:
        lines.append("  暂无样本 (影子刚上线) — capture 产出后自动显示")
        return lines
    caps = [r for r in recs if r.get("type") == "capture"]
    scs = [r for r in recs if r.get("type") == "score"]

    def blk(days):
        c = [r for r in caps if _within(r.get("date"), days)]
        if not c:
            return "无 capture"
        surf = sum(len(x.get("surfaced", [])) for x in c)
        uniq = len({r["symbol"] for x in c for r in x.get("surfaced", [])})
        s = [r for r in scs if _within(r.get("capture_date"), days)]
        fwd = [z["fwd_pct"] for r in s for z in r.get("results", [])]
        hit = [z["hit"] for r in s for z in r.get("results", [])]
        part = f"capture={len(c)} 浮现{surf}人次/{uniq}只"
        if hit:
            part += f" | 命中率{round(sum(hit)/len(hit),3)} 平均前瞻{round(sum(fwd)/len(fwd),2):+}%"
        else:
            part += " | 未评分(待 score)"
        return part

    lines.append(f"  近7天: {blk(7)}")
    lines.append(f"  近30天:{blk(30)}")
    return lines


def build_board():
    head = [f"🌓 FTSeek-US 影子看板 | {_today_et()} (ET) | 单日 / 近7天", "=" * 46]
    body = []
    for sec in (_sec_divergence, _sec_technical, _sec_sltp, _sec_edge, _sec_verdict, _sec_premarket_peer, _sec_exit_param, _sec_universe_expansion):
        try:
            body += sec() + [""]
        except Exception as e:
            body += [f"⚠️ {sec.__name__} 整段异常: {e}", ""]
    tail = ["注: 只读汇总; 现与 sltp日报/edge周报并存, 稳定后收敛为唯一影子推送。"]
    return "\n".join(head + [""] + body + tail)


def main(push=False):
    msg = build_board()
    print(msg)
    if push:
        try:
            from app.notifier.feishu import send_text
            send_text(msg)
            print("\n[ok] 飞书已推送")
        except Exception as e:
            print(f"\n[warn] 推送失败(不影响看板): {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="影子统一看板 (只读)")
    ap.add_argument("--push", action="store_true", help="推送飞书")
    a = ap.parse_args()
    if a.push:
        from scripts.utils.market_guard import exit_if_not_trading_day
        exit_if_not_trading_day()
    main(push=a.push)
