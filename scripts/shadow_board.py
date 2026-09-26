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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.utils.timezone import ET

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
# ── 段1 已摘除 (2026-09-12, 体检「生产者死消费者仍读」批, 用户裁「按建议执行」): ──
#   _sec_divergence (分歧仲裁) 上游=DA 3模型投票制 debate_outcome, 投票制已废 →
#   cron 每日重算 30 天窗恒 0 候选 (末行 7/17, 累计 40 行), 看板恒显「分歧0」假绿灯。
#   cron 已注释停行, jsonl 留档 data/shadow/archive/ 不删 (同段2/段5先例)。
#   若未来恢复投票制再重新挂段。


# ── 段2/段5 已摘除 (2026-08-26 用户批准): ──
#   _sec_technical (技术vsLLM) 与 _sec_verdict (挑战者判决) 的上游是 3 模型投票制,
#   投票制已废 → 产出停更在 7/11, 每天空转显示"无样本"。jsonl/db 留档不删,
#   消费方零影响。若未来恢复投票制再重新挂段。


# ── 段3: SL/TP 影子 (= LLM×代码 SL/TP 分歧校准; pnl 只是关联结果) ──
def _sec_sltp():
    """2026-09-23 (用户裁「按建议执行」): 补印判据行 sl_agree/tp_agree/|分歧|。

    只印 pnl 看不出本影子在测什么 (实测 mean≈0/win51% 读不出结论); 本意是
    "LLM 给的 SL/TP 与代码波动率分桶算出的差多少" — 一致率与 |分歧| 中位是判据,
    pnl 是按分歧分组后的关联结果。缺了判据行, 段内只剩一个近乎无信号的均值。
    口径: 一致率在【全量影子行】上算 (reprice 时刻属性, 与 pnl join 无关)。
    """
    import statistics
    lines = ["【SL/TP 影子 (本意: LLM×代码 SL/TP 分歧校准; pnl=关联结果)】"]
    try:
        import sltp_shadow_pnl_report as sl
        rows = sl._load_shadow()
        if not rows:
            lines.append("  暂无样本 (reprice 未触发 / flag 未开) — 产出后自动显示")
            return lines
        _parts = []
        for _lbl, _agf, _dvf in (("SL", "sl_agree", "sl_div_pct"),
                                 ("TP", "tp_agree", "tp_div_pct")):
            _ag = [r for r in rows if r.get(_agf) is not None]
            _dv = [r[_dvf] for r in rows if r.get(_dvf) is not None]
            if not _ag:
                continue
            _seg = (f"{_lbl} 一致{100*sum(1 for r in _ag if r[_agf])/len(_ag):.0f}%"
                    f"(n={len(_ag)})")
            if _dv:
                _seg += f" 分歧中位{statistics.median(_dv):.2f}%"
            _parts.append(_seg)
        if _parts:
            lines.append("  判据: " + " | ".join(_parts))
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


