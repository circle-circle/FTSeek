#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PULLBACK_LIMIT/FADE_SHORT 影子次日重放 (DEB-20260901 Phase B).

口径: 对带结构化入场语义 (entry_archetype + confirmation) 的 DA 指令,
重放"若按确认位限价入场 (而非现状市价穿越)"的反事实路径:
  - 确认位在 TTL 窗内触及 → 成交价 = min(确认位, bar开盘) [BUY] / max [SHORT]
  - TTL 内未触及 → EXPIRED_UNFILLED (不进场, 机会成本用当日实际成交对照)
  - 出场: 先判指令 SL 触及, 否则 EOD 收盘价
  - 量价条件 (confirmation 仅价格+TTL) 未重放 → 结果为价格条件下界
数据源:
  - playbook_instructions.raw_instruction_json (archetype/confirmation)
  - data/shadow/pullback_limit_shadow.jsonl (触发上下文, 可选)
  - data/replay_bars/<SYM>_<date>.json ([time,o,h,l,c])
  - trade_records (实际执行结果对照)
用法: python3 scripts/cf_pullback_limit_replay.py [--date YYYY-MM-DD] [--json out]
      缺省 date = 上一交易日 (简单回退: 昨天, 周一跑周日无 bar 自然跳过)。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
# cron 以 `venv/bin/python scripts/cf_pullback_limit_replay.py` 调用 ⇒ sys.path[0]=scripts/,
# `from scripts.exit_path_replay import ...` 与其内部 `from app.playbook...` 均无法解析。
# 与 scripts/trading_day_check.py 同款 bootstrap (仓根入 path), 不改调用方式。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAR_DIR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"
SHADOW = REPO / "data" / "shadow" / "pullback_limit_shadow.jsonl"
OUT_JSONL = REPO / "data" / "shadow" / "pullback_limit_replay.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "pullback_limit_replay_latest.json"
# 多日窗口模式 (--from-date/--to-date) 独立落点: fresh 覆写, 重跑幂等,
# 不污染 cron 单日追加的 OUT_JSONL (否则同一行会被重复计入配对差)。
OUT_V2_JSONL = REPO / "data" / "shadow" / "pullback_limit_replay_v2.jsonl"
OUT_V2_LATEST = REPO / "data" / "shadow" / "pullback_limit_replay_v2_latest.json"


def load_bars(sym: str, date: str):
    p = BAR_DIR / f"{sym.replace('.', '_')}_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_shadow_rows(date: str):
    out, bad = {}, 0
    if not SHADOW.exists():
        return out
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = _loads(line)
        if r is None:
            bad += 1
            continue
        if r.get("date") != date:
            continue
        out.setdefault(r.get("inst_id"), {})[r.get("type", "trigger")] = r
    if bad:
        print(f"WARNING: pullback_limit_shadow.jsonl 有 {bad} 行解析失败已跳过")
    return out


def replay_one(sym, date, bars, direction, confirm, sl, qty, start_hm):
    """返回 dict: status/cf_entry/cf_entry_t/cf_exit/cf_exit_why/cf_pnl."""
    lvl = confirm.get("price_level")
    side = confirm.get("side") or ("above" if direction == "SHORT" else "below")
    try:
        ttl = int(confirm.get("ttl_min") or 15)
    except Exception:
        ttl = 15
    if not lvl or qty <= 0:
        return {"status": "skip_no_level"}
    sign = -1.0 if direction == "SHORT" else 1.0
    # TTL 窗: 从 start_hm 起 ttl 分钟 (start 缺省 09:31)
    def _hm(b):
        return b[0][11:16]
    end_h = int(start_hm[:2]) * 60 + int(start_hm[3:5]) + ttl
    end_hm = f"{end_h // 60:02d}:{end_h % 60:02d}"
    entry = None
    for b in bars:
        if _hm(b) < start_hm or _hm(b) >= end_hm:
            if entry is None and _hm(b) >= end_hm:
                break
            if entry is None:
                continue
        if side == "below" and b[3] <= lvl:
            entry = (_hm(b), min(lvl, b[1]))
            break
        if side == "above" and b[2] >= lvl:
            entry = (_hm(b), max(lvl, b[1]))
            break
    if entry is None:
        return {"status": "expired_unfilled", "confirm_level": lvl}
    et, ep = entry
    exit_px, exit_why = None, None
    if sl:
        for b in bars:
            if _hm(b) <= et:
                continue
            if direction == "SHORT" and b[2] >= sl:
                exit_px, exit_why = max(sl, b[1]), "SL"
                break
            if direction != "SHORT" and b[3] <= sl:
                exit_px, exit_why = min(sl, b[1]), "SL"
                break
    if exit_px is None:
        exit_px, exit_why = bars[-1][4], "EOD"
    cf_pnl = (exit_px - ep) * qty * sign
    return {"status": "entered", "confirm_level": lvl, "cf_entry": round(ep, 4),
            "cf_entry_t": et, "cf_exit": round(exit_px, 4), "cf_exit_why": exit_why,
            "cf_pnl": round(cf_pnl, 2)}


def _sign_test_one_sided(k: int, n: int) -> float:
    """单侧符号检验 p = P(X>=k | p=0.5)。n=0 → 1.0 (无证据)。"""
    if n <= 0:
        return 1.0
    from math import comb
    return sum(comb(n, i) for i in range(max(k, 0), n + 1)) / (2 ** n)


def _reachability(level, ref_price, atr_pct):
    """[2026-09-16 用户裁"要"] 确认位可达性刻度 — 纯观测字段, 不是判据。

    dist_atr = |确认位 − 触发时参考价| / (ATR% × 参考价) — 距离多少个日ATR。
    实证 (9/8-9/14 窗 27 笔): 距离<1% 成交率 75%, ≥2% 成交率 0% —
    C3 fill_rate 失败可能全部由"不可达桶"贡献; 裁决日需按桶分层判读,
    否则会把 DA 定价质量问题误判成限价机制成交能力问题。
    ref_price 优先级: 影子 trigger_price > 实盘成交价 > 指令锚价 (逐行标注来源)。
    返回 (dist_atr, dev_pct, ref_source:str|None) — 输入不全时相应分量为 None。
    """
    try:
        lvl = float(level)
    except (TypeError, ValueError):
        return None, None, None
    if not isinstance(ref_price, tuple):
        # 允许调用方传 (price, source) 二元组; 裸数值时来源未知
        ref_price = (ref_price, None)
    raw_ref, ref_source = ref_price
    try:
        ref = float(raw_ref)
    except (TypeError, ValueError):
        ref = None
    if not ref or ref <= 0:
        return None, round(lvl, 4), None
    dev_pct = round((lvl / ref - 1.0) * 100.0, 3)
    dist_atr = None
    try:
        _atr = float(atr_pct)
        if _atr and _atr > 0:
            dist_atr = round(abs(lvl - ref) / (_atr * ref), 3)
    except (TypeError, ValueError):
        pass
    return dist_atr, dev_pct, (ref_source or "unknown")


#: 采样口径双报 (2026-09-07 保真度专项步骤①): 实盘 10s 轮询的真值必落在两口径之间 —
#:   bar_extreme = 假设轮询看到了整根 bar 的极值 (观测**上界**)
#:   bar_close   = 假设轮询只看到收盘价 (观测**下界**)
#: 保真度门要求**两口径都过**, 配对差要求**两口径同号**, 不得只报过门/好看的那一个。
SAMPLE_CALIBERS = ("bar_extreme", "bar_close")
#: 武装滞后双报: 0 = 同根武装即触发 (乐观上界), 1 = 下一根才在场 (订单因果性下界)
ARM_LAG_CALIBERS = (0, 1)


