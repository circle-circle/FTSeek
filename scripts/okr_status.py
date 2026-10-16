#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""OKR Status Report - 标准化输出，任何 agent 可调用"""
import sys, json
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

# ─── 0. 统一口径常量 ───
# OKR 目标是 Playbook 系统, trade_records 中只统计 Playbook 相关来源.
# PLAYBOOK_SHADOW 已废弃但历史数据仍计入, 以便与 playbook_instructions 口径对齐.
PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')

# ─── 1. Playbook Instructions ───
from app.playbook.db import PlaybookDB
db = PlaybookDB()
db.ensure_tables()

instructions = db.get_recent_instructions(limit=500, include_skip=True)
total = len(instructions)
skip_count = sum(1 for i in instructions if i.get('direction') == 'SKIP')
non_skip = total - skip_count
with_entry = sum(1 for i in instructions if i.get('entry_price') and i['entry_price'] > 0)
triggered = sum(1 for i in instructions if i.get('triggered'))
executed = sum(1 for i in instructions if i.get('executed'))

# Outcomes (只统计已执行且产生终态 win/loss 的指令, 与 OKR 目标 "outcome in ('win','loss')" 对齐)
wins = sum(1 for i in instructions if i.get('outcome') == 'win')
losses = sum(1 for i in instructions if i.get('outcome') == 'loss')
holding = sum(1 for i in instructions if i.get('outcome') == 'holding')
missed = sum(1 for i in instructions if i.get('outcome') == 'missed')
skipped = sum(1 for i in instructions if i.get('outcome') == 'skipped')
has_outcome = wins + losses

win_rate = wins / has_outcome * 100 if has_outcome > 0 else 0
# OKR PnL: 先用 playbook_instructions 作为初始值, 后面 trade_records 段会覆盖.
total_pnl = sum((i.get('pnl') or 0) for i in instructions if i.get('executed') and i.get('outcome') in ('win', 'loss'))

# Weekly PnL: 按周聚合, 同样只统计 executed + win/loss
weekly_pnl = {}
for inst in instructions:
    if not (inst.get('executed') and inst.get('outcome') in ('win', 'loss')):
        continue
    d = inst.get('date')
    if not d:
        continue
    dt = datetime.strptime(d, '%Y-%m-%d')
    wk = dt.strftime('%Y-W%W')
    if wk not in weekly_pnl:
        weekly_pnl[wk] = {'pnl': 0, 'trades': 0, 'wins': 0, 'losses': 0}
    weekly_pnl[wk]['trades'] += 1
    p = inst.get('pnl', 0) or 0
    weekly_pnl[wk]['pnl'] += p
    if p > 0:
        weekly_pnl[wk]['wins'] += 1
    elif p < 0:
        weekly_pnl[wk]['losses'] += 1

# ─── 2. Trade Records (Playbook only) ───
from app.storage.db_pool import get_db
conn = get_db("ftseek_us").connection()

# 2026-06-30: 用 ET 日期计算 cutoff (date('now') 是 UTC)
try:
    from app.utils.timezone import now_et as _now_et
    from datetime import timedelta as _td
    _cutoff_30d = (_now_et() - _td(days=30)).strftime("%Y-%m-%d")
except Exception:
    from datetime import datetime as _dt, timedelta as _td
    _cutoff_30d = (_dt.now() - _td(days=30)).strftime("%Y-%m-%d")