# ── 段5 已摘除 (见上方注释, 上游投票制已废) ──


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
# 2026-09-04 (M1): 同步加净口径 — 实盘/变体的净实现中位 (毛实现 - 往返费)。
#   费率参考: 9/3 实测 fees 63.56/15笔, 均名义~5.1k ≈ 8bps (fee_model 同源量级);
#   毛口径会把费用线边缘的单误判为有效改善, 净口径是裁决口径。
_EXIT_FEE_PCT = 0.08


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
        # 净差均值 (bps): 变体-实盘逐笔配对 (双边同费相消), 全员应用期望口径
        live_real = {id(r): r["live"].get("realized_pct") for r in sub}
        live_net_med = _med([p - _EXIT_FEE_PCT for p in live_real.values() if p is not None])
        names = []
        for r in sub:
            for k in r.get("variants", {}):
                if k not in names:
                    names.append(k)
        head = (f"n={len(sub)} 实盘cap中位{live_med:+.3f}"
                + (f" 净实现中位{live_net_med:+.2f}%" if live_net_med is not None else ""))
        parts = [head]
        for name in names:
            if name.startswith("baseline"):
                continue
            caps = [r["variants"][name].get("capture") for r in sub if name in r.get("variants", {})]
            ntrig = sum(1 for r in sub if r.get("variants", {}).get(name, {}).get("triggered"))
            cm = _med(caps)
            nm = _med([r["variants"][name].get("realized_pct") - _EXIT_FEE_PCT
                       for r in sub if name in r.get("variants", {})
                       and r["variants"][name].get("realized_pct") is not None])
            nd = [r["variants"][name]["realized_pct"] - r["live"]["realized_pct"]
                  for r in sub if name in r.get("variants", {})
                  and r["variants"][name].get("realized_pct") is not None
                  and r["live"].get("realized_pct") is not None]
            ndm = f"净差均值{sum(nd)/len(nd)*100:+.1f}bp" if nd else ""
            seg = f"{name} cap中位{cm:+.3f}"
            if nm is not None:
                seg += f"(净{nm:+.2f}% {ndm})"
            parts.append(f"{seg}(触发{ntrig})" if cm is not None else f"{name} n/a")
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
        # 2026-09-23 (用户裁「按建议执行」后续): horizon 拆批。8/26 有一次**一次性回填**
        #   (26 个 capture 日, horizon 1..43 天, 207 只全混在同一个 scored_on) —— 与日常批
        #   (horizon 1-3 天) 混算 = 把长窗走势当 1 日读数的证据。分开印, 不合并。
        def _horizon(r):
            try:
                return (date.fromisoformat(str(r.get("scored_on"))[:10])
                        - date.fromisoformat(str(r.get("capture_date"))[:10])).days
            except Exception:
                return None

        def _agg(rows, label):
            fwd = [z["fwd_pct"] for r in rows for z in r.get("results", [])]
            hit = [z["hit"] for r in rows for z in r.get("results", [])]
            if not hit:
                return ""
            return (f" {label}{len(hit)}只 命中率(按方向,前瞻≥1%){round(sum(hit)/len(hit),3)}"
                    f" 平均前瞻{round(sum(fwd)/len(fwd),2):+}%")

        daily = [r for r in s if (_horizon(r) is not None and _horizon(r) <= 3)]
        batch = [r for r in s if not (_horizon(r) is not None and _horizon(r) <= 3)]
        part = f"capture={len(c)} 浮现{surf}人次/{uniq}只 [窗口按capture日]"
        part += _agg(daily, "日常批(≤3日)")
        part += _agg(batch, "回填/长窗批(>3日)")
        if not daily and not batch:
            part += " | 未评分(待 score)"
        return part

    lines.append(f"  近7天: {blk(7)}")
    lines.append(f"  近30天:{blk(30)}")
    return lines