#: ── 判据换轨 (2026-09-07): 尺子三级许可卡 ──────────────────────────────────
#: 为什么换轨: 实证两件事 ——
#:   ① 单一二元门 ("过没过保真度门") 会把**不同分辨率的结论**混为一谈。本窗 (9/1–9/4)
#:      实测: 每笔净额中位 |$11.86|, 重放单笔误差 MAE $14.87–22.32 ⇒ 比值 1.77–2.02,
#:      即**尺子的单笔误差是被测单笔量级的 2 倍**, 任何"这笔差 $X 是因为 Y"的逐笔归因
#:      都在噪声里; 但聚合层面偏差/MAE 仅 0.049–0.192、方向判对率 0.833 ⇒ 聚合与配对差
#:      可用 (近零偏差在求和中抵消, 且两臂共模)。同一把尺子, 逐笔不能用、聚合能用。
#:   ② 钱口径与标签口径各有盲区, 不能互相替换: 烂窗 7/11–8/14 标签一致率仅 0.384/0.444
#:      而钱口径照样过 (|bias|/MAE 0.054/0.009、符号一致 0.852/0.875) ⇒ 钱口径**无判别力**
#:      去替代标签门; 反过来标签门对经济等价标签 (SL_BREAKEVEN vs SL_TRAIL_LOCK, 钱差中位
#:      仅 $2.47) 过严。⇒ 三级并列, 各答各的问题。
#: 阈值预注册 (防"先看到数再定门"), 且沿用既有 0.80 形状, 不新造阈值语言。
#: 生效日纪律: 钱口径判据是在 2026-09-01…09-04 窗的数据上设计的, 用它裁该窗 = 凑门,
#:   故该日之前的窗只出**指示性**读数; 正式裁决点 = 2026-09-08 起的新窗 (n≥40 配对)。
MONEY_CRITERIA_BINDING_FROM = "2026-09-08"
#: L0 逐笔归因许可: 尺子单笔误差须优于被测单笔量级的 1/4 (分辨率 4 倍余量)
L0_MAE_TO_MEDIAN_NET_MAX = 0.25
#: L1 许可 (拆成 a/b 两级, 因为两种读数对"系统偏差"的要求**相反**, 混在一级会同时
#:   过松与过严 — 2026-09-07 实测: 同一当日域两口径 bias/MAE = 0.088 (bar_extreme) 与
#:   0.443 (bar_close), 若合成一级则整级判死, 连带把"两臂同尺配对差"这个**唯一要裁的
#:   读数**也一起否掉, 而该读数的偏差本来就共模抵消):
#:   L1a 配对差 (summary.caliber_a_same_replayer, 两臂同一重放器): 偏差同时作用于两臂 ⇒
#:       在差值里抵消 (代码既有注释「共模偏差抵消」), 故只要求方向判对率;
#:   L1b 聚合对账 (summary.caliber_b_vs_ledger, cf 重放 vs 账本真值): 偏差全额生效 ⇒
#:       还须要求系统偏差 ≪ 离散度, 否则"聚合差"是尺子偏差不是效应。
#: 两级共用同两个预注册阈值 (0.25 / 0.80), 不新造阈值语言; 本次拆分是**阈值归属**的
#:   修正, 在生效日 (2026-09-08) 之前完成, 不构成对已出裁决的追溯改门。
L1_BIAS_TO_MAE_MAX = 0.25
L1_SIGN_AGREEMENT_MIN = 0.80
#: L2 出场路径归因许可: 沿用既有标签门 (≥0.80 且无 n≥5 弱桶), 原样保留不新造
L2_REASON_MATCH_MIN = 0.80
#: 纪年守卫: 门域内某出场轨 n≥5 且复刻率=0 ⇒ 该轨在当前参数纪年下不可达 (或重放器未实现)
EPOCH_ZERO_MATCH_MIN_N = 5
#: 宪章「n<30 不结论」的样本门槛 (配对笔数)。许可卡发放的是**尺子分辨率**, 不是裁决资格 —
#:   两者必须分开写, 否则 9/8 之后单日 cron (实测 n=6) 会打出一张自称"效力: 正式"的卡,
#:   把小样本读数冒充成裁决依据。
MIN_N_FOR_CONCLUSION = 30
#: **本裁决**(ADR-035 / `entry_archetype_routing_enabled` 接线) 的预注册样本门槛。
#:   为什么与宪章底线 30 分开: 30 是"能不能说任何结论"的地板, 40 是"能不能裁这个开关"的
#:   门槛 (DEB-20260907 §4.9 预注册: 正式裁决点 = 2026-09-08 起新窗 n≥40 配对)。
#:   若不分开, 9/8 之后一个 n=30 的窗就会打出 verdict_eligible=True 的卡 ⇒ 提前裁决 =
#:   把"攒够样再判"换成"攒到地板就判", 正是生效日纪律要防的那类挪门。
ADR035_VERDICT_MIN_N_PAIRED = 40


def _v2_replay_arm(bars, direction, entry_price, entry_hm, sl, tp, qty, meta,
                   decision_id="", sample_source="bar_extreme", arm_lag_bars=1):
    """用离线重放器跑一臂 (两臂同尺)。失败返回 None, 不抛 (尺子不得阻断 cron)。

    decision_id: 供 DEAD_SIGNAL 的 Phase0 reentry 呼吸窗判据 (前缀 intraday_reentry);
                 不传就会把 reentry 仓的 120min 窗误算成 60min 窗 (全窗 19 笔)。
    """
    if not bars or not entry_price or not entry_hm or not qty:
        return None
    try:
        from scripts.exit_path_replay import replay_exit
        rep = replay_exit(
            direction=direction, entry_price=float(entry_price), entry_hm=str(entry_hm),
            initial_sl=sl, take_profit=tp, bars=bars, qty=float(qty),
            conditions=(meta or {}).get("conditions") or [],
            cohort=str((meta or {}).get("cohort") or "A"),
            horizon=str((meta or {}).get("horizon") or "short"),
            atr_abs=float((meta or {}).get("atr") or 0),
            atr_basis=str((meta or {}).get("atr_basis") or "?"),
            decision_id=decision_id, sample_source=sample_source,
            arm_lag_bars=arm_lag_bars)
        return rep if rep.get("status") == "replayed" else None
    except Exception as e:  # 重放器异常 → 该臂 null (与 v1 "未采集≠零" 同纪律)
        return {"status": f"replay_error:{type(e).__name__}"}