_source_filter = ", ".join(f"'{s}'" for s in PLAYBOOK_SOURCE_TYPES)
# 2026-08-20 (用户裁"切"): PnL/胜率统一净口径 — pnl_net=毛-费; 旧行无 pnl_net 回退毛.
#   毛口径把"毛>0但≤费用"的假盈利计为胜, 8/1-8/20 实测虚高胜率 18pp.
_NET = "COALESCE(pnl_net, realized_pnl)"
# 2026-09-06 修尺子: 尺寸归一口径 (bps of notional) 与美元口径**并列输出**。
#   美元/笔跨 2026-08-06 sizing 变更 (三提交 8b81f4dc/8e281cd3/e889b6b5 → 名义额中位
#   $3,307→$5,737, 每笔佣金几乎不变 $4.14→$4.21) 不可比: "净$/笔变好"里混着"注下大了"。
#   bps 把分母归一才是同一把尺子。单位: 万分比 (1bp = 0.01%), 名义额 = entry_price*quantity。
# 括号是**必需**的: SQL 里 `/` 与 `*` 同级左结合, 裸写 `pnl / entry_price * quantity`
#   会被解析成 `(pnl/entry_price)*quantity` → bps 放大 quantity 倍 (实测炸到 +1.5e6 bps)。
#   与 exit_scorecard.size_normalized() 的 Python 侧算式互为对照, 两边必须同值。
_NOTIONAL = "(entry_price * quantity)"
# guard 同时要求 realized_pnl 非空: 未平仓行的 realized_pnl 是 NULL, AVG 会自动跳过 →
#   毛/净均值的实际取样面是"已平仓", 而 fee 用 COALESCE(fees_est,0) 不会跳过 → 三个均值
#   取样面不一致, "毛 − 费 = 净" 就不恒等 (实测费被 3 行未平仓稀释成 8.83 而非 8.95)。
#   统一到已平仓面, cnt_bps 才是这三个数字真实的 n。
_BPS_GUARD = f"CASE WHEN realized_pnl IS NOT NULL AND {_NOTIONAL} > 0 THEN"
# bps 合理性哨兵: 单笔 |bps| 上界取 10000 (=100% 名义额)。日均超此值 = 量纲/括号又错了,
#   标 suspect 而不是静默输出天文数字 (承 AGENT.md「单位/量纲声明」军规)。
_BPS_SUSPECT_LIMIT = 10000.0
trade_stats = {}
try:
    cursor = conn.execute(f"""
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN {_NET} > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN {_NET} < 0 THEN 1 ELSE 0 END) as losses,
               SUM({_NET}) as total_pnl,
               SUM(CASE WHEN realized_pnl IS NOT NULL AND {_NOTIONAL} > 0 THEN 1 ELSE 0 END) as cnt_bps,
               AVG({_BPS_GUARD} realized_pnl / {_NOTIONAL} * 10000 END) as gross_bps,
               AVG({_BPS_GUARD} COALESCE(fees_est, 0) / {_NOTIONAL} * 10000 END) as fee_bps,
               AVG({_BPS_GUARD} {_NET} / {_NOTIONAL} * 10000 END) as net_bps,
               AVG({_BPS_GUARD} {_NOTIONAL} END) as avg_notional
        FROM trade_records
        WHERE trade_date >= ?
          AND source_type IN ({_source_filter})
          AND trade_mode != 'live'
    """, (_cutoff_30d,))
    row = cursor.fetchone()
    trade_stats = dict(row) if row else {}
    for _k in ("gross_bps", "fee_bps", "net_bps", "avg_notional"):
        if trade_stats.get(_k) is not None:
            trade_stats[_k] = round(trade_stats[_k], 2)
    trade_stats["bps_unit"] = "bps of notional (万分比; 1bp = 0.01%)"
    trade_stats["bps_suspect"] = any(
        abs(trade_stats.get(_k) or 0) > _BPS_SUSPECT_LIMIT
        for _k in ("gross_bps", "fee_bps", "net_bps"))
except Exception as e:
    trade_stats = {"error": str(e)}

# By strategy (Playbook only)
by_strategy = []
try:
    cursor2 = conn.execute(f"""
        SELECT strategy_id,
               COUNT(*) as cnt,
               SUM(CASE WHEN {_NET} > 0 THEN 1 ELSE 0 END) as wins,
               SUM({_NET}) as total_pnl
        FROM trade_records
        WHERE trade_date >= ?
          AND strategy_id IS NOT NULL
          AND source_type IN ({_source_filter})
          AND trade_mode != 'live'
        GROUP BY strategy_id
    """, (_cutoff_30d,))
    by_strategy = [dict(r) for r in cursor2.fetchall()]
except:
    pass

# ─── 2b. 用 trade_records 作为 PnL/WL 的真实来源 (覆盖 playbook_instructions) ───
# 2026-07-02 修复: playbook_instructions.pnl 会被 ORPHAN_CLEANUP 幽灵交易污染,
# 且部分真实成交未回写到 pi. trade_records 是成交的唯一真实记录.
try:
    _tr_real = conn.execute(f"""
        SELECT {_NET}
        FROM trade_records
        WHERE trade_date >= ?
          AND source_type IN ({_source_filter})
          AND trade_mode != 'live'
          AND exit_reason IS NOT NULL
          AND exit_reason != ''
    """, (_cutoff_30d,)).fetchall()
    if _tr_real:
        _tr_pnl = sum(r[0] or 0 for r in _tr_real)
        _tr_w = sum(1 for r in _tr_real if (r[0] or 0) > 0)
        _tr_l = sum(1 for r in _tr_real if (r[0] or 0) < 0)
        total_pnl = _tr_pnl
        wins = _tr_w
        losses = _tr_l
        has_outcome = _tr_w + _tr_l
        win_rate = wins / has_outcome * 100 if has_outcome > 0 else 0
except Exception:
    pass