# ── 段9: 下单前统一收口审计 (exec_gate_audit) ─────────────────
def _sec_exec_gate():
    """下单前统一收口审计 (2026-07-17)。

    exec_gate_audit.jsonl: 每个到达执行漏斗的候选 (DA/UE/内部全覆盖) 的
    来源 + 最终动作 + EdgeShadowGate 判定。核心指标: 已执行单里 edge 判为
    BLOCK (负期望) 的比例 — 量化"若 edge 门实装, 会拦掉多少我们实际下的单"。
    """
    recs = _read_jsonl(SHADOW_DIR / "exec_gate_audit.jsonl")
    lines = ["【下单前收口审计 (全来源: DA/UE/内部)】"]
    if not recs:
        lines.append("  暂无样本 (盘中未执行 / 刚上线) — 产出后自动显示")
        return lines

    # 2026-08-26: 结局回填 — exec_gate 在下单前埋点, 天然无 pnl 字段。
    #   join trade_records (symbol + 入场日期) 回填真实结局, 量化
    #   "edge 门拦的单到底该不该拦"。已验证口径 (8/26 独立复核):
    #   BLOCK 笔均 -$5.2 < PASS -$12.3 → 拦截方向正确 (筛掉更差的)。
    _pnl_by_key = {}
    try:
        _db = ROOT / "data" / "ftseek_us.db"
        if _db.exists():
            _c = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
            for _sym, _d, _n, _pnl in _c.execute(
                "SELECT symbol, date(entry_time), COUNT(*), "
                "SUM(COALESCE(pnl_net, realized_pnl, 0)) FROM trade_records "
                "WHERE entry_time IS NOT NULL GROUP BY symbol, date(entry_time)"):
                _pnl_by_key[(_sym, _d)] = (_n, _pnl)
            _c.close()
    except Exception:
        pass

    def blk(days):
        sub = [r for r in recs if _within(r.get("date"), days)]
        n = len(sub)
        if not n:
            return "无样本"
        src = Counter(r.get("source") for r in sub)
        src_s = " ".join(f"{k}:{v}" for k, v in src.most_common())
        execed = [r for r in sub if r.get("action") == "executed"]
        skipped = [r for r in sub if str(r.get("action", "")).startswith("skipped")]
        # 2026-09-23 (用户裁「按建议执行」): 三项分列。原先只印"执行/跳过",
        #   deferred_cycle_cap 两头都不计 ⇒ 行内 候选 ≠ 执行+跳过 (实测 679 vs 666,
        #   差额 13 条看着像丢数据)。现在 候选 = 执行+跳过+延后+其他 恒等。
        deferred = [r for r in sub if str(r.get("action", "")).startswith("deferred")]
        other = n - len(execed) - len(skipped) - len(deferred)
        # 核心: 已执行单里 edge 判 BLOCK 的 (edge 门若实装会拦掉的实盘单)
        edge_block_exec = [r for r in execed if r.get("edge_verdict") == "BLOCK"]
        s = (f"候选{n} 执行{len(execed)} 跳过{len(skipped)} 延后{len(deferred)}"
             + (f" 其他{other}" if other else "")
             + f" | 来源[{src_s}]")
        if execed:
            s += (f" | edge门若实装会拦{len(edge_block_exec)}/{len(execed)}执行单"
                  f"({len(edge_block_exec)/len(execed)*100:.0f}%)")
        return s

    def outcome_blk(days):
        """BLOCK vs PASS 已执行单的真实结局 (反事实裁决依据)。

        2026-09-23 (用户裁「按建议执行」) 两处纠偏:
          ① 显示 n (笔数) — 只印两个 $ 均值必误判: BLOCK 组 n 远小于 PASS 组
             (7d 14 vs 24 / 30d 46 vs 136), 均值方差完全不可比;
          ② 方向由本窗实数判, 不再写死结论。原行尾括号 "(BLOCK笔均≥PASS=门在筛
             更差的单)" 是 8/26 那次读数的快照, 与现读数方向相反。
        口径: 键 = 唯一 (symbol, ET日); 同键多候选行只计一次 (否则同一
        trade_records 组被重复计入, 30d PASS 会被虚增 154 笔 vs 实 136 笔)。
        笔数 = 该键在 trade_records 的行数 (按 symbol+入场日聚合)。
        返回 (显示串, {verdict: 笔均})。
        """
        sub = [r for r in recs if _within(r.get("date"), days)
               and r.get("action") == "executed"]
        if not sub or not _pnl_by_key:
            return "", {}
        parts, means = [], {}
        for verdict in ("BLOCK", "PASS"):
            keys = {(r.get("symbol"), str(r.get("date", ""))[:10])
                    for r in sub if r.get("edge_verdict") == verdict}
            matched = [_pnl_by_key[k] for k in keys if k in _pnl_by_key]
            matched = [m for m in matched if m]
            if not matched:
                continue
            n_tr = sum(m[0] for m in matched)
            if not n_tr:
                continue
            means[verdict] = sum(m[1] for m in matched) / n_tr
            parts.append(f"{verdict}笔均{means[verdict]:+.2f}$(n={n_tr})")
        if len(means) == 2:
            _gap = means["BLOCK"] - means["PASS"]
            parts.append(f"差{_gap:+.2f}$"
                         + ("(门在筛更差的单)" if _gap < 0 else "(门拦的是更赚的单)"))
        return " | ".join(parts), means

    lines.append(f"  近7天: {blk(7)}")
    lines.append(f"  (今日 : {blk(1)})")
    # 结局回填: 两窗并列 (2026-09-23)。单窗读数会翻转裁决方向, 必须两窗一起看;
    #   两窗方向相反 = 门对该拦的单无稳定区分力 ⇒ 不给"实装"读数。
    _wmeans = {}
    for _d, _lbl in ((7, "近7天"), (30, "近30天")):
        _ob, _m = outcome_blk(_d)
        if _ob:
            _wmeans[_lbl] = _m
            lines.append(f"  结局回填{_lbl}: {_ob}")
    if all(set(m) >= {"BLOCK", "PASS"} for m in _wmeans.values()) and len(_wmeans) == 2:
        _g7 = _wmeans["近7天"]["BLOCK"] - _wmeans["近7天"]["PASS"]
        _g30 = _wmeans["近30天"]["BLOCK"] - _wmeans["近30天"]["PASS"]
        if _g7 * _g30 < 0:
            lines.append("  ⇒ 两窗方向相反 (7d 拦对 / 30d 拦错) → 门对哪些单该拦无稳定"
                         "区分力, 数据不支持实装 edge 门 (待 DEB)")
        elif _g7 < 0:
            lines.append("  ⇒ 两窗同向 (BLOCK 更低) → 门在筛更差的单 (待 DEB 裁决)")
        else:
            lines.append("  ⇒ 两窗同向 (BLOCK 更高) → 门拦的是更赚的单, 数据不支持实装"
                         " edge 门 (待 DEB)")
    return lines