def build_rows(db, date):
    """单日: 取带 entry_archetype 的指令, 产出 v1 + v2 双臂行与保真度配对样本。

    v2 修正两处 v1 口径缺陷 (2026-09-07):
      ① 反事实臂此前只近似 "SL 或 EOD", 而实盘臂走完整退出引擎 (保本 ratchet /
         回撤锁利 / 早期紧 SL / TP / PRE_CLOSE) ⇒ 系统性低估反事实臂;
      ② 此前拿 cf_pnl(毛) 比 actual_net(=pnl_net, 净) ⇒ 又系统性高估反事实臂。
    现两臂都走 scripts/exit_path_replay.py 同一重放器、同为净额, 近似偏差共模抵消;
    实盘真值 (pnl_net/exit_reason) 只用于保真度校验, 不进配对差。
    """
    shadow = load_shadow_rows(date)
    rows = db.execute(
        """SELECT i.id, i.symbol, i.direction, i.entry_price, i.stop_loss,
                  i.take_profit, i.position_size, i.decision_id, i.raw_instruction_json,
                  i.atr_pct,
                  t.pnl_net AS actual_net, t.quantity AS actual_qty, t.exit_reason,
                  t.entry_price AS actual_entry_price, t.entry_time AS actual_entry_time,
                  t.exit_time AS actual_exit_time, t.metadata_json AS actual_meta
           FROM playbook_instructions i
           LEFT JOIN trade_records t
             ON t.id = (SELECT MIN(t2.id) FROM trade_records t2
                        WHERE t2.decision_id = i.decision_id AND i.decision_id != ''
                          AND t2.source_type = 'PLAYBOOK_SIM')
           WHERE i.date = ? AND i.direction IN ('BUY','SELL_SHORT')""", (date,)).fetchall()

    results, fidelity_pairs = [], []
    bad_raw = 0
    for r in rows:
        raw = _loads(r["raw_instruction_json"] or "{}")
        if raw is None:
            bad_raw += 1
            continue
        arch = raw.get("entry_archetype")
        if not arch:
            continue  # 旧指令无结构化语义, 不进池
        conf = raw.get("confirmation") or {}
        sym = r["symbol"]
        bars = load_bars(sym, date)
        direction = "SHORT" if r["direction"] == "SELL_SHORT" else "LONG"
        qty = r["actual_qty"] or r["position_size"] or 0
        sh = shadow.get(r["id"], {})
        trig = sh.get("trigger") or {}
        start_hm = "09:31"
        ts = trig.get("ts") or ""
        if ts:
            try:
                start_hm = datetime.fromisoformat(ts).strftime("%H:%M")
            except Exception:
                start_hm = "09:31"  # ts 异常回落开盘窗起点 (保守)
        meta = _loads(r["actual_meta"] or "{}") or {}
        if bars is None:
            results.append({"date": date, "symbol": sym, "inst_id": r["id"],
                            "entry_archetype": arch, "status": "no_bars"})
            continue
        rep = replay_one(sym, date, bars, direction, conf,
                         r["stop_loss"], qty, start_hm)
        # 2026-09-02 (DEB-20260902-mfe-caliber-and-shadow-zero 议题二): 无影子行的指令
        #   chase_cap 维度输出 null — "未采集"与"采集到零违例"语义相反, False 是假零。
        row = {
            "date": date, "symbol": sym, "inst_id": r["id"],
            "decision_id": r["decision_id"], "direction": direction,
            "entry_archetype": arch,
            "chase_cap_price": raw.get("chase_cap_price"),
            "chase_cap_violated": (bool(sh.get("chase_cap_violation"))
                                   if sh else None),
            "actual_executed": r["actual_net"] is not None,
            "actual_net": r["actual_net"],
            "actual_exit_reason": r["exit_reason"],
            **rep,
        }
        # [2026-09-16 用户裁"要"] 可达性刻度 (record-only, 供 C3 分层判读) —
        #   参考价优先级: 影子 trigger_price (触发瞬间真价) > 实盘成交价 > 指令锚价。
        _lvl = (conf or {}).get("price_level")
        _ref, _ref_src = (trig.get("trigger_price"), "shadow_trigger") \
            if trig.get("trigger_price") else (
                (r["actual_entry_price"], "actual_fill") if r["actual_entry_price"]
                else (r["entry_price"], "instruction_anchor"))
        _dist_atr, _dev_pct, _ = _reachability(_lvl, (_ref, _ref_src), r["atr_pct"])
        row["reach_dist_atr"] = _dist_atr
        row["reach_dev_pct"] = _dev_pct
        row["reach_ref_source"] = (_ref_src if _dist_atr is not None or _dev_pct is not None else None)

        # ── v2: 双臂同尺重放 ──
        did = r["decision_id"] or ""
        act_arm = None
        if r["actual_entry_price"] and r["actual_entry_time"]:
            act_kw = dict(bars=bars, direction=direction,
                          entry_price=r["actual_entry_price"],
                          entry_hm=str(r["actual_entry_time"])[11:16],
                          sl=meta.get("stop_loss") or r["stop_loss"],
                          tp=meta.get("take_profit") or r["take_profit"],
                          qty=qty or meta.get("_qty"), meta=meta, decision_id=did)
            act_arm = _v2_replay_arm(**act_kw)
            # 保真度 2×2 口径矩阵 (arm_lag × sample_source): 门读数不得挑口径, 故四格全跑。
            #   主口径 (lag=1, bar_extreme) 复用上面那次, 不重复计算。
            if r["actual_net"] is not None:
                same_day = str(r["actual_exit_time"] or "")[:10] == date
                for lag in ARM_LAG_CALIBERS:
                    for src in SAMPLE_CALIBERS:
                        cal = (act_arm if (lag, src) == (1, "bar_extreme")
                               else _v2_replay_arm(**act_kw, sample_source=src,
                                                   arm_lag_bars=lag))
                        if cal and cal.get("status") == "replayed":
                            fidelity_pairs.append({
                                "replay_reason": cal.get("exit_reason"),
                                "replay_net": cal.get("net_pnl"),
                                "db_reason": r["exit_reason"],
                                "db_net": r["actual_net"],
                                "same_day": same_day,
                                "arm_lag_bars": lag, "sample_source": src,
                                # 纪年守卫需要: 零复刻轨的**末次日期**决定最早可用窗起点
                                "date": date,
                            })
        if rep.get("status") == "entered":
            cf_arm = _v2_replay_arm(bars, direction, rep.get("cf_entry"),
                                    rep.get("cf_entry_t"),
                                    r["stop_loss"], r["take_profit"], qty, meta,
                                    decision_id=did)
            # 口径敏感性臂: 同一入场方式, 只换观测模型 ⇒ 差值的变化纯属尺子, 不是信号
            cf_arm_alt = _v2_replay_arm(bars, direction, rep.get("cf_entry"),
                                        rep.get("cf_entry_t"),
                                        r["stop_loss"], r["take_profit"], qty, meta,
                                        decision_id=did, sample_source="bar_close")
        else:
            # expired_unfilled / skip_no_level = 确认位路由下不持仓 ⇒ 净额 0 (不是 null)
            cf_arm = {"status": "not_entered", "net_pnl": 0.0, "exit_reason": None,
                      "mfe_pct": None}
            cf_arm_alt = dict(cf_arm)
        act_net_v2 = (act_arm or {}).get("net_pnl") if (act_arm or {}).get("status") == "replayed" else None
        cf_net_v2 = (cf_arm or {}).get("net_pnl") if cf_arm else None
        # 配对群要求实盘侧**已结算** (pnl_net 非空): 未平仓行的重放会被迫在 PRE_CLOSE/EOD
        #   假平并记一笔现实中不存在的亏损, 再与 cf 侧的"未持仓=0"相比 ⇒ 伪配对差
        #   (首版实测 US.UBER 9/3: db 未结算, 却贡献 +152.22 的 cf "优势")。
        paired = (cf_net_v2 is not None and act_net_v2 is not None
                  and r["actual_net"] is not None)
        row["v2"] = {
            "cf_net": cf_net_v2,
            "cf_exit_reason": (cf_arm or {}).get("exit_reason"),
            "cf_exit_hm": (cf_arm or {}).get("exit_hm"),
            "cf_mfe_pct": (cf_arm or {}).get("mfe_pct"),
            "act_replay_net": act_net_v2,
            "act_replay_reason": (act_arm or {}).get("exit_reason"),
            "act_replay_mfe_pct": (act_arm or {}).get("mfe_pct"),
            "paired_diff": round(cf_net_v2 - act_net_v2, 2) if paired else None,
            # 口径 (b): cf 侧 bar 重放 vs 实盘侧**账本真值**。两臂观测模型不同
            #   (bar 极值 vs 20s 轮询), 但实盘侧是真实发生的钱。与 (a) 并报成区间 —
            #   单点读数会被观测模型差异冒充成入场方式效应。
            "paired_diff_vs_db": (round(cf_net_v2 - r["actual_net"], 2)
                                  if (paired and r["actual_net"] is not None) else None),
            "paired": paired,
            "replay_status": {"cf": (cf_arm or {}).get("status"),
                              "actual": (act_arm or {}).get("status")},
        }
        # 观测口径敏感性 (sample_source=bar_close, arm_lag 仍为 1): 与上面同一批配对群,
        #   只换"重放器认为实盘看到了什么"。中位数符号若与 bar_extreme 不一致 ⇒ 该窗
        #   结论被观测模型决定, 不是被入场方式决定 ⇒ 不可判读 (见 summarize_v2)。
        act_alt_net = None
        if r["actual_entry_price"] and r["actual_entry_time"]:
            act_alt = _v2_replay_arm(bars=bars, direction=direction,
                                     entry_price=r["actual_entry_price"],
                                     entry_hm=str(r["actual_entry_time"])[11:16],
                                     sl=meta.get("stop_loss") or r["stop_loss"],
                                     tp=meta.get("take_profit") or r["take_profit"],
                                     qty=qty or meta.get("_qty"), meta=meta,
                                     decision_id=did, sample_source="bar_close")
            act_alt_net = (act_alt or {}).get("net_pnl") \
                if (act_alt or {}).get("status") == "replayed" else None
        cf_alt_net = (cf_arm_alt or {}).get("net_pnl") if cf_arm_alt else None
        paired_alt = (paired and cf_alt_net is not None and act_alt_net is not None)
        row["v2"]["sens_bar_close"] = {
            "cf_net": cf_alt_net, "act_replay_net": act_alt_net,
            "paired": bool(paired_alt),
            "paired_diff": (round(cf_alt_net - act_alt_net, 2) if paired_alt else None),
            "cf_exit_reason": (cf_arm_alt or {}).get("exit_reason"),
        }
        results.append(row)
    return results, fidelity_pairs, bool(shadow), bad_raw