# ─── 2c. 毕业统计按 exit config_version 窗口归一化 (2026-07-20 修复M) ───
# cap_reduction / 毕业判定的 PnL/胜率维度 = 入场 edge × 退出执行. 退出参数一变,
# 同一批入场信号的胜率就变. 只取"当前退出参数版本"窗口内的已平仓样本, 消除
# "边积累边调参数"的污染 (IC 侧 forward-return 天然免疫, 不需窗口化).
versioned_stats = {}
try:
    from app.playbook.config import get_playbook_config as _get_pb_cfg
    _cur_ver = _get_pb_cfg().get_exit_config_version()
    _vrows = conn.execute(f"""
        SELECT {_NET}
        FROM trade_records
        WHERE trade_date >= ?
          AND source_type IN ({_source_filter})
          AND trade_mode != 'live'
          AND exit_reason IS NOT NULL
          AND exit_reason != ''
          AND json_extract(metadata_json, '$.config_version') = ?
    """, (_cutoff_30d, _cur_ver)).fetchall()
    _vp = sum((r[0] or 0) for r in _vrows)
    _vw = sum(1 for r in _vrows if (r[0] or 0) > 0)
    _vl = sum(1 for r in _vrows if (r[0] or 0) < 0)
    _vn = _vw + _vl
    versioned_stats = {
        "config_version": _cur_ver,
        "cnt": len(_vrows),
        "wins": _vw,
        "losses": _vl,
        "win_rate": round(_vw / _vn * 100, 1) if _vn > 0 else 0,
        "total_pnl": round(_vp, 2),
    }
except Exception as _ver_err:
    versioned_stats = {"error": str(_ver_err)}

# ─── 2d. Void 率趋势 (2026-07-20 #7) ───
# void = ghost/orphan 幽灵仓判定 (非真实交易, 毕业样本清洗时剔除, 见 OKR-SPEC δ.6 —
# 该 SPEC 的 OKR 周期已届满, 唯 δ.6 口径沿用, 见 docs/OKR-SPEC.md 文首注记).
# 口径: void / (win+loss+void) = 进入平仓终态的指令中被判幽灵的比例.
# void 率回升 = 执行/对账链路又在产幽灵仓, 是数据质量回退信号, 需趋势可见而非事后才发现.
void_trend = {"weekly": [], "overall_rate": None}
try:
    _void_rows = conn.execute("""
        SELECT strftime('%Y-W%W', date) AS wk,
               SUM(CASE WHEN outcome IN ('win','loss','void') THEN 1 ELSE 0 END) AS closed,
               SUM(CASE WHEN outcome='void' THEN 1 ELSE 0 END) AS voids
        FROM playbook_instructions
        WHERE date >= date('now', '-42 days')
        GROUP BY wk ORDER BY wk
    """).fetchall()
    _v_total_closed = 0
    _v_total_void = 0
    for _vr in _void_rows:
        _wk, _closed, _voids = _vr[0], int(_vr[1] or 0), int(_vr[2] or 0)
        if _closed <= 0:
            continue
        _v_total_closed += _closed
        _v_total_void += _voids
        void_trend["weekly"].append({
            "week": _wk, "closed": _closed, "voids": _voids,
            "void_rate": round(_voids / _closed * 100, 1),
        })
    if _v_total_closed > 0:
        void_trend["overall_rate"] = round(_v_total_void / _v_total_closed * 100, 1)
except Exception as _void_err:
    void_trend["error"] = str(_void_err)

# ─── 3. IC ───
# Phase 0 (2026-06-29): Playbook IC 是 Playbook 策略 end-to-end 的 SSOT；
# Signal IC 仅作为 VotingCoordinator 模型的诊断参考。
# 此前把 Signal IC 的 T+5 错误映射为 T+3，已修正。
ic_data = {
    "playbook": {
        "ic_t1": None, "ic_t3": None, "ic_t5": None,
        "sample_t1": 0, "sample_t3": 0, "sample_t5": 0,
        "graduation": "UNKNOWN",
    },
    "signal": {"ic_t1": None, "ic_t5": None, "sample_t1": 0, "sample_t5": 0},
}

