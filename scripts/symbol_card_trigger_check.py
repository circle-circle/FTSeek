# -*- coding: utf-8 -*-
"""股性卡规则触发状态确定性判定 (DEB-20260819 §十六, 2026-08-19).

背景: 8/19 不落库演练发现错位案例 — LLM 目测 SNDK 8/18 "贴近日低"声称触发 R9b,
实算收盘位置=20.50% > 20% 阈值, 未触发 (差 0.5pp). LLM 估算边界不可靠。

设计: 注入时用代码逐字复刻回放口径计算触发状态, 写进 prompt 段,
LLM 只消费判定结果不再自行估算。

口径 (与回放脚本逐字一致):
  R5  跳空高开: gap = (open - prev_close)/prev_close > +2%      [symbol_rule_replay.py L88-91]
  R9  收盘贴日高: pos = (c-l)/(h-l) >= 0.8                      [daily_r9r10_replay.py L48-50]
  R9b 收盘贴日低: pos <= 0.2                                    [daily_r9r10_replay.py L51-52]
  R10 NR7: 当日振幅(相对收盘%) = 近7日(含当日)最小               [daily_r9r10_replay.py L53-54]
  M1/M1b 开盘30min: o30r = (30min收盘-开盘)/开盘 与 ±0.3% 比      [minute_rules_replay.py L58,76-79]
  M2/M2b 区间突破: h > o30h*1.001 / l < o30l*0.999              [minute_rules_replay.py L81-84]
  M3/M3b 开盘5min: o5r 与 ±0.3% 比                               [minute_rules_replay.py L59,86-89]

语义 (盘前时刻): 判定对象=最近一个完整交易日 (昨日); 今日=结果日。
分钟族在盘前无当日行情 → "今日盘中待观察" (若分K缓存已含当日且≥30根则实算)。
陈旧守卫: 日K缓存末日 < 上一交易日 → 返回 None (不判定, 宁缺勿错)。
"""
import json
from datetime import datetime
from pathlib import Path

DAILY_DIR = Path("/opt/ftseek/FTSeek-US/data/replay_bars_daily")
MINUTE_DIR = Path("/opt/ftseek/FTSeek-US/data/replay_bars_1m")

# 规则族 → 判定类型
DAILY_RULES = ("R5_", "R9_", "R9b_", "R10_")
MINUTE_RULES = ("M1", "M2", "M3")


def _load_daily(sym):
    fp = DAILY_DIR / f"{sym.replace('.', '_')}.json"
    if not fp.exists():
        return None
    try:
        rows = json.loads(fp.read_text())
        return [(r[0][:10], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in rows]  # date, o, h, l, c
    except Exception:
        return None


def _pos_str(p):
    return f"{p*100:.1f}%"


def evaluate(sym, rule_names):
    """返回 {rule: status_str}; 数据不足返回 {rule: None}."""
    daily = _load_daily(sym)
    out = {r: None for r in rule_names}
    if not daily or len(daily) < 8:
        return out
    # 新鲜度: 缓存末日必须是最近交易日 — 用缓存倒数第二根做参照判定最后一根是否为"昨日"
    # 这里不依赖外部日历: 若缓存末日早于上一交易日, 调用方(注入器)按生成日期守卫兜底。
    d0, o0, h0, l0, c0 = daily[-1]
    dp, op, hp, lp, cp = daily[-2]

    # ── 日K族: 判定最近完整日 (盘前=昨日触发与否, 今日为结果日) ──
    rng0 = h0 - l0
    pos0 = (c0 - l0) / rng0 if rng0 > 0 else None
    gap0 = (o0 - cp) / cp * 100 if cp > 0 else None
    # NR7: 近7日(含当日)相对振幅最小
    rngs7 = []
    for d, o, h, l, c in daily[-7:]:
        if c > 0:
            rngs7.append((h - l) / c * 100)
    nr7 = bool(rngs7) and abs(rngs7[-1] - min(rngs7)) < 1e-9

    for rule in rule_names:
        if rule.startswith("R9b"):
            if pos0 is None:
                continue
            if pos0 <= 0.2:
                out[rule] = f"✅昨日已触发(收盘位置{_pos_str(pos0)}≤20%)·今日为结果日"
            else:
                out[rule] = f"昨日未触发(收盘位置{_pos_str(pos0)},阈值≤20%)"
        elif rule.startswith("R9_"):
            if pos0 is None:
                continue
            if pos0 >= 0.8:
                out[rule] = f"✅昨日已触发(收盘位置{_pos_str(pos0)}≥80%)·今日为结果日"
            else:
                out[rule] = f"昨日未触发(收盘位置{_pos_str(pos0)},阈值≥80%)"
        elif rule.startswith("R10"):
            if nr7:
                out[rule] = f"✅昨日已触发(振幅{rngs7[-1]:.2f}%=近7日最窄)·今日为结果日"
            else:
                out[rule] = f"昨日未触发(振幅{rngs7[-1]:.2f}%,近7日最窄{min(rngs7):.2f}%)"
        elif rule.startswith("R5_"):
            if gap0 is None:
                continue
            if gap0 > 2:
                out[rule] = f"✅昨日已触发(跳空{gap0:+.2f}%>+2%)·今日为结果日"
            else:
                out[rule] = f"昨日未触发(跳空{gap0:+.2f}%,阈值>+2%)"

    # ── 分钟族: 盘前无当日行情 → 待观察; 若分K缓存含当日且足够则实算 ──
    minute_today = None
    try:
        fp = MINUTE_DIR / f"{sym.replace('.', '_')}.json"
        if fp.exists():
            mb = json.loads(fp.read_text())
            if mb and mb[-1][0][:10] == datetime.now().strftime("%Y-%m-%d"):
                minute_today = mb
    except Exception:
        pass

    for rule in rule_names:
        if not rule.startswith(MINUTE_RULES):
            continue
        if minute_today is None:
            out[rule] = "今日盘中待观察(开盘后30min内判定)"
            continue
        db = minute_today
        if len(db) < 30:
            out[rule] = "今日盘中观察中(不足30min)"
            continue
        o = db[0][1]
        o30 = db[:30]
        o5 = db[:5]
        o30r = (o30[-1][4] - o) / o * 100
        o5r = (o5[-1][4] - o) / o * 100
        o30h = max(x[2] for x in o30)
        o30l = min(x[3] for x in o30)
        h = max(x[2] for x in db)
        l = min(x[3] for x in db)
        if rule.startswith("M1"):
            v, th = o30r, 0.3
            hit = v > th if "b" not in rule else v < -th
            label = f"30min{o30r:+.2f}%"
        elif rule.startswith("M3"):
            v, th = o5r, 0.3
            hit = v > th if "b" not in rule else v < -th
            label = f"5min{o5r:+.2f}%"
        else:  # M2
            hit = (h > o30h * 1.001) if "b" not in rule else (l < o30l * 0.999)
            label = "区间突破" if "b" not in rule else "区间跌破"
        out[rule] = ("✅今日已触发(" + label + ")" if hit
                     else f"今日未触发({label})")
    return out