def _epoch_guard(fidelity_pairs, window_start=None):
    """纪年守卫: 机械判断本窗是否**单一参数纪年** (自更新, 不写死 config 名单)。

    为什么必须有: 重放器只读**现行** config, 所以"当前纪年已关闭"的出场轨, 其历史真值行
    在本窗**不可能**被复刻; 混进窗里会把门读数压在一个不可修的上限下, 而读数看起来像
    "重放器不努力"。此前只能靠人工点名字典 (evaluability.dead_epoch_tracks 里的
    MFE_STALL_EXIT), 再关一条轨就漏判 ⇒ 改成从数据里算。

    判据 (预注册): 门域 (arm_lag_bars=1 ∧ 当日出场) 内某 db_reason
    n≥EPOCH_ZERO_MATCH_MIN_N 且复刻率=0 ⇒ 该轨"不可达"。零复刻有两种成因, 都使本窗
    不可判读, 守卫不区分: (a) 该轨在当前参数纪年已关闭; (b) 重放器根本没实现该轨。
    门域零样本 ⇒ single_epoch=None (fail-closed: 不宣称同纪年, 也不宣称跨纪年)。
    """
    def _d10(v):
        s = str(v or "")[:10]
        return s if len(s) == 10 else None

    gate = [p for p in fidelity_pairs
            if p.get("arm_lag_bars", 1) == 1 and p.get("same_day") is not False]
    per = {}
    for p in gate:
        key = str(p.get("db_reason") or "?")
        b = per.setdefault(key, {"n": 0, "match": 0, "first_date": None, "last_date": None})
        b["n"] += 1
        b["match"] += int(p.get("replay_reason") == p.get("db_reason"))
        dt = _d10(p.get("date"))
        if dt:
            b["first_date"] = min(b["first_date"] or dt, dt)
            b["last_date"] = max(b["last_date"] or dt, dt)
    for b in per.values():
        b["match_rate"] = round(b["match"] / b["n"], 3) if b["n"] else None
    tracks = dict(sorted(per.items(), key=lambda kv: -kv[1]["n"]))
    unreachable = {k2: v for k2, v in tracks.items()
                   if v["n"] >= EPOCH_ZERO_MATCH_MIN_N and v["match"] == 0}
    floor = None
    for v in unreachable.values():
        if v["last_date"]:
            nxt = (datetime.strptime(v["last_date"], "%Y-%m-%d")
                   + timedelta(days=1)).strftime("%Y-%m-%d")
            floor = nxt if floor is None else max(floor, nxt)

    # 域收窄会把弱桶藏起来 (实测 9/1–9/4: SL_INITIAL 全口径 n=8 复刻 0, 当日域只剩 n=4
    #   < 5 ⇒ 躲过既有弱桶规则)。不改门, 但必须披露 —— "无弱桶" ≠ "无坏轨"。
    allper = {}
    for p in fidelity_pairs:
        key = str(p.get("db_reason") or "?")
        b = allper.setdefault(key, {"n": 0, "match": 0})
        b["n"] += 1
        b["match"] += int(p.get("replay_reason") == p.get("db_reason"))
    masked = {k2: {"n_all_calibers": v["n"],
                   "match_rate_all_calibers": round(v["match"] / v["n"], 3),
                   "n_in_gate_domain": (tracks.get(k2) or {}).get("n", 0)}
              for k2, v in allper.items()
              if v["n"] >= EPOCH_ZERO_MATCH_MIN_N and v["match"] == 0
              and (tracks.get(k2) or {}).get("n", 0) < EPOCH_ZERO_MATCH_MIN_N}

    dates = sorted({_d10(p.get("date")) for p in fidelity_pairs} - {None})
    if not gate:
        single = None
        note = ("门域 (lag1 ∧ 当日出场) 内零配对样本 ⇒ 纪年不可判定 "
                "(fail-closed: 不宣称同纪年)")
    elif unreachable:
        single = False
        note = (f"门域内 {len(unreachable)} 条零复刻轨 "
                f"({', '.join(sorted(unreachable))}) ⇒ 本窗不可视为单一参数纪年; "
                f"换窗起点 = {floor or '不可计算 (配对样本缺 date 字段)'}")
    else:
        single = True
        note = (f"门域内无「n≥{EPOCH_ZERO_MATCH_MIN_N} 且复刻率=0」的出场轨 ⇒ "
                "本窗可视为单一参数纪年")
    return {"single_epoch": single,
            "window_start": window_start or (dates[0] if dates else None),
            "window_end": (dates[-1] if dates else None),
            "epoch_floor": floor, "unreachable_tracks": unreachable, "tracks": tracks,
            "masked_zero_match_tracks": masked,
            "rule": (f"门域内 db_reason n≥{EPOCH_ZERO_MATCH_MIN_N} 且复刻率=0 ⇒ 不可达轨; "
                     "epoch_floor = 不可达轨末次日期 + 1 天"),
            "note": note}


def _ruler_license(fid_gate, epoch, window_start, caliber_sign_agree, n_paired=0):
    """三级(四许可)卡: 把"过没过门"换成"这把尺子**能回答哪一级问题**"。

    L0  逐笔归因    : 能说"这一笔差 $X 是因为 Y"    — 尺子单笔误差 ≪ 单笔盈亏量级
    L1a 配对差      : 能说"整体上 A 比 B 好/差 $M"  — 两臂同尺 ⇒ 偏差共模抵消, 只要方向判对
    L1b 聚合对账    : 能说"重放的钱对得上账本"      — 对账本真值 ⇒ 偏差全额生效, 还须偏差≪离散
    L2  出场路径归因: 能说"因为触发了哪条退出规则"  — 既有标签门 (≥0.80 ∧ 无弱桶)

    各级互不替代 (判别力实证见模块常量段)。每级都要求**两个 sample_source 都过** —
    只报过门的那一格 = 挑口径。指标缺失 (None) 一律 fail-closed 判不许可。
    许可判定域 = 与既有保真度门同域 (arm_lag_bars=1 ∧ 当日出场), 预注册; 全日域读数见
    summary.fidelity_matrix, 只作披露不参与许可 (换域即挑口径)。
    """
    def _obs(key):
        return {k2: v.get(key) for k2, v in fid_gate.items()}

    def _all_within(d, ok):
        vals = list(d.values())
        return bool(vals) and all(x is not None and ok(x) for x in vals)

    def _worst(d, higher_is_worse=True):
        got = [x for x in d.values() if x is not None]
        if not got:
            return None
        return max(got) if higher_is_worse else min(got)

    o_l0 = _obs("mae_over_median_abs_net")
    o_bias = _obs("bias_to_mae_ratio")
    o_sign = _obs("sign_agreement_rate")
    o_match = _obs("match_rate")
    sign_ok = _all_within(o_sign, lambda x: x >= L1_SIGN_AGREEMENT_MIN)
    l0 = _all_within(o_l0, lambda x: x <= L0_MAE_TO_MEDIAN_NET_MAX)
    l1a = sign_ok
    l1b = sign_ok and _all_within(o_bias, lambda x: x <= L1_BIAS_TO_MAE_MAX)
    l2 = bool(fid_gate) and all(v.get("pass") for v in fid_gate.values())
    binding = bool(window_start) and window_start >= MONEY_CRITERIA_BINDING_FROM
    if epoch.get("single_epoch") is False:
        status = "NOT_SINGLE_EPOCH"
    elif not binding:
        status = "PROVISIONAL_INDICATIVE_ONLY"
    elif l1a and caliber_sign_agree:
        status = "EVALUABLE_AT_AGGREGATE_LEVEL"
    elif l2:
        status = "EVALUABLE_AT_PATH_LEVEL"
    else:
        status = "NOT_EVALUABLE_FIDELITY"
    weak_all = sorted({w for g in fid_gate.values() for w in (g.get("weak_buckets") or [])})
    sign_txt = (f"方向判对率 = {_worst(o_sign, higher_is_worse=False)} "
                f"(阈 ≥{L1_SIGN_AGREEMENT_MIN}, 两口径 {o_sign})")
    levels = {
        "L0_per_trade_attribution": {
            "granted": bool(l0),
            "reading": "逐笔差值 (rows[].v2.paired_diff 单笔读数)",
            "criterion": (f"mae_over_median_abs_net ≤ {L0_MAE_TO_MEDIAN_NET_MAX} "
                          "(两 sample_source 都过)"),
            "observed": o_l0, "worst": _worst(o_l0),
            "observed_text": (f"尺子单笔误差 = 被测单笔量级的 {_worst(o_l0)} 倍 "
                              f"(阈 ≤{L0_MAE_TO_MEDIAN_NET_MAX}; 两口径 {o_l0})"),
            "say": "说「这一笔差 $X 是因为 Y」这类逐笔归因",
            "why": ("比值 >1 ⇒ 尺子单笔误差比该笔自身盈亏还大, 逐笔差值全是尺子噪声; "
                    "只有聚合/配对差因共模抵消与近零偏差而可用"),
        },
        "L1a_paired_diff_same_replayer": {
            "granted": bool(l1a),
            "reading": "caliber_a_same_replayer (限价确认位臂 vs 现行入场臂, 同一重放器)",
            "criterion": f"sign_agreement_rate ≥ {L1_SIGN_AGREEMENT_MIN} (两口径都过)",
            "observed": {"sign_agreement_rate": o_sign},
            "worst": {"sign_agreement_rate": _worst(o_sign, higher_is_worse=False)},
            "observed_text": sign_txt + "; 系统偏差同时作用于两臂 ⇒ 在配对差里抵消, 故不作要求",
            "say": "说「整体上限价确认位比现行入场好/差 $M」这类配对差结论 (本次要裁的就是它)",
            "why": ("两臂同一重放器同一 config ⇒ 偏差共模; 抵消是一阶近似而非恒等 "
                    "(两臂入场价不同 → 出场路径可能不同), 残余部分由 L0 的 MAE 界定"),
        },
        "L1b_aggregate_vs_ledger": {
            "granted": bool(l1b),
            "reading": "caliber_b_vs_ledger (cf 重放 vs trade_records 账本真值)",
            "criterion": (f"bias_to_mae_ratio ≤ {L1_BIAS_TO_MAE_MAX} ∧ "
                          f"sign_agreement_rate ≥ {L1_SIGN_AGREEMENT_MIN} (两口径都过)"),
            "observed": {"bias_to_mae_ratio": o_bias, "sign_agreement_rate": o_sign},
            "worst": {"bias_to_mae_ratio": _worst(o_bias),
                      "sign_agreement_rate": _worst(o_sign, higher_is_worse=False)},
            "observed_text": (f"系统偏差/离散度 = {_worst(o_bias)} (阈 ≤{L1_BIAS_TO_MAE_MAX})、"
                              + sign_txt),
            "say": "说「重放出来的总钱数对得上账本真值」这类跨尺子聚合对账结论",
            "why": ("对账本真值时偏差全额生效: 实测 bar_close 口径 bias +$6.58/笔 = 其 MAE 的 "
                    "44%, 而待裁效应中位数仅 −$9.63/笔 ⇒ 偏差与效应同量级, 不得当结论用"),
        },
        "L2_exit_path_attribution": {
            "granted": bool(l2),
            "reading": "exit_reason 归因 (哪条退出规则造成了这个结果)",
            "criterion": (f"reason_match_rate ≥ {L2_REASON_MATCH_MIN} ∧ 无 n≥5 弱桶 "
                          "(既有标签门, 原样保留)"),
            "observed": o_match, "worst": _worst(o_match, higher_is_worse=False),
            "observed_text": (f"同纪年当日域标签一致率 {o_match} "
                              f"(阈 ≥{L2_REASON_MATCH_MIN})"
                              + (f", 弱桶 {weak_all}" if weak_all else ", 无 n≥5 弱桶")),
            "say": "说「因为触发了哪条退出规则」这类出场路径归因",
            "why": ("标签门对经济等价标签 (SL_BREAKEVEN vs SL_TRAIL_LOCK, 钱差中位 $2.47) "
                    "偏严, 但它是路径归因的唯一凭据 — 不许可时不得讲因果故事"),
        },
    }
    granted_levels = [k2.split("_")[0] for k2, v in levels.items() if v["granted"]]
    return {
        "levels": levels,
        "granted_levels": granted_levels,
        "status": status,
        "epoch": epoch,
        "window_start": window_start, "window_end": epoch.get("window_end"),
        "license_domain": ("arm_lag_bars=1 ∧ 当日出场 ∧ 两 sample_source (与既有保真度门同域, "
                           "预注册); 全日域读数见 fidelity_matrix, 不参与许可"),
        "money_criteria_binding": bool(binding),
        "money_criteria_binding_from": MONEY_CRITERIA_BINDING_FROM,
        "provisional": not binding,
        "caliber_median_sign_agree": bool(caliber_sign_agree),
        # 裁决资格与尺子许可是两件事: 许可只说"这把尺子量得够细", 资格还要 n 达标。
        "sample_size": {"n_paired": n_paired, "min_for_conclusion": MIN_N_FOR_CONCLUSION,
                        "sufficient": bool(n_paired >= MIN_N_FOR_CONCLUSION),
                        "min_for_adr035_verdict": ADR035_VERDICT_MIN_N_PAIRED,
                        "verdict_sufficient": bool(n_paired >= ADR035_VERDICT_MIN_N_PAIRED)},
        # 裁决资格 = 生效日 ∧ 本裁决 n 门槛 ∧ 尺子在 L1a 这一级许可。三者缺一即无资格。
        "verdict_eligible": bool(binding and n_paired >= ADR035_VERDICT_MIN_N_PAIRED
                                 and status == "EVALUABLE_AT_AGGREGATE_LEVEL"),
        "rule": ("各级并列互不替代; 每级两口径都过才许可; 指标缺失 = 不许可 (fail-closed); "
                 f"钱口径判据自 {MONEY_CRITERIA_BINDING_FROM} 起才具裁决效力, "
                 f"之前的窗只出指示性读数; 配对 n<{MIN_N_FOR_CONCLUSION} 时按宪章不结论; "
                 f"n<{ADR035_VERDICT_MIN_N_PAIRED} 时不得裁 ADR-035 接线 "
                 "(许可照发, 裁决资格不发 — 见 sample_size / verdict_eligible)"),
        "note": ("许可卡回答的是**尺子分辨率**, 不是**信号有无 edge** — 后者仍受 ADR-035 "
                 "四判据与 n 门槛约束。L0 全窗不许可是设计使然: 每笔净额中位仅十几美元, "
                 "而单笔重放误差二三十美元, 逐笔归因在本系统里物理上不可判读。"),
    }