# Playbook IC (SSOT): 基于实际生成的 Playbook 指令
from app.playbook.ic_tracker import compute_playbook_ic, _check_graduation
try:
    _pb_ic_report = compute_playbook_ic(db, days=30)
    _pb_grad = _check_graduation(_pb_ic_report)
    ic_data["playbook"] = {
        "ic_t1": _pb_ic_report.get("overall_ic_t1"),
        "ic_t3": _pb_ic_report.get("overall_ic_t3"),
        "ic_t5": _pb_ic_report.get("overall_ic_t5"),
        "sample_t1": _pb_ic_report.get("sample_t1", 0),
        "sample_t3": _pb_ic_report.get("sample_t3", 0),
        "sample_t5": _pb_ic_report.get("sample_t5", 0),
        # KR1 修尺子 (2026-08-24): 按日聚类成对输出 (宪章第 9 律).
        # 头条采信口径 = 日聚类值; pooled 值仅诊断参考 (DEB-20260823: pooled 显著性夸大~8倍).
        "day_clustered": _pb_ic_report.get("day_clustered", {}),
        "graduation": _pb_grad.get("verdict", "UNKNOWN"),
        "graduation_checks": _pb_grad.get("checks", {}),
        # ⚠️ DEPRECATED (2026-08-24): confidence-based 毕业判据已停用 (测常量无意义),
        # 门禁本体保持现状; 新判据待按日聚类尺子预注册+用户批准.
        "graduation_deprecated": True,
    }
except Exception as e:
    ic_data["playbook"]["error"] = str(e)

# Signal IC (诊断): 基于所有 VotingCoordinator 信号快照
from app.analysis.signal_ic_tracker import get_ic_summary
try:
    _ic_t1 = get_ic_summary(min_signals=20, forward_col="fwd_ret_t1", days_back=60)
    _ic_t5 = get_ic_summary(min_signals=20, forward_col="fwd_ret_t5", days_back=60)
    ic_data["signal"] = {
        "ic_t1": _ic_t1.get("ic_mean") if _ic_t1.get("status") == "ok" else None,
        "sample_t1": _ic_t1.get("n_signals", 0),
        "ic_t5": _ic_t5.get("ic_mean") if _ic_t5.get("status") == "ok" else None,
        "sample_t5": _ic_t5.get("n_signals", 0),
    }
except Exception as e:
    ic_data["signal"]["error"] = str(e)

# ─── 4. Config ───
config_state = {}
try:
    from app.core.config_container import get_config
    cfg = get_config()
    config_state = {
        "enabled": cfg.get_dotted('playbook.enabled', False),
        "execute_mode": cfg.get_dotted('playbook.execute_mode', 'simulate'),
        "auto_execute": cfg.get_dotted('playbook.auto_execute', False),
    }
except:
    config_state = {"error": "config unavailable"}

# ─── 5. Factor Stats ───
factor_stats = {}
try:
    singles = db.get_factor_stats(level="single")
    if singles:
        factor_stats = {
            "positive": [f for f in singles if f.get('verdict') == 'positive'],
            "negative": [f for f in singles if f.get('verdict') == 'negative'],
            "observing": [f for f in singles if f.get('verdict') == 'observing'],
        }
except:
    pass

# ─── 6. OKR Scorecard ───
OKR_TARGETS = {
    "win_rate": 55,
    "weekly_pnl": 1000,
    "min_trades": 30,
    "ic_t3": 0.05,
    "max_drawdown": 5,
}

def calc_okr_score():
    """Calculate overall OKR progress (0-100)"""
    scores = {}
    # KR1: Win rate
    scores["kr1_win_rate"] = min(100, win_rate / OKR_TARGETS["win_rate"] * 100) if has_outcome >= 5 else 0
    # KR2: Weekly PnL — 取最近 2 周(而非 PnL 最高的 2 周), 与 OKR "连续 2 周" 目标对齐
    sorted_weeks = sorted(weekly_pnl.keys(), reverse=True)
    recent_weeks = [weekly_pnl[wk] for wk in sorted_weeks[:2]]
    avg_weekly = sum(w['pnl'] for w in recent_weeks) / len(recent_weeks) if recent_weeks else 0
    scores["kr2_weekly_pnl"] = max(0, min(100, avg_weekly / OKR_TARGETS["weekly_pnl"] * 100))
    # KR3: Sample size
    scores["kr3_sample"] = min(100, has_outcome / OKR_TARGETS["min_trades"] * 100)
    # KR4: IC — 使用 Playbook IC(T+3) 作为 SSOT
    ic_t3 = ic_data["playbook"].get("ic_t3")
    scores["kr4_ic"] = min(100, max(0, ic_t3 / OKR_TARGETS["ic_t3"] * 100)) if ic_t3 else 0
    # KR5: Trigger rate
    trigger_rate = triggered / non_skip * 100 if non_skip > 0 else 0
    scores["kr5_trigger"] = min(100, trigger_rate / 50 * 100)
    
    overall = sum(scores.values()) / len(scores)
    return overall, scores

overall_score, kr_scores = calc_okr_score()