# ── 段10: 死单早期识别影子 (2026-08-11, DEB-20260811) ────────────
def _sec_early_dead():
    recs = _read_jsonl(ROOT / "data" / "early_dead_signal_shadow.jsonl")
    lines = ["【死单早期识别影子 (15/30min MFE快照, 30min<0.2%=候选死单)】"]
    if not recs:
        lines.append("  (尚无检查点样本)")
        return lines

    def blk(days):
        sub = [r for r in recs if _within(r.get("ts"), days)]
        c30 = [r for r in sub if r.get("checkpoint") == 30]
        c15 = [r for r in sub if r.get("checkpoint") == 15]
        # 2026-09-23 (用户裁「按建议执行」): ①`or 99` 把 mfe_pct==0.0 的行当缺失丢掉
        #   (最死的一批恰是 0.0) ⇒ 命中数比结算器少算; 改用"There is not None"判缺失。
        #   ②死单率标签注明词表口径: 预注册词表在现役退出栈基率仅 ~1%, 不标注会被读成
        #   "零死单⇒影子无效" (实为词语表漂移, 现役词表读法见结算器输出)。
        h30 = [r for r in c30 if (r.get("mfe_pct") if r.get("mfe_pct") is not None else 99) < 0.2]
        h15 = [r for r in c15 if (r.get("mfe_pct") if r.get("mfe_pct") is not None else 99) < 0.15]
        # 已结算行带 final_pnl_net/is_dead (由结算脚本回填)
        settled30 = [r for r in h30 if r.get("settled")]
        dead30 = sum(1 for r in settled30 if r.get("is_dead"))
        net30 = sum(r.get("final_pnl_net") or 0 for r in settled30)
        s = (f"30min行{len(c30)} 命中{len(h30)}"
             + (f" 已结算{len(settled30)} 死单率(预注册词表){100*dead30/len(settled30):.0f}% 净{net30:+.0f}$" if settled30 else " 待结算")
             + f" | 15min命中{len(h15)}/{len(c15)}")
        return s

    lines.append(f"  今日 : {blk(1)}")
    lines.append(f"  近7天: {blk(7)}")
    # 2026-09-23 (用户裁「按建议执行」): 结算面守卫行。此前全历史 0 行有真值
    #   (settler scripts/early_dead_signal_report.py 从未挂 cron), 本段却每天照印
    #   "30min行/命中N" 一大行 = 假活跃 (看着在攒样, 其实永远裁不了)。守卫行让
    #   "结算面停摆" 与 "统计口径变更" 一样当天可见, 不再靠人想起来去查。
    _n_all = len(recs)
    _n_settled = sum(1 for r in recs if r.get("settled"))
    if _n_settled == 0:
        lines.append(f"  ⚠️ 结算面停摆: 0/{_n_all} 行有真值 — settler "
                     "(scripts/early_dead_signal_report.py) 未跑 ⇒ 本段无裁决力")
    else:
        lines.append(f"  结算面: 已结算{_n_settled}/{_n_all} 行 (settler="
                     "scripts/early_dead_signal_report.py; 未结算=未平仓或 "
                     "entry_time 无法配对)")
    return lines


# ── 段11: 保本线穿透影子 (2026-09-03, DEB-20260903 议题二后续, 用户裁"A") ──
def _sec_penetration():
    recs = _read_jsonl(SHADOW_DIR / "penetration_shadow.jsonl")
    lines = ["【保本线穿透影子 (快跌vsV回判别器: 总≥20 且每类≥8 才可训)】"]
    evs = [r for r in recs if r.get("type") == "penetration"]
    ocs = [r for r in recs if r.get("type") == "outcome" and r.get("status") == "labeled"]
    if not evs:
        lines.append("  (尚无穿透样本 — 采集随下次服务重启生效)")
        return lines

    def blk(days):
        se = [r for r in evs if _within(r.get("et_date"), days)]
        so = [r for r in ocs if _within(r.get("et_date"), days)]
        v5 = sum(1 for r in so if r.get("recovered_5m") is True)
        vr = f" V型率{100*v5/len(so):.0f}%" if so else ""
        return f"事件{len(se)} 已回填{len(so)}{vr}"

    n_labeled = len(ocs)
    v5_all = sum(1 for r in ocs if r.get("recovered_5m") is True)
    n_nonv = n_labeled - v5_all          # 少数类 = 未V回 (真"快跌"候选)
    lines.append(f"  今日 : {blk(1)}")
    lines.append(f"  近7天: {blk(7)}")
    # 2026-09-23 (用户裁「按建议执行」): 毕业判据改按类计 + 类别计数上板。
    #   旧文案只数总样本量 (n_labeled≥20 → "已达毕业门"), 实测 86 例里 84 是 V回 /
    #   仅 2 未回 ⇒ 判别器正负类 84:2, precision≥0.7 门在 0.977 基线上形同虚设
    #   (= 数量误导, 会让"该不该开 DEB"的裁决走反)。每类下限取脚本内 SSOT 常量,
    #   不在此处另写一份 (改门只改一处)。
    try:
        import penetration_llm_analysis as _pen
        _minority_min, _grad_n = _pen.GRAD_MINORITY, _pen.GRAD_N
    except Exception:
        _minority_min, _grad_n = 8, 20
    lines.append(f"  类别: V回{v5_all} / 未V回{n_nonv} (每类须 ≥{_minority_min})")
    if n_labeled and v5_all / n_labeled >= 0.95 and n_nonv < _minority_min:
        lines.append(f"  ⇒ V型率{100*v5_all/n_labeled:.0f}% (穿透后绝大多数回摆): 支持"
                     "不加「穿透即早砍」—— 砍在穿透点会错在所测的绝大多数")
    if n_labeled < _grad_n:
        _tail = " — 继续攒样"
    elif n_nonv < _minority_min:
        _tail = (f" — 样本量达门但少数类 {n_nonv}<{_minority_min} ⇒ 判别器不可训练"
                 " (V型率高时 precision 门在基线上即过)")
    else:
        _tail = " — 样本量与类别平衡均达门, 周六分析环评估开DEB"
    lines.append(f"  攒样: {n_labeled}/{_grad_n} 带标签"
                 + (f" (V型率{100*v5_all/n_labeled:.0f}%)" if n_labeled else "")
                 + _tail)
    return lines