def _readout_card(s):
    """人话读数卡: 6 行必读 + 最多 2 行条件警告, 让入口自己说"能回答什么/不能回答什么"。

    动机 (用户 2026-09-07: "历史的尺子和回测太多了, 我都有点迷糊了, 我需要真实可用保真
    的数据"): 尺子越加越多 (标签门 / 钱口径 / 双口径 / 抗极值阶梯 / ADR-035 四判据),
    读的人反而不知道该信哪个数。这张卡**不新增任何判据**, 只把已有读数翻译成
    "可以下的结论"与"不可以下的结论", 并标明本次读数的**效力** (指示性 / 正式 / 不可用)。
    """
    lic = s.get("ruler_license") or {}
    ep = lic.get("epoch") or {}
    lv = lic.get("levels") or {}
    sens = s.get("caliber_sensitivity") or {}
    single = ep.get("single_epoch")
    lines = ["┌─ 读数卡: 这把尺子本次能回答什么 (不新增判据) ─────────────────────"]
    epoch_txt = ("单一参数纪年" if single else
                 ("跨纪年/有零复刻轨" if single is False else "不可判定 (门域零样本)"))
    if single is False and ep.get("epoch_floor"):
        epoch_txt += f", 换窗起点 {ep.get('epoch_floor')}"
    lines.append(f"│ 窗 {lic.get('window_start')} → {lic.get('window_end')} | "
                 f"配对 n={s.get('n_paired')} | 纪年: {epoch_txt}")
    # 域收窄藏坏轨 (实测 SL_INITIAL: 全口径 n=8 复刻 0, 当日域仅 n=4 ⇒ 躲过弱桶规则)。
    #   "无弱桶" ≠ "无坏轨", 这句必须写在明面上, 否则读者会把 L2 的失配归因归错。
    masked = ep.get("masked_zero_match_tracks") or {}
    if masked:
        lines.append("│ ⚠ 域外零复刻轨 (不计入门, 但存在): " + "; ".join(
            f"{k2} 全口径 n={v['n_all_calibers']} 复刻 {v['match_rate_all_calibers']}, "
            f"当日域仅 n={v['n_in_gate_domain']} < {EPOCH_ZERO_MATCH_MIN_N} ⇒ 躲过弱桶规则"
            for k2, v in sorted(masked.items(), key=lambda kv: -kv[1]["n_all_calibers"])))
    # 宪章「n<30 不结论」必须与许可同屏: 许可卡发放的是**尺子分辨率**, 不是裁决资格。
    #   缺这一行 ⇒ 9/8 之后单日 cron (实测 n=6) 会打出一张自称"效力: 正式"的卡。
    ss = lic.get("sample_size") or {}
    if ss.get("n_paired") is not None and not ss.get("sufficient"):
        lines.append(f"│ ⚠ 样本不足: 配对 n={ss.get('n_paired')} < "
                     f"{ss.get('min_for_conclusion')} ⇒ 宪章「n<30 不结论」, "
                     "本卡只作规模披露, 不得被引用为裁决依据")
    elif ss.get("n_paired") is not None and not ss.get("verdict_sufficient"):
        # 中间态 (30≤n<40): 可以说方向, 但**不得裁开关**。缺这一行 ⇒ n=30 的窗看起来
        #   与 n=45 的窗同等可用, 而后者才是预注册的裁决点。
        lines.append(f"│ ⚠ 未达本裁决门槛: 配对 n={ss.get('n_paired')} ≥ "
                     f"{ss.get('min_for_conclusion')} (宪章可下结论) 但 < "
                     f"{ss.get('min_for_adr035_verdict')} (ADR-035 接线裁决预注册门槛) ⇒ "
                     "可说方向, 不得裁 entry_archetype_routing_enabled; 不得降门槛, "
                     "也不得把生效日之前的窗并进来凑数 (宪章禁跨窗拼接)")
    for key, tag in (("L0_per_trade_attribution", "L0  逐笔归因    "),
                     ("L1a_paired_diff_same_replayer", "L1a 配对差(两臂同尺)"),
                     ("L1b_aggregate_vs_ledger", "L1b 聚合对账(对账本)"),
                     ("L2_exit_path_attribution", "L2  出场路径归因  ")):
        d = lv.get(key) or {}
        granted = bool(d.get("granted"))
        lines.append(f"│ {tag}: {'✅ 许可' if granted else '❌ 不许可'} — "
                     f"{d.get('observed_text')} ⇒ "
                     f"{'可以' if granted else '不可以'}{d.get('say')}")
    med, p, n = s.get("paired_diff_median"), s.get("sign_test_p_one_sided"), s.get("n_paired")
    wire = (s.get("adr035_verdict") or {}).get("wire_routing")
    if not n or med is None:
        concl = "本窗无配对样本 ⇒ 什么都不能下结论 (不是「无效」, 是「没测到」)"
    elif n < MIN_N_FOR_CONCLUSION:
        # 宪章「n<30 不结论」优先于符号检验: n=6 时 p 可以很小 (6/6 同向 ⇒ p=0.016),
        #   但那是小样本巧合不是证据。此分支只报点估计, 不写"有统计学证据"字样。
        concl = (f"配对差中位 {med:+.2f} 美元/笔 (n={n} < {MIN_N_FOR_CONCLUSION}) ⇒ "
                 f"宪章「n<30 不结论」: 只报点估计与符号检验 p={p}, 不下结论")
    else:
        rel = "优于" if med > 0 else ("劣于" if med < 0 else "持平于")
        agree = ("两口径同号" if sens.get("median_sign_agree")
                 else "两口径异号 ⇒ 结论由观测模型决定")
        # 措辞纪律: 点估计与统计证据必须分开说 — "中位数为负"不等于"劣于",
        #   否则一句"劣于…没有统计学证据"自相矛盾, 读的人会只记住前半句。
        if p is not None and p < 0.05:
            ev = f"符号检验 p={p} < 0.05 ⇒ 有统计学证据表明{rel}"
        else:
            ev = f"符号检验 p={p} ⇒ 没有统计学证据判定优劣 (点估计不是结论)"
        concl = (f"限价确认位 vs 现行入场 — 点估计{rel} (配对差中位 {med:+.2f} 美元/笔, "
                 f"n={n}, {agree}), {ev}")
    # [2026-09-16 用户裁"要"] C3 可达性分层一行 — 观测不裁决 (桶界 dist_atr≤0.5 预注册)
    _rs = s.get("c3_reachability_split") or {}
    if _rs.get("reachable_n") or _rs.get("unreachable_n"):
        lines.append(
            f"│ C3分层(观测): 可达(≤0.5ATR) n={_rs.get('reachable_n')} 成交率={_rs.get('reachable_fill_rate')} | "
            f"不可达 n={_rs.get('unreachable_n')} 成交率={_rs.get('unreachable_fill_rate')} "
            f"⇒ C3={'仅不可达桶拖累(DA定价问题)' if (_rs.get('reachable_fill_rate') or 0) >= 0.5 > (_rs.get('unreachable_fill_rate') or 0) else '需看两桶'}")
    # [2026-09-16 RFC-081盲区2/3 用户裁"要"] D分支误杀率 + 放弃经济一行 (expired 口径冻结)
    _mk = s.get("d_branch_miskill") or {}
    if _mk.get("n_expired"):
        lines.append(
            f"│ D误杀(expired口径,门≤40%): {_mk['n_miskilled']}/{_mk['n_expired']}={_mk['miskill_rate']}"
            f"{' ✅' if _mk['pass'] else ' ❌'} | 不成交腿放弃实盘净利 {_mk['forgone_actual_net_usd']:+.2f}"
            f"{' (净放弃利润,躲烂单叙事打折)' if (_mk['forgone_actual_net_usd'] or 0) > 0 else ''}")
    lines.append(f"│ 结论: {concl}; 接线开关 entry_archetype_routing_enabled "
                 f"{'维持关闭' if not wire else '四判据全过, 可进入接线裁决'}")
    st = lic.get("status")
    if st == "NOT_SINGLE_EPOCH":
        eff = (f"不可用 — 本窗跨参数纪年/有零复刻轨 "
               f"({', '.join(sorted(ep.get('unreachable_tracks') or {})) or '?'}), "
               f"换到 {ep.get('epoch_floor') or '该轨末次日期'} 之后的窗重跑")
    elif st == "PROVISIONAL_INDICATIVE_ONLY":
        eff = (f"指示性, 不具裁决效力 — 钱口径判据是在本窗数据上设计的, 用它裁本窗 = 凑门; "
               f"判据自 {lic.get('money_criteria_binding_from')} 起才有约束力, "
               f"正式裁决点 = 该日起新窗攒到 n≥40 配对")
    elif st == "NOT_EVALUABLE_FIDELITY":
        eff = "不可用 — 保真度未达任一级许可, 上面所有数字只作规模披露"
    else:
        eff = (f"尺子许可正式生效 (状态 {st}, 许可层级 {lic.get('granted_levels')})"
               + ("" if lic.get("verdict_eligible") else
                  f"; 但配对 n={(lic.get('sample_size') or {}).get('n_paired')} < "
                  f"{ADR035_VERDICT_MIN_N_PAIRED} (本裁决预注册门槛) ⇒ 无裁决资格, "
                  "只可作方向性参考"))
    lines.append(f"│ 效力: {eff}")
    lines.append("└───────────────────────────────────────────────────────────────────")
    return lines