# ─── 6b. OKR-2026-09 KR 集 (现行判据, 与上方 8 月旧块**并列不替代**) ───
# 病灶 (修尺子批③, 2026-09-06 用户裁「需要」): 上方 calc_okr_score 判的是 **OKR-2026-08 的
# KR 集** (win_rate 55% / weekly_pnl $1000 / sample 30 / IC(T+3) 0.05 / trigger 50%), 而
# docs/OKR-2026-08.md 已于 2026-08-23 **提前结算为 SUPERSEDED**; 现行判据是
# docs/OKR-2026-09.md 的 KR1 修尺子 / KR2 参数冻结 / KR3 预注册找 edge / KR4 实盘硬门 /
# KR5 学费明码 ⇒ 头条 overall_score 与现行判据**不同源** (KR2/KR4 恒 0 是已废目标算出来的)。
# 治法=并列不改旧块: kr_scores/overall_score 键名被 scripts/okr_query.py:58,168 消费, 且
# okr_history.jsonl 有时序连续性 — 改键 = 断消费方 + 造假时序。
# 判分逻辑拆到 scripts/okr_2026_09_scoring.py (纯函数): 本脚本是**模块级执行体** (import 即跑
# 全部 SQL), 契约测试钉不到它 (批② 只能抽源码字符串); 拆出去才钉得到。只读载体, 零写副作用。
_OKR09_UNAVAILABLE = "⚠️ OKR-2026-09 判分不可用: "
try:
    from scripts.okr_2026_09_scoring import collect_and_score as _collect_2026_09
    from scripts.okr_2026_09_scoring import render_lines as _render_2026_09
    okr_2026_09 = _collect_2026_09(
        repo_root=Path('/opt/ftseek/FTSeek-US'),
        ic_playbook=ic_data["playbook"],
        graduation=ic_data["playbook"].get("graduation"),
        graduation_deprecated=ic_data["playbook"].get("graduation_deprecated"),
        trade_stats=trade_stats,
        window_days=30,          # trade_stats 的取样窗 (_cutoff_30d), KR5 笔频外推用
        gate_redefined=False,    # 新毕业判据尚未预注册+用户批准 (OKR-2026-09 KR4); 批准后才可翻 True
        # KR2 修订判据 (2026-09-06 用户授权代裁): 判分单位=主 cohort 窗, 已结算笔数取本脚本
        # 既有的"毕业样本 (当前退出参数版本)"聚合 — 不新增 SQL (第四把尺子纪律)。
        cohort_settled=(versioned_stats or {}).get("cnt")
        if isinstance(versioned_stats, dict) and "error" not in versioned_stats else None,
        today=datetime.now().date().isoformat(),
    )
    okr_2026_09_lines = _render_2026_09(okr_2026_09)
except Exception as _okr09_err:  # 显性留痕 (except:pass 棘轮 955 只降不升): 判分炸了必须看得见
    okr_2026_09 = {"error": f"{type(_okr09_err).__name__}: {_okr09_err}"}
    okr_2026_09_lines = [_OKR09_UNAVAILABLE + okr_2026_09["error"]]

# ─── 6c. OKR-2026-10 现行状态块 (2026-10-16 推送联动修复, 用户令「确保联动感知推送」) ───
# 只读组装, **非判分引擎** — `okr_2026_10_scoring` 判分模块仍是 OKR-2026-10 落地债,
# 本块不替代它, 只把权威读数带进推送面 (判据一字不立, 读数全部回源到既有权威产物)。
_okr10 = {"okr": "OKR-2026-10", "window": "2026-09-17→2026-10-31",
          "as_of": datetime.now().date().isoformat()}
try:
    _inv = json.loads((Path('/opt/ftseek/FTSeek-US/data/invariant_check_latest.json'))
                      .read_text(encoding="utf-8"))
    _cf = (_inv.get("config_freeze") or {})
    _oct = _cf.get("oct_cohort") or {}
    _post = _cf.get("post_ca4487") or {}
    _okr10["KR1"] = {
        "status": "BROKEN_终局 (10-08 退出哈希换版, n_unique=%s; DEB-20261009 §30 方案A "
                  "10-10 用户裁: 不豁免不移窗, 开 post_ca4487 观测窗)" % _oct.get("n_unique"),
        "post_ca4487_n": _post.get("n_closed"), "post_ca4487_target": _post.get("n_target"),
        "authority": "data/invariant_check_latest.json::config_freeze"}
except Exception as _e10:  # 显性留痕: 读不到权威产物必须可见
    _okr10["KR1"] = {"error": f"{type(_e10).__name__}: {_e10}"}