# ── 段10: 出场后全天路径 (全量平仓单, 2026-09-04 MU V反核查产物) ─────
def _sec_post_exit():
    """出场后路径: post_fav/close_gap 方向感知 (正=市场继续朝原方向走)。
    与段9 (penetration, 10m短窗) 互补: 本段看到收盘。
    状态四分类 (P1 口径): ok=全天bar收盘前出场 / ah_exit=夜盘出场(隔夜段
    看 next_open_gap) / bar_truncated=窗口bar补拉失败 / no_bars=无bar。
    判分只用 ok; 其余在状态行如实可见 (数据不许静默蒸发)。"""
    import statistics
    all_recs = [r for r in _read_jsonl(SHADOW_DIR / "post_exit_path.jsonl")
                if r.get("type") == "post_exit"]
    recs = [r for r in all_recs if r.get("status") == "ok"]
    lines = ["【出场后全天路径 (全量平仓单, 退出判分对照臂)】"]
    if not all_recs:
        lines.append("  暂无样本 — post_exit_path_shadow.py 盘后跑后自动显示")
        return lines
    # 状态分布 (全量口径, 含非判分样本)
    sc = {}
    for r in all_recs:
        sc[r.get("status")] = sc.get(r.get("status"), 0) + 1
    sc_s = " ".join(f"{k}={v}" for k, v in sorted(sc.items()))
    lines.append(f"  状态: {sc_s} (判分只用 ok; ah_exit=夜盘出场看隔夜段)")

    def blk(days):
        sub = [r for r in recs if _within(r.get("date"), days)]
        if not sub:
            return "无样本"
        fav = statistics.median([r["post_fav_pct"] for r in sub])
        gaps = [r["close_gap_pct"] for r in sub]
        gm, ga = statistics.median(gaps), sum(gaps) / len(gaps)
        miss = sum(1 for g in gaps if g > 1) / len(gaps)
        deep = [r for r in sub if r.get("deep_dd_day")]
        deep_s = ""
        if deep:
            dg = statistics.median([r["close_gap_pct"] for r in deep])
            deep_s = f" | 深回撤V反日{len(deep)}笔 gap中位{dg:+.2f}%"
        # 坑底附近割 (距当日低<1%) 且深回撤日 — MU 9/3 形态
        # 2026-09-23: 同批修的 `or 99` 陷阱 (exit_dist_from_low_pct==0.0 = 正好割在当日低点,
        #   恰是最该被数出来的一批, 原写法把它们当缺失丢掉)
        pit = [r for r in deep
               if (r.get("exit_dist_from_low_pct") if r.get("exit_dist_from_low_pct") is not None else 99) < 1.0]
        pit_s = f" 其中坑底附近割{len(pit)}笔" if pit else ""
        return (f"n={len(sub)} 出场后同向空间中位{fav:+.2f}% | 到收盘 gap中位{gm:+.2f}%"
                f"(均值{ga:+.2f}%) 错过>1%: {100*miss:.0f}%{deep_s}{pit_s}")

    lines.append(f"  今日 : {blk(1)}")
    lines.append(f"  近7天: {blk(7)}")
    lines.append(f"  近30天:{blk(30)}")
    # 夜盘出场隔夜段 (ah_exit): 出场→次日开盘方向感知 gap
    ah = [r for r in all_recs if r.get("status") == "ah_exit"
          and r.get("next_open_gap_pct") is not None]
    if ah:
        nog = statistics.median([r["next_open_gap_pct"] for r in ah])
        lines.append(f"  夜盘出场 {len(ah)} 笔: 隔夜gap中位 {nog:+.2f}% (出场→次日开盘, 方向感知)")
    return lines