def summarize_v2(results, fidelity_pairs):
    """v2 汇总: 同尺配对差 + 符号检验 + 抗极值 + 保真度 + ADR-035 四判据结算。"""
    from scripts.exit_path_replay import cfg_snapshot, fidelity_report
    import statistics

    v2rows = [r["v2"] for r in results if r.get("v2")]
    paired = [v for v in v2rows if v.get("paired")]
    diffs = [v["paired_diff"] for v in paired]
    # 窗边界: 许可卡的"生效日"判据要用 (钱口径判据自 MONEY_CRITERIA_BINDING_FROM 起才有
    #   约束力 — 在它之前设计判据所用的窗上裁决 = 凑门)。
    _dates = sorted({str(r.get("date"))[:10] for r in results if r.get("date")})
    window_start = _dates[0] if _dates else None
    window_end = _dates[-1] if _dates else None
    entered = [r for r in results if r.get("status") == "entered"]
    expired = [r for r in results if r.get("status") == "expired_unfilled"]
    fill_rate = (len(entered) / (len(entered) + len(expired))
                 if (entered or expired) else None)

    def _mfe0(vals):
        got = [v for v in vals if v is not None]
        return (round(sum(1 for v in got if abs(v) < 1e-9) / len(got), 3)
                if got else None)

    # 口径纪律: 改善率与全部合计**只在同一配对群**上算 — cf 侧若把"实盘未成交"的行
    #   (cf_net=0 而无实盘臂) 也计进来, 分子分母来自不同样本群, 改善率即失真
    #   (首版实测: cf_sum 取全 v2 行 → improve=-0.6485 与配对差合计 +194.11 符号相反)。
    cf_sum = round(sum(v["cf_net"] for v in paired), 2) if paired else None
    act_sum = round(sum(v["act_replay_net"] for v in paired), 2) if paired else None
    improve = (round((cf_sum - act_sum) / abs(act_sum), 4)
               if paired and act_sum not in (None, 0) else None)
    n_unpaired = len([v for v in v2rows if not v.get("paired")])
    mfe0_cf = _mfe0([v.get("cf_mfe_pct") for v in v2rows])
    mfe0_act = _mfe0([v.get("act_replay_mfe_pct") for v in paired])
    k = sum(1 for d in diffs if d > 0)
    trimmed = sorted(diffs, key=abs)[:-3] if len(diffs) > 6 else diffs

    # 抗极值阶梯 (承 PROPOSAL-20260905 A2 教训: "HIGH 前 3 笔占净额 77.9%" —
    #   合计口径可被两三笔绑架, 故均值/中位/胜率/剔除极值四种读法必须并报)。
    ranked = sorted(diffs, key=abs, reverse=True)
    trim_ladder = {}
    for cut in (1, 3, 5):
        if len(ranked) > cut:
            t = ranked[cut:]
            trim_ladder[f"drop_{cut}_largest_abs"] = {
                "n": len(t), "sum": round(sum(t), 2),
                "mean": round(statistics.mean(t), 2),
                "median": round(statistics.median(t), 2),
                "cf_better": sum(1 for v in t if v > 0)}

    # 合计口径的前三大贡献笔 — 附带实盘真值, 使"重放伪影"当场可见:
    #   若某笔 paired_diff 很大而 act_replay_net 与 actual_net(db) 差距同样大,
    #   该笔贡献来自重放器不保真, 不是来自入场方式差异。
    top = sorted((r for r in results if (r.get("v2") or {}).get("paired")),
                 key=lambda r: -abs(r["v2"]["paired_diff"]))[:3]
    top_contributors = [{
        "date": r["date"], "symbol": r["symbol"],
        "paired_diff": r["v2"]["paired_diff"],
        "cf_net": r["v2"]["cf_net"],
        "act_replay_net": r["v2"]["act_replay_net"],
        "actual_net_db": r.get("actual_net"),
        "replay_vs_db_gap": (round(r["v2"]["act_replay_net"] - r["actual_net"], 2)
                             if r.get("actual_net") is not None else None),
        "act_replay_reason": r["v2"]["act_replay_reason"],
        "actual_reason_db": r.get("actual_exit_reason"),
    } for r in top]

    crit = {
        "C1_n_ge_20": {"value": len(paired), "threshold": 20,
                       "pass": len(paired) >= 20},
        "C2_expectancy_improve_ge_30pct": {"value": improve, "threshold": 0.30,
                                           "pass": (improve is not None and improve >= 0.30)},
        "C3_fill_rate_ge_50pct": {"value": (round(fill_rate, 3) if fill_rate is not None else None),
                                  "threshold": 0.50,
                                  "pass": (fill_rate is not None and fill_rate >= 0.50)},
        "C4_mfe0_share_down": {"value": {"cf": mfe0_cf, "actual": mfe0_act},
                               "pass": (mfe0_cf is not None and mfe0_act is not None
                                        and mfe0_cf < mfe0_act)},
    }
    n_pass = sum(1 for c in crit.values() if c["pass"])

    # ── 保真度前置门 ──
    # 为什么必须有: 首版 v2 实测 C2「期望改善」通过 (+153%), 但拆开看 +194.11 的合计
    #   里, GTLB(+173.51) / UBER(+152.22) / META(+54.50) 三笔贡献了全部优势, 而这三笔的
    #   act_replay_net 与实盘真值分别差 -173.46 / -152.22 / -54.32 —— 重放器在
    #   SL_TRAIL_LOCK (复刻率 0%) 与 DEAD_SIGNAL_EXIT (未复刻) 上系统性把实盘赢家判成
    #   输家。**判据通过的是重放器的缺陷, 不是入场方式的优势。**
    # 门: 整体出场原因复刻率 ≥0.80 且 任何 n≥5 的实盘出场桶复刻率 ≥0.50,
    #   否则整份 v2 判读标 NOT_EVALUABLE_FIDELITY — 四条判据照常输出但不作数。
    # 门口径 (2026-09-07 保真度专项重写, 预注册):
    #   ① **域** = 当日出场 (跨日仓真值路径需跨日 bar+墙钟, 单日重放物理上不可复刻);
    #   ② **口径** = arm_lag_bars=1 (订单因果性) × sample_source 两格**都要过** —
    #      实盘 10s 轮询的真值落在两格之间, 只报过门的那一格 = 挑口径;
    #   ③ 阈值沿用既有 0.80 / 弱桶 0.50 (n≥5), 不新造阈值语言。
    def _weak_buckets(rep):
        return [f"{k3}(n={v3['n']},复刻{v3['match_rate']})"
                for k3, v3 in (rep.get("by_db_reason") or {}).items()
                if v3.get("n", 0) >= 5 and (v3.get("match_rate") or 0) < 0.50]

    fid = fidelity_report(fidelity_pairs)              # 混口径全样本: 只作规模披露, 不作门
    fid_matrix = {}
    for lag in ARM_LAG_CALIBERS:
        for src in SAMPLE_CALIBERS:
            sub = [q for q in fidelity_pairs
                   if q.get("arm_lag_bars", 1) == lag
                   and q.get("sample_source", "bar_extreme") == src]
            if sub:
                fid_matrix[f"lag{lag}|{src}"] = fidelity_report(sub)
    fid_gate = {}
    for src in SAMPLE_CALIBERS:
        sd = [q for q in fidelity_pairs
              if q.get("arm_lag_bars", 1) == 1
              and q.get("sample_source", "bar_extreme") == src
              and q.get("same_day") is not False]
        rep = fidelity_report(sd)
        wk = _weak_buckets(rep)
        fid_gate[f"lag1|{src}"] = {
            "n_same_day": rep.get("n"), "match_rate": rep.get("reason_match_rate"),
            "threshold": 0.80, "weak_buckets": wk, "net_bias": rep.get("net_bias"),
            "net_mae": rep.get("net_mae"),
            # 钱口径三指标 (与标签门**并列**, 不替换): 供 ruler_license 的 L0/L1 分级
            "sign_agreement_rate": rep.get("sign_agreement_rate"),
            "bias_to_mae_ratio": rep.get("bias_to_mae_ratio"),
            "median_abs_db_net": rep.get("median_abs_db_net"),
            "mae_over_median_abs_net": rep.get("mae_over_median_abs_net"),
            "pass": bool(rep.get("n")) and (rep.get("reason_match_rate") or 0) >= 0.80 and not wk,
        }
    fid_ok = bool(fid_gate) and all(g["pass"] for g in fid_gate.values())
    weak = sorted({w for g in fid_gate.values() for w in g["weak_buckets"]})

    # 参数纪年上限: 现行 config 已关闭的轨, 其历史真值行在本纪年下**不可能**被复刻 ⇒
    #   该窗保真度存在数学上限 (宪章禁跨窗拼接 ⇒ 只能换窗, 不能改门)。
    snap = cfg_snapshot()
    dead_tracks = {}
    if not snap.get("mfe_stall_exit_enabled"):
        dead_tracks["MFE_STALL_EXIT"] = "mfe_stall_exit_enabled=False (末次实盘触发 2026-08-11)"
    n_dead = sum(v3["n"] for k3, v3 in (fid.get("by_db_reason") or {}).items()
                 if k3 in dead_tracks)
    ceiling_epoch = (round(1 - n_dead / fid["n"], 3) if fid.get("n") else None)

    # 观测口径敏感性: 同一批配对群, 只换 sample_source ⇒ 差值变化纯属尺子
    alt = [v["sens_bar_close"]["paired_diff"] for v in v2rows
           if (v.get("sens_bar_close") or {}).get("paired_diff") is not None]
    med_pri = round(statistics.median(diffs), 2) if diffs else None
    med_alt = round(statistics.median(alt), 2) if alt else None
    sign_agree = (med_pri is None or med_alt is None
                  or (med_pri > 0) == (med_alt > 0) or 0 in (med_pri, med_alt))
    caliber_sensitivity = {
        "bar_extreme": {"n": len(diffs), "sum": round(sum(diffs), 2) if diffs else None,
                        "median": med_pri, "cf_better": k},
        "bar_close": {"n": len(alt), "sum": round(sum(alt), 2) if alt else None,
                      "median": med_alt, "cf_better": sum(1 for x in alt if x > 0)},
        "median_sign_agree": bool(sign_agree),
        "note": ("两口径 = 实盘 10s 轮询真值的上/下界 (exit_path_replay 模块头「采样口径」)。"
                 "中位数符号不一致 ⇒ 本窗结论由观测模型决定而非入场方式 ⇒ 不可判读"),
    }
    if fid_ok and not sign_agree:
        status = "NOT_EVALUABLE_CALIBER_SPLIT"
    elif fid_ok:
        status = "EVALUABLE"
    else:
        status = "NOT_EVALUABLE_FIDELITY"
    evaluability = {
        "status": status,
        "gate_domain": "same_day_only (跨日仓结构性不可复刻, 不计入门)",
        "gate_calibers": list(fid_gate),
        "fidelity_gate": fid_gate,
        "reason_match_rate": fid.get("reason_match_rate"),
        "match_rate_same_day": fid.get("match_rate_same_day"),
        "threshold": 0.80,
        "weak_buckets": weak,
        "ceiling_cross_day": fid.get("ceiling_cross_day"),
        "ceiling_dead_epoch": ceiling_epoch,
        "dead_epoch_tracks": dead_tracks,
        "caliber_median_sign_agree": bool(sign_agree),
        "note": ("保真度不过门 (或两口径中位数异号) ⇒ adr035_criteria 一律不作数 (含通过项)。"
                 "两道上限 (跨日 / 已关闭纪年轨) 是全窗读数的数学天花板, 不是重放器缺陷; "
                 "过门只能在「同纪年 ∧ 当日出场」的窗上要求"),
    }
    # ── 纪年守卫 + 三级许可卡 (2026-09-07 判据换轨) ──
    epoch = _epoch_guard(fidelity_pairs, window_start)
    license_ = _ruler_license(fid_gate, epoch, window_start, sign_agree, len(paired))

    out = {
        "readout_card": None,   # 末尾回填: 卡片要读全部读数, 故必须最后生成
        "window_start": window_start, "window_end": window_end,
        "ruler_license": license_,
        "evaluability": evaluability,
        "caliber": "v2 双臂同尺 (scripts/exit_path_replay.py), 两臂均净额",
        "n_rows": len(results), "n_paired": len(paired),
        "cf_net_sum": cf_sum, "act_replay_net_sum": act_sum,
        "n_unpaired_no_actual_fill": n_unpaired,
        "population_note": "所有合计/改善率/符号检验只在 paired 群 (两臂皆有) 上计算",
        "paired_diff_sum": round(sum(diffs), 2) if diffs else None,
        "paired_diff_mean": round(statistics.mean(diffs), 2) if diffs else None,
        "paired_diff_median": round(statistics.median(diffs), 2) if diffs else None,
        "caliber_a_same_replayer": {
            "sum": round(sum(diffs), 2) if diffs else None,
            "median": round(statistics.median(diffs), 2) if diffs else None,
            "cf_better": k,
            "note": "两臂同一重放器 (共模偏差抵消, 但受 bar-vs-轮询 观测模型限制)"},
        "caliber_b_vs_ledger": (lambda ds: {
            "sum": round(sum(ds), 2), "median": round(statistics.median(ds), 2),
            "cf_better": sum(1 for x in ds if x > 0), "n": len(ds),
            "note": "cf 侧 bar 重放 vs 实盘侧账本真值 (实盘侧为真实发生的钱)"})(
            [v["paired_diff_vs_db"] for v in paired
             if v.get("paired_diff_vs_db") is not None]) if paired else None,
        "cf_better_count": k,
        "sign_test_p_one_sided": round(_sign_test_one_sided(k, len(diffs)), 4) if diffs else None,
        "robust_drop3_largest_abs": {
            "cf_better_count": sum(1 for d in trimmed if d > 0), "n": len(trimmed),
            "median": round(statistics.median(trimmed), 2) if trimmed else None},
        "trim_ladder": trim_ladder,
        "top_contributors": top_contributors,
        "fill_rate": (round(fill_rate, 3) if fill_rate is not None else None),
        # [2026-09-16 用户裁"要"] C3 可达性分层 — 观测字段, 不改判据不挪门槛。
        #   裁决日读法: C3 若败, 先看 reachable/unreachable 两桶各自的成交率与净额,
        #   区分"限价机制成交能力不足"(可达桶也败) vs "DA 确认位定价不可达"(仅不可达桶败)。
        #   可达定义 = reach_dist_atr ≤ 0.5 (预注册于 ADR-035 批注§五.2, 桶界不得事后调)。
        "c3_reachability_split": (lambda _rr: {
            "reachable_n": sum(1 for x in _rr if x.get("reach_dist_atr") is not None
                               and x["reach_dist_atr"] <= 0.5),
            "reachable_fill_rate": (lambda s: (round(
                sum(1 for x in s if x.get("status") == "entered") / len(s), 3) if s else None))(
                [x for x in _rr if x.get("reach_dist_atr") is not None
                 and x["reach_dist_atr"] <= 0.5]),
            "unreachable_n": sum(1 for x in _rr if x.get("reach_dist_atr") is not None
                                 and x["reach_dist_atr"] > 0.5),
            "unreachable_fill_rate": (lambda s: (round(
                sum(1 for x in s if x.get("status") == "entered") / len(s), 3) if s else None))(
                [x for x in _rr if x.get("reach_dist_atr") is not None
                 and x["reach_dist_atr"] > 0.5]),
            "no_atr_n": sum(1 for x in _rr if x.get("reach_dist_atr") is None),
            "rule": "可达=dist_atr≤0.5 (预注册桶界); 观测不裁决 — C3 门槛 50% 本身不动",
        })([x for x in results if x.get("status") in ("entered", "expired_unfilled")]),
        # [2026-09-16 RFC-081盲区2/3 用户裁"要"] D分支误杀率 + expired 腿放弃经济 —
        #   盲区2: 误杀率分母**冻结为 expired_unfilled** (RFC-081 增补"误杀率≤40%"
        #   原文未定分母; expired 口径 = 限价没成交而实盘赚钱 = 真"错过", 全配对口径
        #   会把成交腿稀释进去 — 裁决前锁死 expired 口径, 防临时解释)。
        #   盲区3: "不成交=躲烂单"叙事必须与放弃的实盘净利同屏呈报, 防单边美化。
        "d_branch_miskill": (lambda _exp: {
            "denominator": "expired_unfilled (冻结, RFC-081 增补批注§六)",
            "n_expired": len(_exp),
            "n_miskilled": sum(1 for x in _exp if (x.get("actual_net") or 0) > 0),
            "miskill_rate": (round(sum(1 for x in _exp if (x.get("actual_net") or 0) > 0)
                                   / len(_exp), 3) if _exp else None),
            "threshold_le": 0.40,
            "pass": (len(_exp) == 0 or
                     sum(1 for x in _exp if (x.get("actual_net") or 0) > 0) / len(_exp) <= 0.40),
            "forgone_actual_net_usd": round(sum((x.get("actual_net") or 0) for x in _exp), 2),
            "note": "forgone>0 = 不成交腿净放弃了实盘利润, '躲烂单'叙事不能只看配对差",
        })(expired),
        "mfe0_share": {"cf": mfe0_cf, "actual": mfe0_act},
        "exit_reason_dist": {
            "cf": {k2: sum(1 for v in v2rows if v.get("cf_exit_reason") == k2)
                   for k2 in sorted({str(v.get("cf_exit_reason")) for v in v2rows})},
            "actual_replay": {k2: sum(1 for v in paired if v.get("act_replay_reason") == k2)
                              for k2 in sorted({str(v.get("act_replay_reason")) for v in paired})}},
        "db_net_sum_paired": (round(sum(r["actual_net"] for r in results
                                        if r.get("v2", {}).get("paired")
                                        and r.get("actual_net") is not None), 2)
                              if paired else None),
        "fidelity": fid,
        "fidelity_matrix": fid_matrix,
        "caliber_sensitivity": caliber_sensitivity,
        "adr035_criteria": crit,
        "adr035_verdict": {
            "n_pass": n_pass, "n_total": len(crit),
            "wire_routing": bool(fid_ok and sign_agree and n_pass == len(crit)),
            "blocked_by": (None if (fid_ok and sign_agree)
                           else ("fidelity_gate" if not fid_ok else "caliber_split")),
            "rule": "ADR-035 预注册: 四条全过才谈执行层接线 (entry_archetype_routing_enabled)"},
        "replay_cfg": snap,
    }
    out["readout_card"] = _readout_card(out)
    return out