try:
    _kr3 = json.loads((Path('/opt/ftseek/FTSeek-US/data/shadow/kr3_final_adjudication.json'))
                      .read_text(encoding="utf-8"))
    _legs = {l.get("direction"): l.get("verdict") for l in _kr3.get("legs", [])}
    _okr10["KR3"] = {"status": "达成 2026-10-15 (两腿 FALSIFIED=证据产出, 用户裁停采)",
                     "legs": _legs, "authority": "data/shadow/kr3_final_adjudication.json"}
except Exception as _e10b:
    _okr10["KR3"] = {"error": f"{type(_e10b).__name__}: {_e10b}"}
_okr10["KR2"] = {"status": "攒样中 (净 bps/笔三态结论须 n=200 已结算)",
                 "progress_note": "见 trade_records 节 (PLAYBOOK_SIM 已结算计数)",
                 "authority": "scripts/okr_status.py 既有聚合 (不新增尺子)"}
_okr10["KR4"] = {"status": "四锁状态见 okr_2026_09.locks (同一组锁)",
                 "locks": (okr_2026_09 or {}).get("locks") if isinstance(okr_2026_09, dict) else None}
_okr10["note"] = ("判分模块 okr_2026_10_scoring 未建 (OKR-2026-10 落地债#2); "
                  "本块=权威读数指针, 结算判据以 docs/OKR-2026-10.md 为准")


# ─── OUTPUT ───
print("=" * 65)
print("  OKR STATUS REPORT — " + datetime.now().strftime('%Y-%m-%d %H:%M'))
print("=" * 65)

print(f"\n🎯 OVERALL PROGRESS: {overall_score:.0f}%  "
      f"← OKR-**2026-08** KR 集 (2026-08-23 已 SUPERSEDED, 仅续算; 现行判据=OKR-2026-10 见最下方 10 块)")
for kr, score in kr_scores.items():
    bar = "█" * int(score/5) + "░" * (20 - int(score/5))
    print(f"  {kr:<20} {bar} {score:.0f}%")

print()
for _line09 in okr_2026_09_lines:
    print(_line09)

# OKR-2026-10 现行状态块 (只读指针, 见 6c 注释)
print(f"\n🎯 OKR-2026-10 (现行, 窗 {_okr10['window']}, as_of {_okr10['as_of']})")
for _k in ("KR1", "KR2", "KR3", "KR4"):
    _v = _okr10.get(_k) or {}
    print(f"  {_k}: {_v.get('status') or _v.get('error')}")
print(f"  注: {_okr10['note']}")

print(f"\n📊 PIPELINE METRICS")
print(f"  Instructions:    {total} (SKIP: {skip_count}, active: {non_skip})")
print(f"  With entry_price: {with_entry}/{non_skip} ({with_entry/non_skip*100:.0f}%)" if non_skip > 0 else "  With entry_price: 0")
print(f"  Triggered:       {triggered}/{non_skip} ({triggered/non_skip*100:.1f}%)" if non_skip > 0 else "  Triggered: 0")
print(f"  Executed:        {executed}")
print(f"  Outcomes:        {has_outcome} (W:{wins} L:{losses} hold:{holding} miss:{missed})")

if has_outcome > 0:
    print(f"\n💰 PERFORMANCE")
    print(f"  Win Rate:  {win_rate:.1f}% (target: ≥55%)")
    print(f"  Total PnL: ${total_pnl:.2f}")
    if weekly_pnl:
        for wk in sorted(weekly_pnl.keys(), reverse=True)[:3]:
            s = weekly_pnl[wk]
            print(f"  {wk}: ${s['pnl']:.2f} ({s['wins']}W/{s['losses']}L)")

def _fmt_dc(dc):
    """日聚类值格式化: 日均IC (t=X, MDE=Y, n日) 可分辨标记"""
    if not dc or dc.get("day_ic_mean") is None:
        return f"日聚类: ⏳ 不足({(dc or {}).get('n_days', 0)}日)"
    flag = "可分辨✅" if dc.get("distinguishable") else "不可分辨"
    return f"日聚类: {dc['day_ic_mean']:+.4f} (t={dc['t']}, MDE={dc['mde']}, {dc['n_days']}日) {flag}"

