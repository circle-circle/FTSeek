#!/usr/bin/env python3
"""方向命中率记分牌 (2026-07-24) — 回答"LLM 到底看没看清方向"的信心仪表.

背景 (用户 2026-07-24): 对标的选择与指令方向没有底气。经查:
  - 方向信息其实已被生成 (挑战者辩论), 但分歧被降级为仓位折扣而非方向信号;
  - LLM 信号无法经典回测 (回放历史 LLM = 前视偏差), 正确形态是前向指令台账:
    每日指令冻结落库 + fwd_return_t1/t3 回填 —— 基建已在跑, 本脚本把它变成报表。

口径: playbook_instructions, direction IN (BUY, SELL_SHORT), fwd_return 方向调整
  (BUY 用原值, SELL_SHORT 取负)。命中 = 方向调整后 T+1 收益 > 0。
只读, 落 data/direction_scorecard_latest.json + 控制台表。

用法: ./venv/bin/python scripts/direction_hit_scorecard.py [--since 2026-07-01]
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "direction_scorecard_latest.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def q(conn, where: str, params=(), exec_only: bool = False):
    """返回 (n, hit_pct_t1, dir_ret_t1_pct, hit_pct_t3, dir_ret_t3_pct).

    fwd_return_t1/t3 在写入时已方向调整 (ic_tracker.py: sign*(close-entry)/entry),
    正=方向对。直接 >0 判命中, 不再二次 CASE direction (原实现二次调整反转了 SHORT)。
    exec_only=True: 交易口径 (仅已执行) — R1 (2026-08-02): 未执行行的 fwd 从
    市场从未到过的计划价起算 (触发条件化伪影), 机械虚高, 不可作交易结论。
    """
    ex = "AND executed=1" if exec_only else ""
    r = conn.execute(f"""
        SELECT COUNT(*) n,
          ROUND(AVG(CASE WHEN fwd_return_t1>0 THEN 1.0 ELSE 0 END)*100,1) hit1,
          ROUND(AVG(fwd_return_t1)*100,3) ret1,
          ROUND(AVG(CASE WHEN fwd_return_t3 IS NULL THEN NULL
                         WHEN fwd_return_t3>0 THEN 1.0 ELSE 0 END)*100,1) hit3,
          ROUND(AVG(fwd_return_t3)*100,3) ret3
        FROM playbook_instructions
        WHERE fwd_return_t1 IS NOT NULL AND direction IN ('BUY','SELL_SHORT') AND {where} {ex}
    """, params).fetchone()
    return dict(n=r[0], hit_t1=r[1], dir_ret_t1=r[2], hit_t3=r[3], dir_ret_t3=r[4])


def src_expr():
    """来源切分: raw_instruction_json 的 source 标记 (DA=deep_analysis默认无标/UE/topup)."""
    return """CASE
        WHEN raw_instruction_json LIKE '%topup_pool%' THEN 'topup'
        WHEN raw_instruction_json LIKE '%universe_expansion%' THEN 'UE'
        ELSE 'DA' END"""


def main() -> int:
    since = "2026-07-01"
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    conn = sqlite3.connect(str(DB))

    # RFC-062 G1-1 (2026-08-02): 滑点折算假设 — 已执行口径附折算后读数 (round-trip = 2×)
    try:
        from app.core.config_container import get_config as _gc
        _hc_bps = float(_gc().get_dotted("live_readiness.slippage_haircut_bps", 15))
    except Exception:
        _hc_bps = 15.0

    report = {"generated_at": datetime.now().isoformat(), "since": since,
              "benchmark": "命中=方向调整后 T+1 收益>0; 50%=掷硬币基线", "sections": {}}

    print(f"═══ 方向命中率记分牌 (since {since}, 前向台账非回测) ═══")
    print(f"{'切片':<26} {'n':>5} {'T+1命中%':>9} {'T+1方向收益%':>12} {'T+3命中%':>9} {'T+3收益%':>9}")
    print("-" * 78)

    def emit(section, label, where, params=(), exec_only=False):
        d = q(conn, f"date>=? AND {where}", (since, *params), exec_only=exec_only)
        if exec_only and d["dir_ret_t1"] is not None:
            d["dir_ret_t1_slip_adj"] = round(d["dir_ret_t1"] - 2 * _hc_bps / 100.0, 3)
        report["sections"].setdefault(section, {})[label] = d
        if d["n"]:
            _adj = f"  折算{_hc_bps:g}×2:{d['dir_ret_t1_slip_adj']:+.3f}" if "dir_ret_t1_slip_adj" in d else ""
            print(f"{label:<26} {d['n']:>5} {d['hit_t1']:>9} {d['dir_ret_t1']:>12} "
                  f"{str(d['hit_t3'] or '-'):>9} {str(d['dir_ret_t3'] or '-'):>9}{_adj}")

    # ── R1 (2026-08-02): 头条=交易口径 (仅已执行) ──
    #   混合口径把未执行信号的 fwd 从"市场从未到过的计划价"起算 (触发条件化伪影:
    #   7月实测 未执行BUY T+1 +3.08%/命中79.4% vs 已执行 −0.41%/42.6%),
    #   使头条虚高 ~13pp。交易结论只准引用本节; 下方混合口径仅作信号层对照。
    print("【头条·交易口径 (仅已执行 executed=1) — 唯一可作交易结论的读数】")
    emit("headline_exec", "已执行·全部", "1=1", exec_only=True)
    emit("headline_exec", "已执行·BUY", "direction='BUY'", exec_only=True)
    emit("headline_exec", "已执行·SELL_SHORT", "direction='SELL_SHORT'", exec_only=True)
    for s in ("DA", "UE", "topup"):
        emit("headline_exec", f"已执行·{s}", f"({src_expr()})=?", (s,), exec_only=True)
    _mix = q(conn, "date>=? AND 1=1", (since,))
    _exe = report["sections"]["headline_exec"]["已执行·全部"]
    if _mix["n"] and _exe["n"] and _mix["hit_t1"] is not None and _exe["hit_t1"] is not None:
        _gap = round(_mix["hit_t1"] - _exe["hit_t1"], 1)
        report["headline"] = {"caliber": "executed_only", **_exe,
                              "mixed_caliber_hit_t1": _mix["hit_t1"],
                              "artifact_inflation_pp": _gap,
                              "note": "混合口径虚高=触发条件化伪影 (未执行 fwd 从未到达的计划价起算)"}
        print(f"  ⚠ 口径伪影: 混合口径 T+1命中 {_mix['hit_t1']}% vs 已执行 {_exe['hit_t1']}% "
              f"→ 虚高 {_gap}pp (交易结论只用已执行口径)")
    print("-" * 78)
    print("【信号口径对照 (含未执行, 有触发条件化伪影, 不作交易结论)】")

    emit("total", "全部", "1=1")
    emit("direction", "BUY", "direction='BUY'")
    emit("direction", "SELL_SHORT", "direction='SELL_SHORT'")
    # 来源切分 (跨策略污染纪律: topup/UE/DA 分开看)
    for s in ("DA", "UE", "topup"):
        emit("source", f"来源:{s}", f"({src_expr()})=?", (s,))
    # D4 (2026-07-24, DEB §十-b): topup 盘前/盘中窗切分 — 盘中窗预注册判据:
    #   n≥30 前不下结论; 命中<48% 或期望显著负 → 关盘中窗 (intraday_start=end 一行回滚)
    emit("source", "topup·盘中",
         "raw_instruction_json LIKE '%topup_pool%' AND raw_instruction_json LIKE '%\"timing\": \"intraday\"%'")
    emit("source", "topup·盘前",
         "raw_instruction_json LIKE '%topup_pool%' AND raw_instruction_json NOT LIKE '%\"timing\": \"intraday\"%'")
    # 辩论共识切分 (conf 0-100 制: <=40 分歧 / 41-75 部分一致 / >75 高确认)
    emit("debate", "分歧(conf<=40)", "confidence<=40")
    emit("debate", "部分一致(40-75]", "confidence>40 AND confidence<=75")
    emit("debate", "高确认(>75)", "confidence>75")
    # 2026-08-01 (P0.2 conf 校准, X1 前置): 逐离散值校准表 — conf 仅六档 {35,50,70,72,88,90},
    #   每档给实证命中/收益, 即诚实的校准曲线; ladder_direction 判定阶梯是否单调。
    print("-" * 78)
    _cal_rows = []
    for _cv, in conn.execute(
            "SELECT DISTINCT confidence FROM playbook_instructions "
            "WHERE date>=? AND fwd_return_t1 IS NOT NULL AND direction IN ('BUY','SELL_SHORT') "
            "AND confidence IS NOT NULL ORDER BY 1", (since,)):
        d = q(conn, "date>=? AND confidence=?", (since, _cv))
        _cal_rows.append({"conf": _cv, **d})
        if d["n"]:
            print(f"{'conf=' + str(_cv):<26} {d['n']:>5} {d['hit_t1']:>9} {d['dir_ret_t1']:>12} "
                  f"{str(d['hit_t3'] or '-'):>9} {str(d['dir_ret_t3'] or '-'):>9}")
    _valid = [r for r in _cal_rows if (r["n"] or 0) >= 5 and r["hit_t1"] is not None]
    # 阶梯判定对齐预注册三桶 (分歧<=40 vs 高确认>75, 与 debate 节同口径) —
    # 线性秩相关会掩盖"中间高两头低"等非单调形态 (08-01 实测即此形态, 秩相关误判 calibrated)。
    _lo = q(conn, "date>=? AND confidence<=40", (since,))
    _hi = q(conn, "date>=? AND confidence>75", (since,))
    if (_lo["n"] or 0) >= 5 and (_hi["n"] or 0) >= 5:
        _ladder = "calibrated(高确认>分歧)" if _hi["hit_t1"] > _lo["hit_t1"] else "inverted(高确认<=分歧)"
    else:
        _ladder = "insufficient_data"
    report["sections"]["calibration"] = {
        "per_conf_value": _cal_rows,
        "low_bucket_conf_le40": _lo,
        "high_bucket_conf_gt75": _hi,
        "ladder_direction": _ladder,
        "note": "P0.2 校准标尺: 每档 n>=5 才参与阶梯判定; 倒挂=conf 不能作入场门 (X1 前置未达标)",
    }
    print(f"  → 阶梯判定: {_ladder} (仅 n>=5 档参与)")
    # 2026-07-25 (信息源复盘): debate_outcome 是隐藏分层器 (分歧BUY 67% vs 弃权 47%) — 固定成周度切片;
    #   SHORT 相关 outcome 历史全烂 (分歧SHORT 33%/-2.85%, 一致SHORT 0%/-4.61%), 监控是否持续
    emit("debate_outcome", "弃权(一方SKIP)", "raw_instruction_json LIKE '%一方弃权%'")
    emit("debate_outcome", "方向分歧·BUY", "raw_instruction_json LIKE '%方向分歧(BUY%'")
    emit("debate_outcome", "方向分歧·SHORT", "raw_instruction_json LIKE '%方向分歧(SELL_SHOR%'")
    emit("debate_outcome", "2模型一致", "raw_instruction_json LIKE '%2模型一致%'")
    # 2026-08-01 (Simpson 校正): 同四切片按 executed=1 重读 — 混合口径"弃权 63.9%"掩盖
    #   已执行45.5%/未执行86.8% 的限价单逆选择结构 (未成交 BUY=价格已跑远的纸面对单).
    #   成交口径才是交易相关读数; 上四行保留为信号口径对照, 两口径并排防再误读.
    emit("debate_outcome_exec", "已执行·弃权", "executed=1 AND raw_instruction_json LIKE '%一方弃权%'")
    emit("debate_outcome_exec", "已执行·分歧·BUY", "executed=1 AND raw_instruction_json LIKE '%方向分歧(BUY%'")
    emit("debate_outcome_exec", "已执行·分歧·SHORT", "executed=1 AND raw_instruction_json LIKE '%方向分歧(SELL_SHOR%'")
    emit("debate_outcome_exec", "已执行·2模型一致", "executed=1 AND raw_instruction_json LIKE '%2模型一致%'")
    # 2026-07-25 (信息源复盘): UE 按 self_tier 分层 — 八月终裁事件赛道 (HARD_EVENT 专营)
    #   判据消费端; 写侧 07-22 已落 origin.self_tier, 此处补读侧。n<30 不下结论。
    emit("ue_tier", "UE·HARD_EVENT", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%HARD_EVENT%'")
    emit("ue_tier", "UE·MARKET_CORROB", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%MARKET_CORROBORATED%'")
    emit("ue_tier", "UE·THESIS_ONLY", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%THESIS_ONLY%'")
    emit("ue_tier", "UE·tier未落(旧)", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json NOT LIKE '%self_tier%'")
    # 2026-07-25 (E1, DEB-da-ue-pe-agent-optimization): prompt_version 自动 A/B 切片 —
    #   每次 prompt 变更自动可判, 不再靠人工回忆"哪天改的 prompt"。n<30 不下结论。
    emit("prompt_ver", "UE·v2.1", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%v2.1%'")
    emit("prompt_ver", "UE·v2.2", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%v2.2%'")
    # 2026-07-25 (P2 rubric 判据 Q2): rubric_score 高低桶 vs 命中 — 与 conf 相关性对比的消费端
    emit("rubric", "UE·rubric≥3", "raw_instruction_json LIKE '%universe_expansion%' AND (raw_instruction_json LIKE '%\"rubric_score\": 3%' OR raw_instruction_json LIKE '%\"rubric_score\": 4%')")
    emit("rubric", "UE·rubric≤2", "raw_instruction_json LIKE '%universe_expansion%' AND (raw_instruction_json LIKE '%\"rubric_score\": 0%' OR raw_instruction_json LIKE '%\"rubric_score\": 1%' OR raw_instruction_json LIKE '%\"rubric_score\": 2%')")
    # 2026-07-25 (A2 self-critique shadow 判分): DROP/DOWNGRADE vs KEEP 的 fwd 对比 —
    #   若 DROP 组显著更差 → 再裁升级实拦 (那才是机制变更)
    emit("critique", "UE·critique:KEEP", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%\"verdict\": \"KEEP\"%'")
    emit("critique", "UE·critique:DOWN/DROP", "raw_instruction_json LIKE '%universe_expansion%' AND (raw_instruction_json LIKE '%\"verdict\": \"DROP\"%' OR raw_instruction_json LIKE '%\"verdict\": \"DOWNGRADE\"%')")
    # 2026-07-27 (S1, DEB-20260727 Q1): 首报渠道×fwd 切片 — S0 起 origin.info_channel 落真渠道;
    #   判据: n≥30/源, 某源命中≥55% 且另一源≤40% → 信息饮食权重调整立案
    for _ch in ("us_bloomberg", "us_cls", "us_cnbc_direct", "us_seekingalpha_direct",
                "us_wallstreetcn", "finnhub_market", "sec_8k", "none_matched"):
        emit("info_channel", f"UE·渠道:{_ch}",
             f"raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%\"info_channel\": \"{_ch}\"%'")
    # 2026-07-27 (军规: 新增字段必带基数自检 — "常量退化"已三例: DA conf罚35 / UE conf cap50 /
    #   info_source 恒 LLM_EVENT_REASONING): 字段落库但取值退化为常量 → 一切切片零区分度。
    #   此处对 info_channel 做基数体检: 样本≥10 但 distinct<2 → WARNING (S0 落库端失效信号)。
    _row = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT json_extract(raw_instruction_json,'$.origin.info_channel')) "
        "FROM playbook_instructions WHERE date>=? AND raw_instruction_json LIKE '%universe_expansion%' "
        "AND raw_instruction_json LIKE '%\"info_channel\"%'", (since,)).fetchone()
    if _row and _row[0] >= 10 and _row[1] < 2:
        print(f"  ⚠️ [基数自检] info_channel n={_row[0]} 但 distinct={_row[1]} — 疑似常量退化 (第四例?), "
              f"检查 S0 落库端 _annotate_news_channel")
        report.setdefault("warnings", []).append("info_channel_cardinality<2")
    # regime 切分 (07-24 起落库补全, 早期多为 NULL)
    emit("regime", "regime:cautious", "LOWER(market_regime) LIKE '%caution%'")
    emit("regime", "regime:risk_on", "LOWER(market_regime) LIKE '%risk_on%'")
    emit("regime", "regime:未记录", "market_regime IS NULL OR market_regime=''")
    # 逐周趋势 (方向能力是否稳定 — 波动大=无稳定趋势判读能力)
    print("-" * 78)
    for wk, in conn.execute(
            "SELECT DISTINCT strftime('%W',date) FROM playbook_instructions "
            "WHERE date>=? AND fwd_return_t1 IS NOT NULL ORDER BY 1", (since,)):
        emit("weekly", f"第{wk}周", "strftime('%W',date)=?", (wk,))

    # 2026-08-01 (用户裁"同意"四议题·议题四尺子固化): 以下四块统一走 app/review/direction_axes
    #   (周报共用同一口径源). 核心戒律: hit-rate 必须标注价格基准 (entry/open 成对).
    try:
        from app.review.direction_axes import (
            basis_quadrant, direction_regime_cross,
            quality_axis_status, weekly_direction_stability,
        )

        print("-" * 78)
        print("【方向×价格基准 四象限】 (已执行口径; open 基准=当日开盘起算, 最接近市价单可成交反事实)")
        quad = basis_quadrant(conn, since, executed_only=True)
        report["sections"]["basis_quadrant_exec"] = quad
        for d in ("BUY", "SELL_SHORT"):
            for basis in ("entry", "open"):
                a = quad.get(d, {}).get(basis)
                if a and a["n"]:
                    print(f"  {d:<10} {basis:<6} n={a['n']:>4}  T+1 {a['hit_t1']}%/均{a['mean_t1']}%"
                          f"  T+3 {a['hit_t3']}%/均{a['mean_t3']}%")

        print("-" * 78)
        print("【方向×regime 交叉表】 (已执行口径)")
        cross = direction_regime_cross(conn, since, executed_only=True)
        report["sections"]["direction_regime_cross_exec"] = cross
        for d, regimes in cross.items():
            for rg, a in sorted(regimes.items()):
                print(f"  {d:<10} {rg:<10} n={a['n']:>4}  T+1 {a['hit_t1']}%/均{a['mean_t1']}%"
                      f"  T+3 {a['hit_t3']}%/均{a['mean_t3']}%")

        print("-" * 78)
        print("【周度方向稳定性】 (已执行口径, 近5周; |streak|>=4 才称稳定)")
        stab = weekly_direction_stability(conn, weeks=5, executed_only=True)
        report["sections"]["weekly_direction_stability_exec"] = stab
        for d, s in stab.items():
            wk_str = " ".join(f"{w['start']}:n{w['n']}/{w['hit_t1']}%" for w in s["weeks"])
            print(f"  {d:<10} streak={s['streak']:+d} [{s['verdict']}]  {wk_str}")

        print("-" * 78)
        print("【事件质量轴计数器】 (rubric/critique; exec n≥30 才允许用于设门)")
        qa = quality_axis_status(conn, since, min_n=30)
        report["sections"]["quality_axis_status"] = qa
        for axis in ("rubric", "critique"):
            for bucket, dd in qa[axis].items():
                ex, un = dd.get("exec", {}), dd.get("unexec", {})
                flag = "✅可设门" if dd["gate_ready"] else "⏳攒样本"
                print(f"  {axis:<8} {bucket:<12} 已执行 n={ex.get('n', 0):>4} T+1 {ex.get('hit_t1')}%"
                      f" | 未执行 n={un.get('n', 0):>4} T+1 {un.get('hit_t1')}% | {flag}")

        # 2026-08-01 (议题三B): 候选级 rubric×结局 — signal_snapshots 全候选 (含 BLOCKED/SKIP,
        #   rubric_shadow_daily 落分, fwd_ret_t1 快照回填链已有), 攒样速度 ×5 于指令级.
        try:
            import sqlite3 as _sq2
            _snap = _sq2.connect(f"file:{ROOT/'data'/'signal_snapshots.db'}?mode=ro", uri=True)
            # 2026-08-02 (R5): UE 候选级接线后双源并读 — DA/UE 分开, 防跨策略污染.
            for _sid in ("deep_analysis", "universe_expansion"):
                _cand = {}
                for _r in _snap.execute("""
                    SELECT CASE WHEN factors_json LIKE '%"rubric_score": 4%'
                                 OR factors_json LIKE '%"rubric_score": 3%' THEN 'rubric≥3'
                                WHEN factors_json LIKE '%"rubric_score": 2%'
                                  OR factors_json LIKE '%"rubric_score": 1%'
                                  OR factors_json LIKE '%"rubric_score": 0%' THEN 'rubric≤2'
                                ELSE '未落' END b,
                           COUNT(*) n,
                           ROUND(100.0*AVG(CASE WHEN fwd_ret_t1>0 THEN 1 ELSE 0 END),1) hit,
                           ROUND(100.0*AVG(fwd_ret_t1),3) mean
                    FROM signal_snapshots
                    WHERE trade_date >= ? AND strategy_id=? AND fwd_ret_t1 IS NOT NULL
                    GROUP BY 1""", (since, _sid)):
                    _cand[_r[0]] = {"n": _r[1], "hit_t1": _r[2], "mean_t1": _r[3]}
                    print(f"  候选级[{_sid[:6]}] {_r[0]:<12} n={_r[1]:>4} T+1 {_r[2]}%/均{_r[3]}%"
                          + (" | ✅可设门" if _r[0] != "未落" and _r[1] >= 30 else ""))
                report["sections"].setdefault("quality_axis_candidates", {})[_sid] = _cand
            # UE critique verdict×结局 (R5 新增读数 — 影子判官区分度首读 2026-08-02 倒挂,
            #   n<30 攒样中, 显著前不得实拦)
            for _r in _snap.execute("""
                SELECT CASE WHEN factors_json LIKE '%"verdict": "KEEP"%' THEN 'KEEP'
                            WHEN factors_json LIKE '%"verdict": "DROP"%'
                              OR factors_json LIKE '%"verdict": "DOWNGRADE"%' THEN 'DOWN/DROP'
                            ELSE '无critique' END b,
                       COUNT(*) n,
                       ROUND(100.0*AVG(CASE WHEN fwd_ret_t1>0 THEN 1 ELSE 0 END),1) hit,
                       ROUND(100.0*AVG(fwd_ret_t1),3) mean
                FROM signal_snapshots
                WHERE trade_date >= ? AND strategy_id='universe_expansion' AND fwd_ret_t1 IS NOT NULL
                GROUP BY 1""", (since,)):
                print(f"  UE批判[{_r[0]:<8}] n={_r[1]:>4} T+1 {_r[2]}%/均{_r[3]}%"
                      + (" | ⚠️倒挂" if _r[0] == "DOWN/DROP" and (_r[2] or 0) > 50 else ""))
                report["sections"].setdefault("ue_critique_candidates", {})[_r[0]] = \
                    {"n": _r[1], "hit_t1": _r[2], "mean_t1": _r[3]}
            _snap.close()
        except Exception as _ce:
            print(f"  ⚠️ 候选级读取失败 (不影响): {_ce}")
    except Exception as _ax_err:
        print(f"  ⚠️ direction_axes 四块读取失败 (不影响上文切片): {_ax_err}")
        report.setdefault("warnings", []).append(f"direction_axes_error:{_ax_err}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n解读指引: ①交易结论只引用'头条·交易口径(仅已执行)', 混合口径有触发条件化伪影 "
          f"②任何切片 n<30 不下结论 ③50%±5 视为无边缘 "
          f"④关注'分歧vs高确认'差值与逐周稳定性, 而非单周绝对值")
    print(f"已落盘 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