# ── 段12: RTH fade 影子 (RFC-098; 跳空≥+3% 财报票 开盘做空→收盘, 零下单) ──
def _sec_rth_fade():
    """D 项唯一正期望路线的攒样进度。

    2026-09-23 用户问「另外也有影子 请核查是否可融合」⇒ **并入本看板**, 停独立推送
    (项目既有约定: 「同内容 15min 内双推冗余」⇒ 由 shadow_board 段复用, 见 cron 注释)。
    """
    recs = _read_jsonl(ROOT / "data" / "rth_fade_shadow.jsonl")
    lines = ["【RTH fade 影子 (RFC-098; 跳空≥+3% 财报票 开盘做空→收盘, 零下单)】"]
    if not recs:
        lines.append("  (尚无样本)")
        return lines
    days = sorted({r.get("rth_day") for r in recs if r.get("rth_day")})
    nets = [r.get("net_usd") or 0 for r in recs]
    win = sum(1 for n in nets if n > 0)
    day_tot = {}
    for r in recs:
        day_tot[r.get("rth_day")] = day_tot.get(r.get("rth_day"), 0) + (r.get("net_usd") or 0)
    best = max(day_tot, key=lambda d: day_tot[d]) if day_tot else None
    rest = [r.get("ret_pct") or 0 for r in recs if r.get("rth_day") != best]
    leave = (sum(rest) / len(rest)) if rest else 0.0
    lines.append(f"  累计: {len(recs)} 笔 / {len(days)} 个 RTH 日 | 净/笔 ${sum(nets)/len(nets):+.2f} "
                 f"胜率 {100*win/len(nets):.0f}% 合计 ${sum(nets):+.0f} | 留一口 {leave:+.2f}%")
    sl = [r.get("net_usd_sl35") for r in recs if r.get("net_usd_sl35") is not None]
    if sl:
        slw = sum(1 for x in sl if x > 0)
        hit = sum(1 for r in recs if r.get("sl35_hit"))
        lines.append(f"  平行臂(−3.5%止 · 只对照不决策): 净/笔 ${sum(sl)/len(sl):+.2f} 胜率 "
                     f"{100*slw/len(sl):.0f}% 触发 {hit}/{len(recs)}")
    need_d, need_n = 15, 30
    done = len(days) >= need_d and len(recs) >= need_n
    lines.append(f"  转正进度: 需 ≥{need_d} 日 且 ≥{need_n} 笔 → 还差 {max(0, need_d-len(days))} 日 / "
                 f"{max(0, need_n-len(recs))} 笔"
                 + ("；**已达样本门槛, 待 净/笔>0 且 胜率≥55% 且 留一口≥0 三项裁决**"
                    if done else " (裁据: scripts/rth_fade_graduation_check.py)"))
    latest = max((r.get("date") or "") for r in recs)
    try:
        from datetime import date as _d
        stale = (_d.fromisoformat(str(_today_et())[:10]) - _d.fromisoformat(latest)).days
    except Exception:
        stale = None
    if stale is not None and stale > 4:
        lines.append(f"  ⚠️ 记录器停摆: 最新入账 {latest} ({stale} 天前) — cron 的 "
                     "scripts/rth_fade_shadow.py 未跑 ⇒ 本段无新证据")
    else:
        lines.append("  最近: " + " | ".join(
            f"{r['symbol']} 跳空{r.get('gap_pct', 0):+.1f}%→{r.get('ret_pct', 0):+.1f}% "
            f"${r.get('net_usd', 0):+.0f}" for r in recs[-3:]))
    return lines


def _age_days(s):
    try:
        from datetime import date as _d
        return (_d.fromisoformat(str(_today_et())[:10]) - _d.fromisoformat(str(s)[:10])).days
    except Exception:
        return None


# ── 注册表驱动 + 生产者停摆守卫 (2026-09-26, RFC-109③) ──────────────────
# 根因 (RFC-109 审计): 看板 11 段曾绕过 shadow_registry 硬编码 ⇒ 段面无
# status/min_n/停采通道, 生产者死了与「正常无触发」在看板上不可区分。
# 修法 (做减法): 不重写各段读数逻辑 (数字与注册表一致), 由 build_board 统一
# 从 SSOT 追加两类行 — ①注册面行 (status/n/min_n/挂账/停采标, 取自 REGISTRY,
# 计数走 refresh_counts 只读现算) ②停摆守卫行 (artifact mtime 超阈 ⇒ 显式警告;
# 终态/已停采项**豁免**陈旧警告 — 停更属预期)。未登记 id ⇒ 显式告警行, 不静默。
_REG_CACHE = {}