print(f"\n📈 IC METRICS (pooled | 按日聚类成对, 宪章第9律: 无日聚类+MDE不予采信)")
_pb = ic_data['playbook']
_dc = _pb.get('day_clustered', {})
print(f"  [Playbook SSOT] IC(T+1): {_pb.get('ic_t1', 'N/A')} (n={_pb.get('sample_t1', 0)})  └ {_fmt_dc(_dc.get('t1'))}")
print(f"  [Playbook SSOT] IC(T+3): {_pb.get('ic_t3', 'N/A')} (n={_pb.get('sample_t3', 0)})  └ {_fmt_dc(_dc.get('t3'))}")
print(f"  [Playbook SSOT] IC(T+5): {_pb.get('ic_t5', 'N/A')} (n={_pb.get('sample_t5', 0)})  └ {_fmt_dc(_dc.get('t5'))}")
print(f"  Graduation: {_pb.get('graduation', 'UNKNOWN')} ⚠️旧判据已停用(测常量), 新判据待预注册")
print(f"  [Signal diag] IC(T+1): {ic_data['signal'].get('ic_t1', 'N/A')} (n={ic_data['signal'].get('sample_t1', 0)})")
print(f"  [Signal diag] IC(T+5): {ic_data['signal'].get('ic_t5', 'N/A')} (n={ic_data['signal'].get('sample_t5', 0)})")
if _pb.get("sample_t3", 0) < 30:
    print(f"  ⚠️ Playbook IC(T+3) 样本不足, 毕业门禁暂不可用; 待 backfill 积累")

if factor_stats:
    print(f"\n🔬 FACTORS")
    for f in factor_stats.get('positive', []):
        print(f"  ✅ {f.get('factor_key')}: WR={f.get('win_rate',0)*100:.0f}% ({f.get('total_trades',0)} trades)")
    for f in factor_stats.get('negative', []):
        print(f"  ❌ {f.get('factor_key')}: WR={f.get('win_rate',0)*100:.0f}% ({f.get('total_trades',0)} trades)")

if trade_stats and 'error' not in trade_stats:
    print(f"\n📋 TRADE RECORDS (30d)")
    print(f"  Total: {trade_stats.get('cnt', 0)}")
    print(f"  Wins: {trade_stats.get('wins', 0)}, Losses: {trade_stats.get('losses', 0)}")
    print(f"  PnL: ${trade_stats.get('total_pnl', 0):.2f}")
    if trade_stats.get('cnt_bps'):
        if trade_stats.get('bps_suspect'):
            print(f"  ⚠️ 尺寸归一口径 SUSPECT: |bps| > {_BPS_SUSPECT_LIMIT:,.0f} "
                  f"(毛 {trade_stats.get('gross_bps')} / 净 {trade_stats.get('net_bps')}) — "
                  f"量纲或括号错, 数字不可用, 先查 _NOTIONAL 展开")
        else:
            print(f"  尺寸归一 (n={trade_stats['cnt_bps']}, avg名义 ${trade_stats.get('avg_notional') or 0:,.0f}): "
                  f"毛 {trade_stats.get('gross_bps') or 0:+.2f}bps − 费 {trade_stats.get('fee_bps') or 0:.2f}bps "
                  f"= 净 {trade_stats.get('net_bps') or 0:+.2f}bps/笔")
            print(f"  (美元/笔跨 2026-08-06 sizing 变更不可比 — 名义额中位 $3,307→$5,737; bps 才是同口径)")

if versioned_stats and 'error' not in versioned_stats:
    print(f"\n🎓 GRADUATION SAMPLE (当前退出参数版本, 30d)")
    print(f"  config_version: {versioned_stats.get('config_version', '?')}")
    print(f"  Closed: {versioned_stats.get('cnt', 0)} (W:{versioned_stats.get('wins', 0)} L:{versioned_stats.get('losses', 0)})")
    print(f"  Win Rate: {versioned_stats.get('win_rate', 0):.1f}%  PnL: ${versioned_stats.get('total_pnl', 0):.2f}")
    print(f"  (毕业判定只取本窗口样本, 消除跨版本退出参数污染)")

if void_trend.get("weekly"):
    print(f"\n👻 VOID RATE TREND (幽灵仓占平仓终态比例, 6周)")
    for _w in void_trend["weekly"]:
        _flag = " ⚠️" if _w["void_rate"] >= 10 else ""
        print(f"  {_w['week']}: {_w['voids']}/{_w['closed']} ({_w['void_rate']:.1f}%){_flag}")
    print(f"  overall: {void_trend.get('overall_rate', 0)}% (void 不计入毕业样本; 回升=执行/对账链路在产幽灵仓)")

if by_strategy:
    print(f"\n📋 BY STRATEGY (30d)")
    for s in sorted(by_strategy, key=lambda x: x.get('total_pnl', 0), reverse=True):
        wr = s['wins'] / s['cnt'] * 100 if s['cnt'] > 0 else 0
        print(f"  {s.get('strategy_id','?')}: {s['cnt']} trades, WR={wr:.0f}%, PnL=${s.get('total_pnl',0):.2f}")