def _dates_in_range(db, from_date, to_date):
    return [r[0] for r in db.execute(
        """SELECT DISTINCT date FROM playbook_instructions
           WHERE date >= ? AND date <= ? AND direction IN ('BUY','SELL_SHORT')
           ORDER BY date""", (from_date, to_date))]


def _print_card(v2_summary):
    """把读数卡打到 stdout 末尾 — JSON 之后, 使人和 cron 日志都能一眼看到"能回答什么"。"""
    for ln in ((v2_summary or {}).get("readout_card") or []):
        print(ln)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--from-date", default="", help="多日窗口起 (含); 与 --to-date 同用")
    ap.add_argument("--to-date", default="", help="多日窗口止 (含)")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # ── 多日窗口模式: 写独立文件 (fresh, 可重跑幂等), 不动 cron 的 OUT_JSONL/OUT_LATEST ──
    if args.from_date or args.to_date:
        dates = _dates_in_range(db, args.from_date or "0000-01-01",
                                args.to_date or "9999-12-31")
        all_rows, all_pairs = [], []
        per_day = {}
        for d in dates:
            res, pairs, _shadow_ok, _bad = build_rows(db, d)
            if not res:
                continue
            all_rows.extend(res)
            all_pairs.extend(pairs)
            per_day[d] = summarize_v2(res, pairs)
        summary = summarize_v2(all_rows, all_pairs) if all_rows else {"n_rows": 0}
        summary["window"] = {"from": args.from_date, "to": args.to_date,
                             "dates_with_rows": sorted(per_day)}
        summary["per_day"] = per_day
        OUT_V2_JSONL.write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in all_rows),
            encoding="utf-8")
        OUT_V2_LATEST.write_text(json.dumps({"summary": summary, "rows": all_rows},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        _print_card(summary)
        if args.json:
            Path(args.json).write_text(json.dumps(all_rows, ensure_ascii=False, indent=1))
        return

    date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    results, fidelity_pairs, shadow_collected, bad_raw = build_rows(db, date)

    n = len(results)
    entered = [x for x in results if x.get("status") == "entered"]
    expired = [x for x in results if x.get("status") == "expired_unfilled"]
    executed = [x for x in results if x.get("actual_executed")]
    # 2026-09-02 (议题二): 影子未采集 (文件缺失或当日零行) → n_chase_cap_violated=null
    #   非 0 — 9/1 首日 trigger 影子全灭 (json.loads(dict) bug, 修复 7e4d51cb 于盘中),
    #   报告输出 0 会被判读为"零违例", 与"采到零违例"语义相反。
    note = ("v1 臂保留供历史对照 (毛额/仅SL+EOD近似, 不可与净额混算); "
            "判读一律用 v2 (双臂同尺、均净额)")
    if not shadow_collected:
        note += "; trigger shadow 未采集 → chase_cap 维度 null (非零)"
    summary = {
        "date": date, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "schema": "v2",
        "n_instructions_with_archetype": n,
        "n_cf_entered": len(entered), "n_expired_unfilled": len(expired),
        "n_no_bars": sum(1 for x in results if x.get("status") == "no_bars"),
        "n_actually_executed": len(executed),
        "actual_net_sum": round(sum(x["actual_net"] for x in executed), 2),
        "cf_net_sum": round(sum(x["cf_pnl"] for x in entered if x.get("cf_pnl") is not None), 2),
        "n_chase_cap_violated": (sum(1 for x in results
                                     if x.get("chase_cap_violated"))
                                 if shadow_collected else None),
        "note": note,
    }
    if results:
        summary["v2"] = summarize_v2(results, fidelity_pairs)
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        for x in results:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    OUT_LATEST.write_text(json.dumps({"summary": summary, "rows": results},
                                     ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for x in results:
        st = x.get("status")
        tag = {"entered": f"CF入场{x.get('cf_entry')}@{x.get('cf_entry_t')} "
                          f"出场{x.get('cf_exit')}({x.get('cf_exit_why')}) CF毛={x.get('cf_pnl')}",
               "expired_unfilled": f"确认位{x.get('confirm_level')} TTL内未触及→不进场",
               "no_bars": "无bar数据", "skip_no_level": "无确认位"}.get(st, st)
        act = f"实际净={x['actual_net']}({x.get('actual_exit_reason')})" if x.get("actual_executed") else "实际未成交"
        v2 = x.get("v2") or {}
        tail = ""
        if v2.get("paired"):
            tail = (f" | v2同尺: CF净={v2.get('cf_net')}({v2.get('cf_exit_reason')}) "
                    f"实盘重放净={v2.get('act_replay_net')}({v2.get('act_replay_reason')}) "
                    f"配对差={v2.get('paired_diff')}")
        print(f"{x['symbol']} {x.get('entry_archetype')}: {tag} | {act}{tail}")
    _print_card(summary.get("v2"))
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