def _reg_item(reg_id):
    if not _REG_CACHE:
        try:
            from app.playbook.shadow_registry import REGISTRY, refresh_counts
            refresh_counts()  # 只读现算 current_n (内存态, 不落盘不改文件)
            _REG_CACHE.update({s.id: s for s in REGISTRY})
        except Exception as e:
            _REG_CACHE["__error__"] = str(e)
    if "__error__" in _REG_CACHE:
        return None
    return _REG_CACHE.get(reg_id)


def _guard_lines(entries):
    """entries: [(reg_id, artifact_rel, stale_days), ...] — stale_days=None 跳过陈旧判定。"""
    out = []
    import time as _t
    now = _t.time()
    for reg_id, artifact, stale_days in entries:
        s = _reg_item(reg_id)
        if s is None:
            out.append(f"  ⚠️ 注册面缺失[{reg_id}] — 不在 shadow_registry (无裁决/退役通道)")
        else:
            st = s.status.value if hasattr(s.status, "value") else str(s.status)
            extra = ""
            if s.collection_retired:
                extra += " ·已停采"
            if s.no_adjudication_reason:
                extra += f" ·挂账={s.no_adjudication_reason.split(':')[0]}"
            out.append(f"  注册[{reg_id}] {st} n={s.current_n}/{s.min_n}{extra}")
        p = ROOT / artifact
        if not p.exists():
            out.append(f"  ⚠️ 生产者缺产物: {artifact} 不存在 ⇒ 本段无证据 (≠正常无触发)")
            continue
        try:
            age = (now - p.stat().st_mtime) / 86400.0
        except OSError:
            continue
        terminal = s is not None and (
            s.collection_retired
            or (s.status.value if hasattr(s.status, "value") else str(s.status))
            in ("KILLED", "SUPERSEDED", "GRADUATED"))
        if stale_days is not None and age > stale_days and not terminal:
            out.append(f"  ⚠️ 生产者停摆: {artifact} 最新写入 {age:.1f} 天前 "
                       f"(>{stale_days}d 阈) ⇒ 本段无新证据, 查对应 cron/写方")
    return out


# 段 → (注册表 id, 产物, 停摆阈值天数)。融合段(13)挂多条; 阈值=调度面最长自然间隔
# +1 tick (交易日日频⇒4d; 事件驱动低频⇒8d, 同 review_coverage_scan 口径)。
_SECTION_TABLE = None  # 在 build_board 前由 _init_table() 填充 (函数需先定义)


def _init_table():
    global _SECTION_TABLE
    _SECTION_TABLE = (
        (_sec_sltp, (("sltp_verify_shadow", "data/shadow/sltp_verify_shadow.jsonl", 4),)),
        (_sec_edge, (("edge_gate_shadow", "data/shadow/edge_gate_shadow.jsonl", 4),)),
        (_sec_premarket_peer, (("premarket_peer_shadow", "data/shadow/premarket_peer_shadow.jsonl", 4),)),
        (_sec_exit_param, (("exit_param_shadow", "data/shadow/exit_param_shadow.jsonl", 4),)),
        (_sec_universe_expansion, (("universe_expansion_shadow", "data/shadow/universe_expansion_shadow.jsonl", 4),)),
        (_sec_exec_gate, (("exec_gate_audit", "data/shadow/exec_gate_audit.jsonl", 4),)),
        (_sec_early_dead, (("early_dead_signal_shadow", "data/early_dead_signal_shadow.jsonl", 4),)),
        (_sec_penetration, (("penetration_shadow", "data/shadow/penetration_shadow.jsonl", 4),)),
        (_sec_post_exit, (("post_exit_path", "data/shadow/post_exit_path.jsonl", 4),)),
        (_sec_rth_fade, (("eth_rth_fade", "data/rth_fade_shadow.jsonl", 8),)),
        (_sec_eth_short_family, (
            ("overnight_earnings_carry", "data/overnight_earnings_carry_shadow.jsonl", None),
            ("short_funnel_shadow", "data/short_funnel_shadow.jsonl", 4),
            ("ue_short_boost_shadow", "data/ue_short_boost_shadow.jsonl", 4),
            ("eth_earnings_direction_path", "data/earnings_direction_path_shadow.jsonl", 4),
        )),
    )