# ── Signal source attribution (voting_llm vs deep_analysis) ──
try:
    _snap_db_path = str(Path(get_db("ftseek_us")._get_path("ftseek_us")).parent / "signal_snapshots.db")
    conn.execute("ATTACH DATABASE ? AS snap", (_snap_db_path,))
    _attr_rows = conn.execute(f"""
        SELECT ss.strategy_id as signal_source,
               COUNT(*) as cnt,
               SUM(CASE WHEN COALESCE(tr.pnl_net, tr.realized_pnl) > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN COALESCE(tr.pnl_net, tr.realized_pnl) < 0 THEN 1 ELSE 0 END) as losses,
               SUM(COALESCE(tr.pnl_net, tr.realized_pnl)) as total_pnl
        FROM trade_records tr
        LEFT JOIN snap.signal_snapshots ss ON tr.decision_id = ss.decision_id
        WHERE tr.trade_date >= ?
          AND tr.source_type IN ({_source_filter})
          AND tr.trade_mode != 'live'
          AND tr.exit_reason IS NOT NULL AND tr.exit_reason != ''
        GROUP BY ss.strategy_id
        ORDER BY total_pnl DESC
    """, (_cutoff_30d,)).fetchall()
    if _attr_rows:
        print(f"\n🏷️ SIGNAL SOURCE ATTRIBUTION (30d)")
        for r in _attr_rows:
            d = dict(r)
            _w = d['wins'] or 0
            _l = d['losses'] or 0
            _wr = _w/(_w+_l)*100 if (_w+_l) > 0 else 0
            _src = d['signal_source'] or 'unlinked'
            print(f"  {_src:20s}: {d['cnt']:3d} trades, WR={_wr:.0f}%, PnL=${d['total_pnl'] or 0:.2f}")
    conn.execute("DETACH DATABASE snap")
except Exception as _attr_err:
    pass

print(f"\n⚙️ CONFIG")
for k, v in config_state.items():
    print(f"  {k}: {v}")

print("\n" + "=" * 65)

# Also output JSON for programmatic consumption
output = {
    "timestamp": datetime.now().isoformat(),
    "overall_score": round(overall_score, 1),
    "kr_scores": {k: round(v, 1) for k, v in kr_scores.items()},
    # 口径声明 (军规: 新落库字段带单位/来源声明): 旧块是 SUPERSEDED OKR 的续算值, 不是现行判据
    "kr_scores_caliber": ("OKR-2026-08 KR 集 (win_rate/weekly_pnl/sample/ic_t3/trigger), "
                          "该 OKR 2026-08-23 提前结算为 SUPERSEDED ⇒ 本块仅续算以保 "
                          "okr_query.py 消费面与 okr_history.jsonl 时序连续; **现行判据见 okr_2026_09**"),
    "okr_2026_09": okr_2026_09,
    "okr_2026_10": _okr10,
    "pipeline": {
        "total": total, "skip": skip_count, "active": non_skip,
        "with_entry": with_entry, "triggered": triggered, "executed": executed,
        "wins": wins, "losses": losses, "holding": holding, "missed": missed,
        "win_rate": round(win_rate, 1), "total_pnl": round(total_pnl, 2),
    },
    "ic": {
        "playbook": ic_data["playbook"],
        "signal": ic_data["signal"],
    },
    "factors": factor_stats,
    "trade_records": trade_stats,
    "graduation_sample": versioned_stats,
    "void_trend": void_trend,
    "by_strategy": by_strategy,
    "config": config_state,
}

data_dir = Path('/opt/ftseek/FTSeek-US/data')
data_dir.mkdir(exist_ok=True)

# Save latest
with open(data_dir / 'okr_latest.json', 'w') as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

# Append to history (one JSON per line)
# 2026-09-09 (REVIEW-20260909 F-3): 同日幂等 — 同日多次跑只留最新行
#   (此前纯追加, 157 行里 17 个日期重复, "与昨日 delta" 锚到当天临时跑)。
#   历史存量重复行不回填 (家法), 由读侧 okr_query.load_history 去重防护。
_hist_path = data_dir / 'okr_history.jsonl'
_today_key = output["timestamp"][:10]
_keep = []
if _hist_path.exists():
    with open(_hist_path) as f:
        for _ln in f:
            _ln = _ln.strip()
            if not _ln:
                continue
            try:
                if json.loads(_ln).get("timestamp", "")[:10] == _today_key:
                    continue  # 同日旧行被本轮替换
            except Exception as _bad_line:
                print(f"[okr_status] history 坏行保留 (不丢数据): {_bad_line}", file=sys.stderr)
            _keep.append(_ln)
_keep.append(json.dumps(output, ensure_ascii=False))
with open(_hist_path, 'w') as f:
    f.write('\n'.join(_keep) + '\n')

print("JSON saved to data/okr_latest.json + data/okr_history.jsonl")