# ── 段13: 延长时段/空头族 融合批 (2026-09-23 用户裁「按建议实施执行」) ──
def _sec_eth_short_family():
    """把"同人群/同方向、且**生产者活着**"的几个影子并到一张卡上 (原本各自攒样、无人看)。

    只融活的: 生产者已死的 (gap_semantics 26 天无写入; band_vol/entry_sync/stale_anchor/
    stall_entry 同批 26 天; intraday_loss_breaker 无台账) **一律不融** —— 融死影子 =
    让看板替僵尸背书 (本项目「生产者死消费者仍读」病灶), 已列清单待退役。
    """
    out = ["【延长时段/空头族 (融合批 · 均 record-only, 零下单)】"]
    r = _read_jsonl(ROOT / "data" / "overnight_earnings_carry_shadow.jsonl")
    if r:
        last = r[-1]
        s = last.get("summary") or {}
        today = str(_today_et())[:10]
        pk = max((x.get("summary", {}).get("n_passes_floors", 0)
                  for x in r if x.get("et_date") == today), default=0)
        # 2026-09-24 标签诚实化: carry 影子已退役 (用户裁「彻底停采不重启」, 判据
        # <0.5只/日达成, 见 docs/analysis/2026-09-24-carry-shadow-adjudication.md)。
        # 末行数据保留展示仅作归档, 必须带退役标 — 看板不得替死影子背书。
        out.append(f"  ① 隔夜财报carry [⛔已退役9/24, 末行系停采前归档] [{last.get('session', '?')}] cohort {s.get('cohort', 0)} "
                   f"报价 {s.get('quoted', 0)} 陈旧 {s.get('stale_quotes', 0)} | 今日过闸上界 {pk} "
                   f"| 最近 {last.get('et_date')} {last.get('et_time')} ET")
    else:
        out.append("  ① 隔夜财报carry: (⛔已退役 2026-09-24 用户裁停采, 无样本)")
    r = _read_jsonl(ROOT / "data" / "short_funnel_shadow.jsonl")
    if r:
        last = r[-1]
        a = _age_days(last.get("date"))
        out.append(f"  ② 空头漏斗 [{last.get('date')}] 分析 {last.get('analyzed_n')} 只 → 看空 "
                   f"{last.get('rated_bearish_n')} → 空头指令 {last.get('short_instructions')} → "
                   f"执行 {last.get('short_executed')} (闸阻塞提示 {last.get('gate_block_hints')})"
                   + (f" ⚠️{a}天前" if a is not None and a > 3 else ""))
    else:
        out.append("  ② 空头漏斗: (无样本)")
    r = _read_jsonl(ROOT / "data" / "ue_short_boost_shadow.jsonl")
    if r:
        days = sorted({x.get("date") for x in r})
        a = _age_days(days[-1])
        syms = ", ".join(x.get("symbol") for x in r if x.get("date") == days[-1])
        out.append(f"  ③ UE空头增强 累计 {len(r)} 只/{len(days)} 日 | 最近 {days[-1]}: {syms}"
                   + (f" ⚠️{a}天前" if a is not None and a > 3 else ""))
    else:
        out.append("  ③ UE空头增强: (无样本)")
    r = _read_jsonl(ROOT / "data" / "earnings_direction_path_shadow.jsonl")
    if r:
        days = sorted({x.get("date") for x in r})
        a = _age_days(days[-1])
        nb = sum(1 for x in r if x.get("beat_miss") == "beat")
        nm = sum(1 for x in r if x.get("beat_miss") == "miss")
        ns = sum(1 for x in r if x.get("plan_dir") == "SHORT")
        nl = sum(1 for x in r if x.get("plan_dir") == "LONG")
        nets = [x["plan_net_usd"] for x in r if x.get("plan_net_usd") is not None]
        sn = f" 计划净 ${sum(nets):+.0f} ({len(nets)} 笔)" if nets else ""
        out.append(f"  ④ 财报全量路径(beat+miss): {len(r)} 事件/{len(days)} 日 | beat {nb} miss {nm} "
                   f"| 计划 SHORT {ns} LONG {nl}{sn}"
                   + (f" ⚠️{a}天前" if a is not None and a > 3 else ""))
    else:
        out.append("  ④ 财报全量路径(beat+miss): (尚无样本 — cron 20 4 * * 2-6)")
    return out


def build_board():
    head = [f"🌓 FTSeek-US 影子看板 | {_today_et()} (ET) | 单日 / 近7天", "=" * 46]
    body = []
    _init_table()
    for sec, entries in _SECTION_TABLE:
        try:
            seg = sec()
            try:
                seg += _guard_lines(entries)
            except Exception as ge:
                seg += [f"  ⚠️ 守卫行异常: {ge}"]
            body += seg + [""]
        except Exception as e:
            body += [f"⚠️ {sec.__name__} 整段异常: {e}", ""]
    tail = ["注: 只读汇总; 看板=唯一影子推送出口 (RFC-109⑦: 独立 --push 已撤, 段注册面取自 shadow_registry)。"]
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
