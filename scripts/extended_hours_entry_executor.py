#!/usr/bin/env python3
"""盘前/盘后事件驱动开仓安全入口 (2026-09-20, SIM direct-gate).

信号过滤 (宁缺毋滥):
  same-session positive earnings surprise + price/change/volume floor
  + market cap + extended dollar turnover + maturity window
  + extended VWAP/volume confirmation. Candidates are graded A/B before sizing.
当前 Futu SIM 不支持 STOP/STOP_LIMIT。没有券商侧止损时本脚本拒绝真实开仓；
dry 模式仅输出候选，不写 state、不下单。

2026-09-20 direct SIM update:
  same-session earnings + market cap + extended turnover + maturity window
  + extended VWAP/volume confirmation + A/B grade sizing/liquidity.

护栏 (开仓风险高于锁盈, 更严):
  - 单笔仓位 $5000 (用户 9/17 拍板; 高低价股都覆盖)
  - 每日盘前+盘后合计 ≤MAX_PER_DAY 笔新仓 (2026-09-21 用户裁: 10, 防连环开仓失控上限;
    实际并发约束由在仓帽 MAX_OPEN=5 承担 — 平仓动态回补, 平掉即可再开)
  - 下单前盘口硬闸: spread/买一深度/买一漂移/预估往返费用 (数据缺失 fail-closed)
  - SIM 无券商 STOP/STOP_LIMIT 时真实开仓 fail-closed
  - REAL 环境未授权, 一律拒绝
  - 每标的每日一次 attempt, 失败不消耗开仓配额

与锁盈执行器共用: 独立 OpenQuoteContext (主服务 quote ctx 被占用, 勿复用).
用法: PYTHONPATH=. venv/bin/python3.11 scripts/extended_hours_entry_executor.py \
        --session premarket [--dry]
回滚: 删本脚本 + 对应 cron 行.
"""
import argparse
import contextlib
import fcntl
import json
import logging
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from app.core.trade_identity import ensure_test_symbol_not_in_production_db, is_test_symbol

_log = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    _log.warning("dotenv load failed", exc_info=True)

# ── 用户 9/17 拍板参数 ──
POSITION_USD = 5000        # 单笔仓位
POSITION_B_USD = float(os.getenv("EXT_ENTRY_POSITION_B_USD", "2500"))
GRADE_A_CERT_LOCK_PCT = float(os.getenv("EXT_GRADE_A_CERT_LOCK_PCT", "0.0"))
GRADE_B_CERT_LOCK_PCT = float(os.getenv("EXT_GRADE_B_CERT_LOCK_PCT", "3.0"))
MAX_PER_DAY = int(os.getenv("EXT_ENTRY_MAX_PER_DAY", "3"))
PREMARKET_MAX_PER_DAY = int(os.getenv("EXT_ENTRY_PREMARKET_MAX_PER_DAY", "2"))
MAX_OPEN = int(os.getenv("EXT_ENTRY_MAX_OPEN", str(MAX_PER_DAY)))
MAX_OPEN_NOTIONAL_USD = float(
    os.getenv("EXT_ENTRY_MAX_OPEN_NOTIONAL_USD", str(POSITION_USD * MAX_OPEN))
)
# ↑ 盘前+盘后**合计**每日新仓上限. 2026-09-17 用户裁: 2 → 5 (原话"配额可以提升到5，再观察").
#   动机: 延长时段机会面比预期宽(官方榜 top50), 2 笔在盘前窗早段就被用尽 ⇒ 当晚真催化票开不出来;
#   叠加同日上线的价格下限闸($5 挡仙股)后, 单笔噪声风险已降, 放宽数量换取样本速度。
#   2026-09-18 观察期: env/代码默认均压到 3, 暴露上限 = 3 × POSITION_USD($5000)。
#   回滚: env EXT_ENTRY_MAX_PER_DAY=2 或 EXT_ENTRY_ALLOW_SIM=false。
# PREMARKET_MAX_PER_DAY 在合计 3 笔内为盘后保留至少 1 笔启动额度:
#   盘前最多 2 笔, 盘后仍可 1~3 笔; 全日合计仍不超过 3 笔.
# 2026-09-21 用户裁决 (取代上两段现值, 历史动机保留): 日流量帽=10, 仅作防连环开仓的
#   失控上限; 盘前/盘后共享总额、不单窗限制 (PREMARKET_MAX_PER_DAY=总配额 → 单窗闸
#   永不先触发); 实际并发约束由 MAX_OPEN=5 承担 — 在仓帽平仓动态回补, 平掉即可再开.
#   现值以 .env.us 为准; 回滚 9/18 观察期: MAX_PER_DAY=3 + PREMARKET=2 + MAX_OPEN=3.
# 盘前涨幅门槛。2026-09-17 回放: **边际跳空档 (涨幅 <3%) 是唯一负档** ——
#   现役口径 n=46: <3% 档 均值 -0.65%/WR 42.9%/最差 -6.86%; ≥3% 档 均值 +1.41%(含6-10%档 +3.27%)。
#   放宽口径 n=161 同向复现: <3% 档 均值 -0.33%/WR 39.4%/最差 -8.93%; ≥3% 档 均值 +1.32%。
#   ⇒ 提到 3.0 理论上把每笔均值 +0.26% → +1.32% (供给 161→57 笔; 现役口径 46→约 32 笔)。
#   ⚠️ 两样本**同窗口**(06-22→09-16) ⇒ 非真样本外; 按既定转正门槛(双窗口)未擅自改默认值。
#   开: EXT_ENTRY_CHANGE_PCT=3.0 (无需改码/重启)
ENTRY_CHANGE_PCT = float(os.getenv("EXT_ENTRY_CHANGE_PCT", "2.0"))
MIN_VOLUME = 200000        # 盘前量门槛 (未分档时的全时段默认)
# 量闸按时段分档 (RFC-100, 2026-09-23 用户裁「中」): 格式 "4-7:20000,8-9:200000" (ET 小时闭区间)。
#   动机 (实测 4 个交易日 / 402 轮候选池): 04:00-07:59 ET 的候选量中位仅 398~2,640 股,
#   200,000 门槛在该时段约是 p88 分位 (只有 12~26% 能过) ⇒ 盘前早段**结构性不下单**,
#   而 bmo 财报 (常 07:00-08:30 发) 的盘前跳空正好落在这里。分档 = 让"活跃"在各时段指同一分位。
#   未配置 (默认空) ⇒ 全时段 MIN_VOLUME, 行为 byte 级不变。回滚 = 删掉 .env.us 该键 (一个词)。
#   执行质量由既有 MAX_SPREAD_PCT (1.0%) 闸兜底 —— 早段价差大时它会把票拦下 (实测 CBRL 盘前 12.4%)。
MIN_VOLUME_BUCKETS_RAW = os.getenv("EXT_ENTRY_MIN_VOLUME_BUCKETS", "").strip()
# 影子分档 (record-only, 默认空): 只用于反事实读数 ("若这些时段也分档会多放行几只"), 不参与判定。
MIN_VOLUME_BUCKETS_SHADOW_RAW = os.getenv("EXT_ENTRY_MIN_VOLUME_BUCKETS_SHADOW", "").strip()
MIN_PRICE = float(os.getenv("EXT_ENTRY_MIN_PRICE", "5.0"))
EXEC_PRICE_SHADOW_ON = os.getenv("EXT_ENTRY_EXEC_PRICE_SHADOW", "false").lower() == "true"
EXEC_PRICE_SHADOW_MIN_PREMARKET = float(
    os.getenv("EXT_ENTRY_EXEC_MIN_PRICE_PREMARKET", "50.0")
)
EXEC_PRICE_SHADOW_MIN_AFTERHOURS = float(
    os.getenv("EXT_ENTRY_EXEC_MIN_PRICE_AFTERHOURS", "50.0")
)
MIN_EXT_TURNOVER_USD = float(os.getenv("EXT_ENTRY_MIN_TURNOVER_USD", "10000000"))
A_GRADE_TURNOVER_USD = float(os.getenv("EXT_ENTRY_A_TURNOVER_USD", "20000000"))
MIN_MARKET_CAP_USD = float(os.getenv("EXT_ENTRY_MIN_MARKET_CAP_USD", "1000000000"))
MAX_SPREAD_PCT_A = float(os.getenv("EXT_ENTRY_MAX_SPREAD_PCT_A", "0.5"))
MIN_ASK_DEPTH_RATIO_A = float(os.getenv("EXT_ENTRY_MIN_ASK_DEPTH_RATIO_A", "2.0"))
MAX_VWAP_DISTANCE_PCT = float(os.getenv("EXT_ENTRY_MAX_VWAP_DISTANCE_PCT", "0.8"))
MIN_VOLUME_CONFIRM_RATIO = float(os.getenv("EXT_ENTRY_MIN_VOLUME_CONFIRM_RATIO", "0.5"))
MAX_SPREAD_PCT = float(os.getenv("EXT_ENTRY_MAX_SPREAD_PCT", "1.0"))
MAX_ROUND_TRIP_FEE_PCT = float(os.getenv("EXT_ENTRY_MAX_ROUND_TRIP_FEE_PCT", "1.0"))
MAX_PRICE_DRIFT_PCT = float(os.getenv("EXT_ENTRY_MAX_PRICE_DRIFT_PCT", "0.5"))
MIN_ASK_DEPTH_RATIO = float(os.getenv("EXT_ENTRY_MIN_ASK_DEPTH_RATIO", "1.0"))
# ── 2026-09-20 二轮加固 (DEB-20260920 §10, 独立复核 P0/P1 落地) ──
# P0-1 Finnhub surprise 新鲜度守卫: company_earnings(limit=1) 的 e[0] 在**今日 actual 尚未
#   入库**时是上一季的记录 — "最近 surprise>0" 只证明上季 beat, 不证明今日 beat。
#   miss + 盘前先涨 2% (好 guidance/市场误读) 会透过旧闸 ⇒ "财报高开回落"病灶变种。
#   守卫: e[0].period (财季末) 距今必须在 [-7, N] 天内; 缺失/不可解析/超龄一律 fail-closed。
#   N=150 依据: 财季末→披露常规滞后 30~90 天, 150 天已容纳晚披露者, 而上一季记录
#   (距今 ~170-270 天) 必被拦下。回滚/调参: env EXT_EARNINGS_MAX_PERIOD_AGE_DAYS。
EARNINGS_MAX_PERIOD_AGE_DAYS = int(os.getenv("EXT_EARNINGS_MAX_PERIOD_AGE_DAYS", "150"))

# ── 2026-09-23 P0 判据②口径修正 (DEB-20260923 §2.1/§14.3, RFC-097) ──
# 旧判据②把 company_earnings[0].period (Finnhub 归一化的**日历**季度末) 当"披露新鲜度"锚:
#   实际披露日早于该标签 >7 天的票被判 age_days < -7 ⇒ **当日真 beat 被拒**。
# 实测 (近 14 天已披露票, 9/22 快照): 123 只中 103 只负龄 (83.7%); 生产活例 US.KBH
#   (actual 1.05 / est 0.9022 / +16.38%) 于 2026-09-22 盘后被闸②拒 (logs/ext_entry.log:220)。
# 新判据②直接看"今天这组数字": 当日 Finnhub 日历行 epsActual vs epsEstimate > 0,
#   锚 = 判据①已保证的同日同相位事件; 不再用 period 做新鲜度。
# 回滚: EXT_EARNINGS_CRITERION=legacy_period (一行; cron 每 tick 新进程即生效)。
EARNINGS_CRITERION = (os.getenv("EXT_EARNINGS_CRITERION", "calendar_actual") or "").strip().lower()
EARNINGS_CRITERION_SHADOW = (
    os.getenv("EXT_EARNINGS_CRITERION_SHADOW", "true").lower() == "true"
)
# 入场涨幅带 (2026-09-23 DEB-20260923 §3.3): 空 = 沿用 ENTRY_CHANGE_PCT 下限 (旧行为)。
# 设 "0.5,2.0" ⇒ 温和票档 (回放 31 笔/18 日, 成本 0.53%: 温和票 +$23.09/笔 vs 追高 -$57.76/笔)。
ENTRY_CHG_BAND = (os.getenv("EXT_ENTRY_CHG_BAND", "") or "").strip()


# 做空侧 (2026-09-23 RFC-099): 方向由**幅度**定, 不由财报好坏定 (118 条实测: 超预期幅度 vs
#   报告日涨跌 r=-0.006; 超预期票当日仅 47% 上涨) ⇒ 空侧判据 = 跳空 ≥ SHORT_GAP_MIN。
#   **不新开链路**: 同一执行器 / 同一闸链 / 同一落库 / 同一退出链 (`premarket_exit_shadow._dir_sign`
#   早已支持 SHORT)。默认 false ⇒ 生产行为 byte 级不变; 开 = `.env.us: EXT_ENTRY_ALLOW_SHORT=true`。
SHORT_ALLOW = os.getenv("EXT_ENTRY_ALLOW_SHORT", "false").lower() == "true"
SHORT_GAP_MIN = float(os.getenv("EXT_ENTRY_SHORT_GAP_MIN", "3.0") or 3.0)

# ── 空头近失带观测 (RFC-167, 2026-10-25; record-only · 零下单 · 零改闸) ──────────
#   依据 (A10 实测): 空侧 4 笔 +$281.14 = **$70.3/笔**, 是多头 ($16.8/笔) 的 4.2 倍,
#   但 6 周只有 4 个信号 —— SHORT_GAP_MIN=3.0 把 2–3% 跳空的 fade 全挡在门外,
#   而这一档**没有任何影子在测** (grep data/shadow/ 无 short_gap*). 直接降到 2.0
#   属交易行为变更 (会改变成交集合) ⇒ 先落 record-only, 攒够样本再预注册裁决。
#   判定仍逐字为 `change_pct >= SHORT_GAP_MIN`; 观测写盘失败绝不影响下单路径。
SHORT_NEARMISS_BAND = (os.getenv("EXT_ENTRY_SHORT_NEARMISS_BAND", "") or "").strip()
SHORT_NEARMISS_DIR = ROOT / "data" / "shadow" / "ext_short_nearmiss"


def _short_nearmiss_lo():
    """近失带下界; 空/非法 ⇒ None (整段观测关闭, 不猜默认值)。"""
    if not SHORT_NEARMISS_BAND:
        return None
    try:
        return float(SHORT_NEARMISS_BAND.split(",")[0])
    except (TypeError, ValueError):
        return None


def _record_short_nearmiss(symbol, session, change_pct, price, volume, now_et):
    """把 [lo, SHORT_GAP_MIN) 的空侧近失票按 (ET日, 时段, 标的) 去重落一行 (record-only)。

    为什么按日一文件而不是 jsonl 追加: 本链每 5min 一个 tick, 同一票一天能看到 60+ 次;
    追加会把"出现过几只票"污染成"出现多少票次"(实测其他影子踩过: 同一票 2814 行)。
    首 tick 的价/涨幅 = 事件当时读数, 后续 tick 只累加 ticks/last_chg。
    """
    day = now_et.strftime("%Y-%m-%d")
    path = SHORT_NEARMISS_DIR / (day + ".json")
    SHORT_NEARMISS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        doc = {}
    rows = doc.get("rows") or {}
    key = "%s:%s" % (session, symbol)
    prev = rows.get(key)
    if prev:
        prev["ticks"] = int(prev.get("ticks") or 1) + 1
        prev["last_chg"] = round(float(change_pct), 2)
        prev["last_ts"] = now_et.isoformat()
    else:
        rows[key] = {"symbol": symbol, "session": session,
                     "chg_pct": round(float(change_pct), 2),
                     "px": round(float(price), 4),
                     "volume": int(volume or 0),
                     "first_ts": now_et.isoformat(),
                     "last_ts": now_et.isoformat(), "ticks": 1}
    doc["et_date"] = day
    doc["short_gap_min"] = SHORT_GAP_MIN
    doc["band_lo"] = _short_nearmiss_lo()
    doc["rows"] = rows
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    os.replace(str(tmp), str(path))


def _classify_ext_direction(symbol, session, change_pct, price, volume):
    """方向判定 SSOT (RFC-099 语义, 逐字不变) + 空头近失带记录 (RFC-167)。

    判定 = `SHORT_ALLOW and change_pct >= SHORT_GAP_MIN` —— 与两条扫描臂的历史内联式等价。
    近失带只在**判完之后**追加一行观测, 整段包 try/except: 埋点异常绝不允许改变
    "这一票开不开、开哪一边" (观测面与决策面解耦, 同 2026-09-23 判据②的纪律)。
    """
    is_short = bool(SHORT_ALLOW and change_pct >= SHORT_GAP_MIN)
    if SHORT_ALLOW and not is_short:
        _lo = _short_nearmiss_lo()
        if _lo is not None and _lo <= change_pct < SHORT_GAP_MIN:
            try:
                _record_short_nearmiss(symbol, session, change_pct, price, volume, _now_et())
            except Exception:
                _log.warning("short nearmiss shadow 写入失败 (不影响判定)", exc_info=True)
    return "SHORT" if is_short else "LONG"


def _entry_chg_ok(change_pct):
    """入场涨幅闸: 有 ENTRY_CHG_BAND ⇒ 带内通过; 无 ⇒ 旧口径 (>= ENTRY_CHANGE_PCT)。

    解析失败 fail-safe 回旧口径 (不得因配置笔误把入场闸放开)。
    """
    if ENTRY_CHG_BAND:
        parts = ENTRY_CHG_BAND.split(",")
        if len(parts) == 2:
            try:
                lo, hi = float(parts[0]), float(parts[1])
                return lo <= change_pct <= hi
            except (TypeError, ValueError):
                print(f"[ext_entry] EXT_ENTRY_CHG_BAND 解析失败 ({ENTRY_CHG_BAND!r}) → 回退 ENTRY_CHANGE_PCT")
    return change_pct >= ENTRY_CHANGE_PCT


def _research_chg_ok(change_pct):
    """研究臂财报源的独立涨幅带; 解析失败回严格带 (fail-safe, 不放大)。"""
    parts = RESEARCH_CHG_BAND_RAW.split(",")
    if len(parts) == 2:
        try:
            lo, hi = float(parts[0]), float(parts[1])
            return lo <= change_pct <= hi
        except (TypeError, ValueError):
            print(f"[ext_entry] EXT_ENTRY_RESEARCH_CHG_BAND 解析失败 "
                  f"({RESEARCH_CHG_BAND_RAW!r}) → 回退严格带")
    return _entry_chg_ok(change_pct)
# P1-4 盘前保留额度条件化: PREMARKET_MAX_PER_DAY 的目的是"盘后可能还有机会";
#   当日日历**没有任何 after/post 相位财报**时, 保留是纯机会成本 ⇒ 释放盘前第 3 笔。
#   日历源故障/空 → 保守视为"有" (继续保留)。回滚: EXT_PREMARKET_RESERVE_CONDITIONAL=false。
PREMARKET_RESERVE_CONDITIONAL = (
    os.getenv("EXT_PREMARKET_RESERVE_CONDITIONAL", "true").lower() == "true"
)
# ↑ 价格下限闸 (2026-09-17 用户裁方案A): 挡 $5 以下仙股/妖股.
#   实证: CTNT $0.073(+75.06%, 量2217万, Finnhub超预期+105%) 与 GIPR $0.435(+19.75%)
#   曾透过"事件+涨幅+量"三闸 —— 仙股的第1闸(超预期>0)是季末噪声, 第2/3闸对妖股天然放行。
#   事故代价: 日配额 2 笔被噪声票用尽, 当晚真催化票开不出来。回滚=env EXT_ENTRY_MIN_PRICE=0。

# ── 候选源开关 (2026-09-17 用户令「需要」: 接入常见 watchlist 与财报机会) ──
#   架构: 每个源只产出候选结构 {symbol,px,chg,vol,prev_close}; 下游四闸/价格下限/配额/下单/
#   平仓/台账**零改动**。默认只有现役异动榜开; 新源等回放证据
#   (scripts/replay_ext_entry_sources.py, 用 K_5M+extended_time=True 取历史盘前 bar) 出来再开。
SOURCE_RANK_BOARD = os.getenv("EXT_ENTRY_SOURCE_RANK", "true").lower() == "true"
SOURCE_BEAT_RANK = os.getenv("EXT_ENTRY_SOURCE_BEAT_RANK", "false").lower() == "true"
SOURCE_WATCHLIST = os.getenv("EXT_ENTRY_SOURCE_WATCHLIST", "false").lower() == "true"
# 事件源: **当日同相位财报日历名单** (2026-09-23 供给归因).
#   起因: 严格臂的准入集合 = "当日同相位财报事件", 而候选池 = 按涨跌幅排序的异动榜 ⇒ 两者
#   交集靠运气。实测 2026-09-22 盘后: 榜 42 只与日历交集 0, 而当日盘后财报票 US.KBH
#   (after $49.39/+3.19%/盘后量 26.1 万/市值 2.98B) 四条地板全过却**不在榜内** (榜被 +20%
#   仙股挤掉) ⇒ 合格票从未进闸。本源把日历本身并入候选池: **语义与准入集合对齐, 不放松
#   任何一道闸** (价/动量/量/市值/成交额/分级/结构/费用/盘口/仓位帽全部照旧)。
#   默认 false (扩容方向的安全侧); 开关与回滚点: `.env.us: EXT_ENTRY_SOURCE_CALENDAR`.
SOURCE_CALENDAR = os.getenv("EXT_ENTRY_SOURCE_CALENDAR", "false").lower() == "true"


# 盘前配对扩展 (2026-09-23, RFC-102; 用户裁「A」= 预注册后直接转正):
#   实测 9/23 盘前: 只认「当日 BMO」事件, 而当日 BMO 的 actual 在盘前窗内**结构性不可得**
#   (6 只事件票 5 只到 08:10 ET 仍 actual_missing; 唯一放行的 CBRL 也迟到至 08:10;
#    交叉验第二源 Nasdaq 同日同票同样 eps=None)。而「昨日 AMC」票的 actual 次日早晨已入库
#   (9/22 日历 8 行有 7 行 actual) ⇒ 把昨日 AMC 纳入盘前人群 (人群与数据可得性对齐)。
#   默认 false ⇒ 生产行为 byte 级不变; 开 = `.env.us: EXT_ENTRY_PREMARKET_AMC_PAIRING=true`;
#   回滚 = 改回 false (下一 tick 生效, 无需重启)。
PREMARKET_AMC_PAIRING = os.getenv("EXT_ENTRY_PREMARKET_AMC_PAIRING", "false").lower() == "true"


def _shift_et_day(day_str, offset):
    """ET 日历日字符串 ± 天数 (不可解析 ⇒ 返回原值, fail-safe)。"""
    try:
        base = datetime.strptime(str(day_str)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return str(day_str)[:10]
    return (base + timedelta(days=offset)).isoformat()


def _session_event_pairs(session):
    """session → 允许的 (日偏移, 相位) 列表 —— 相位配对的唯一真源。

    日偏移: 0=当日, -1=前一日 (ET 日历日)。相位: "before"=BMO, "after"=AMC。
    盘后仍只认当日 AMC (与线上一致); 盘前在开关开启时追加「昨日 AMC」。
    """
    if session == "premarket":
        pairs = [(0, "before")]
        if PREMARKET_AMC_PAIRING:
            pairs.append((-1, "after"))
        return pairs
    return [(0, "after")]
# Finnhub 日历候选源 (2026-09-23 供给归因第二源): 当日 Finnhub 日历中**同相位**票并入候选池。
#   起因: 生产主源 (futu 原生日历) 7 日窗只覆盖 Finnhub 的 39% (实测 34 vs 87 只), 且
#   `pub_type=REGULAR` 的票相位未标定 ⇒ 结构性缺席 (ET 9/23 最大 bmo 票 US.CTAS $79.7B 因此进不了池;
#   实测 7 日窗 34 行里 4 行 REGULAR = 11.8%)。
#   **零资格变更**: 本源只让票"进闸", 判据① 的相位判据仍只认 futu/AV 日历 ⇒ 不产生新入场;
#   作用是让 calendar_phase_unknown / calendar_missing 这些票进入 reject 台账, 攒出
#   "若做相位交叉回补 (futu 未标定 ⇒ 取 Finnhub 相位) 能多放多少"的实测证据。
#   回滚: 删/置 false (本源独立开关, 不影响其他源)。
SOURCE_FINNHUB_CALENDAR = os.getenv("EXT_ENTRY_SOURCE_FINNHUB_CALENDAR", "false").lower() == "true"
# 判据① 事件源 (2026-09-23 用户令「先可见再打磨优化」): calendar (默认, 现行为) | calendar_finnhub_union
#   union = futu/AV 日历优先; futu **无该票行**或**相位未标定**(REGULAR → 空相位) 时, 回落到
#   Finnhub 当日日历行 (存在性 + 相位)。futu 明确标了不同相位 ⇒ 仍以 futu 为准 (保守, 不覆盖已知相位)。
#   为什么必须并源: 主源只覆盖 Finnhub 的 39%, 相位未标定 11.8% (活例 US.CTAS $79.7B) ⇒ 生产候选池
#   远小于**入场规则读数的票池**(回放池 = Finnhub 口径全量) ⇒ 实测每轮 0 单, 通道空转。
#   影响面: 只放宽"事件存在性/相位"这一条; 市值/价/成交额/价差/深度/费用/涨幅带/量闸全不变,
#   判据② (当日 actual vs est > 0) 仍是硬门 ⇒ 仍必须真出财报且超预期。
#   回滚: 置 calendar / 删 .env.us 该键 (一行回滚, 无数据迁移)。
EARNINGS_EVENT_SOURCE = os.getenv("EXT_EARNINGS_EVENT_SOURCE", "calendar").lower()
# 事件源观测 (每轮打印后清空): 让"这条票是哪个源认出来的"可见 — 纯观测, 不参与判定。
#   按**票去重** (集合) —— 早期用计数会随调用次数放大 (实测同一 tick 内一票被调两次 ⇒ 读数 ×2)。
_EVENT_SRC_OBS = {"futu": set(), "finnhub": set()}
# RFC-100 量闸分档反事实累加器 (每轮打印后清零; 纯观测)
_VOL_BUCKET_CF = {"live": 0, "shadow": 0, "shadow_in_band": 0, "thr": None,
                  "symbols": [], "shadow_symbols": []}
WATCHLIST_GROUP = os.getenv("EXT_ENTRY_WATCHLIST_GROUP", "美股")
BEAT_TOP_N = int(os.getenv("EXT_ENTRY_BEAT_TOP_N", "200"))
BEAT_RECENT_DAYS = int(os.getenv("EXT_ENTRY_BEAT_RECENT_DAYS", "7"))
BEAT_MIN_MCAP = float(os.getenv("EXT_ENTRY_BEAT_MIN_MCAP", "1000000000"))
SNAPSHOT_BATCH = 200
STOP_PCT = -5.0            # 止损距离 (%) — 开盘写入 initial_sl, 由规则引擎盘中兜底
SCAN_TOP_N = 50            # 盘前/盘后榜扫描深度 (9/17 扩覆盖面: 官方榜 top50, 旧为15只活跃股)
# 2026-09-17 用户裁方案1: SIM 无券商STOP → 接受规则引擎兜底, 开关恢复下单.
#   止损保障: ①开盘 monitor_playbook_sim_exits 读 initial_sl 兜底 ②盘前 tighten 信号由 exit executor 平仓.
# 2026-09-18 (Hermes §23 R8): 代码默认由 "true" 翻为 **"false"** (fail-closed) —
#   原默认是 fail-open: `.env.us` 缺失/未加载/键名写错 = **静默开仓**。开关现在真的在下单,
#   故默认方向必须偏安全侧 (宁不交易, 不乱交易)。当前行为不变: `.env.us:135` 显式
#   `EXT_ENTRY_ALLOW_SIM=true` 且 load_dotenv 先于本行执行 ⇒ 观察期照常开仓;
#   回滚 = 把本行默认改回 "true"。
ALLOW_SIM_ENTRY = os.getenv("EXT_ENTRY_ALLOW_SIM", "false").lower() == "true"

# ── SIM 研究臂 (2026-09-22 用户裁「SIM 研究臂」; DEB-20260920 §12 预注册) ──────────
# 背景: 财报季空窗 × "当日同相位事件"门 ⇒ 供给结构性饿死 (9/21-9/22 零成交;
#   三源日历(futu 6/AV 18/Finnhub 8)与候选池交集恒 ∅, 见 carry 影子脚本头)。
# 用户裁: 更早恢复 SIM 交易, 用真实成交攒数据 + 打磨入场→持仓→退出→结算全链。四条纪律:
#   ① 严格臂优先 — 先跑现行财报门吃满配额, 研究臂只吃剩余 (财报季回归零挤占);
#   ② 全链打标 — attempts/audit/open_positions/trade_records.metadata_json/推送
#      带 gate_mode=research_momentum, 样本与严格臂永不混算, 研究臂读数不构成转正证据;
#   ③ SIM-only 硬连线 — _mode().is_simulate=False (含异常) 即 fail-closed, 与
#      EXT_REAL_AUTHORIZED 双闸并列, REAL 下研究臂物理不可达;
#   ④ 预注册退场线 — EXT_RESEARCH_ARM_UNTIL (默认 2026-10-31) 过期自动停 (日期闸在
#      代码里, 不靠人工撤); 早退条款: 严格臂连续 5 个交易日有成交 → 裁决降级/退役。
# 人群定义 (宁缺毋滥): 仅 **calendar_missing** (当日日历完全无事件) 被拒候选入研究池 —
#   与 near-miss 台账同人群; calendar_phase_mismatch/unknown、surprise_*、源故障
#   一律仍 fail-closed (事件不确定/数据缺失票不是"非财报动量"研究对象)。
# 在仓帽加严: 研究臂**每单前**复查 _open_exposure_gate (严格臂为每轮一次) —
#   研究臂是补充供给, 不得把 EXT 总敞口推过 MAX_OPEN/MAX_OPEN_NOTIONAL。
# 回滚: EXT_ENTRY_RESEARCH_MOMENTUM=false (一键; cron 每 tick 新进程, 无需重启)。
RESEARCH_ARM_ON = os.getenv("EXT_ENTRY_RESEARCH_MOMENTUM", "false").lower() == "true"
RESEARCH_ARM_UNTIL = os.getenv("EXT_RESEARCH_ARM_UNTIL", "2026-10-31")
# 新 source 专用死线: 旧 research_momentum 臂仍按 RESEARCH_ARM_UNTIL 停;
#   2026-10-19 起第 20 个交易日 = 2026-11-13 (11/26 感恩节不计, 11/11 美股正常交易)。
RESEARCH_SOURCE_UNTIL = os.getenv("EXT_RESEARCH_SOURCE_UNTIL", "2026-11-13")
RESEARCH_GATE_MODE = "research_momentum"
# 2026-10-18 财报日盘前影子: 研究臂专用源 + 独立涨幅带 + 源级漏斗。
#   严格臂源开关/带宽/闸门零改动; 这里只把 beat/watchlist 追加进研究池。
RESEARCH_SOURCES_ON = os.getenv("EXT_ENTRY_RESEARCH_SOURCES", "false").lower() == "true"
# 2026-10-18 实源探测: beat 源 14 只里 8 只落在 0.5–2%、0 只落在 2–10%
#   ⇒ 研究带取 0.5–10%，避免把财报超预期大票全部挡掉；严格臂仍 0.5–2.0。
RESEARCH_CHG_BAND_RAW = (os.getenv("EXT_ENTRY_RESEARCH_CHG_BAND", "0.5,10.0") or "").strip()
RESEARCH_MIN_PRICE = float(os.getenv("EXT_ENTRY_RESEARCH_MIN_PRICE", str(MIN_PRICE)) or MIN_PRICE)
RESEARCH_MAX_SYMBOLS = int(os.getenv("EXT_ENTRY_RESEARCH_MAX_SYMBOLS", "80"))
RESEARCH_FUNNEL = ROOT / "data" / "ext_entry_research_funnel.jsonl"


def _research_arm_active():
    """旧 research_momentum 臂本轮是否启用: env 开关 + 未过预注册死线 + SIM 环境。"""
    if not RESEARCH_ARM_ON:
        return False
    if _today_et() > RESEARCH_ARM_UNTIL:   # ISO 日期串可直接字典序比较
        return False
    try:
        return bool(_mode().is_simulate)   # SIM-only 硬连线; 判定异常 fail-closed
    except Exception:
        return False


def _research_sources_active():
    """新 source 影子是否启用: 独立死线, 不复用旧臂 10-31 的截止。"""
    if not (RESEARCH_ARM_ON and RESEARCH_SOURCES_ON):
        return False
    if _today_et() > RESEARCH_SOURCE_UNTIL:
        return False
    try:
        return bool(_mode().is_simulate)
    except Exception:
        return False


def _research_existing_symbols(research_pool, results):
    """只排除已入研究池 / 严格臂已下单的标的; 被严格带拒绝的票仍可进 research 带。"""
    existing = {str(item.get("symbol") or "") for item in research_pool}
    existing |= {str(r.get("symbol") or "") for r in results if r.get("ok")}
    existing.discard("")
    return existing


def _record_research_funnel(session, today, item, stage, reason=""):
    """源级漏斗 (只读 append): 每条研究候选的选取/拒绝/下单去向。"""
    if not RESEARCH_SOURCES_ON:
        return
    try:
        row = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "trade_date": today,
            "session": session,
            "symbol": item.get("symbol"),
            "source": item.get("_source") or "",
            "stage": stage,
            "reason": reason,
            "price": item.get("px"),
            "change_pct": item.get("chg"),
            "volume": item.get("vol"),
            "gate_mode": RESEARCH_GATE_MODE,
        }
        RESEARCH_FUNNEL.parent.mkdir(parents=True, exist_ok=True)
        with RESEARCH_FUNNEL.open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        _log.warning("research funnel write failed", exc_info=True)


def _research_source_candidates(session, qctx, existing):
    """研究臂专用财报源: beat rank + watchlist, 只进研究池, 严格臂不可见。"""
    if not _research_sources_active():
        return []
    try:
        beat = _beat_rank_symbols(qctx)
        watch = _watchlist_symbols(qctx)
    except Exception as exc:
        _log.warning("research source fetch failed", exc_info=True)
        print(f"[ext_entry] 研究臂财报源不可用 (跳过本轮): {type(exc).__name__}: {exc}")
        return []
    source_of = {sym: "research_beat" for sym in beat}
    for sym in watch:
        source_of.setdefault(sym, "research_watchlist")
    symbols = [sym for sym in dict.fromkeys(list(beat) + list(watch))
               if sym and sym not in existing][:RESEARCH_MAX_SYMBOLS]
    if not symbols:
        return []
    try:
        rows = _snapshot_candidates(session, qctx, symbols)
    except Exception as exc:
        _log.warning("research source snapshot failed", exc_info=True)
        print(f"[ext_entry] 研究臂财报源快照失败 (跳过本轮): {type(exc).__name__}: {exc}")
        return []
    out = []
    today = _today_et()
    for item in rows:
        try:
            price = float(item.get("px") or 0)
            change = float(item.get("chg") or 0)
            volume = float(item.get("vol") or 0)
        except (TypeError, ValueError):
            continue
        item["_source"] = source_of.get(item.get("symbol"), "research")
        # 源头预过滤: 价格下限 + 独立财报涨幅带 + 有量; 其余闸交给研究臂下游。
        if price < RESEARCH_MIN_PRICE:
            _record_research_funnel(session, today, item, "reject", "research_price_gate")
            continue
        if volume <= 0:
            _record_research_funnel(session, today, item, "reject", "research_volume")
            continue
        if not _research_chg_ok(change):
            _record_research_funnel(session, today, item, "reject", "research_chg_band")
            continue
        out.append(item)
    return out


# ── 交易模式/账户: 单一真相源 (2026-09-20 切实盘开关清单化) ──────────────────
# 旧实现自带一套 legacy 判定 (`FUTU_TRD_ENV` + 常量 `TrdEnv.SIMULATE` + 裸读
# `SIMULATE_ACCOUNT_ID`), 与主链路 (di_container / AUTO_TRADE_ACCOUNT) 不同源 →
# 切实盘时会错配。现全部经 `TradingMode` 解析; ETH 真钱另需 `EXT_REAL_AUTHORIZED=true`
# (ETH 链从未在实盘跑过)。回滚 = 删本块并还原 FUTU_TRD_ENV 读取。
def _mode():
    from app.core.trading_mode import TradingMode

    return TradingMode()


def _ext_real_authorized() -> bool:
    from app.core.trading_mode import ext_channel_real_authorized

    return ext_channel_real_authorized()


AUDIT = ROOT / "data" / "extended_hours_entry_executions.jsonl"
# 价格闸拦下的候选打点 (按**运行**聚合一行: n + 明细). 用户 2026-09-17 令: 回答
#   "$5 闸到底挡掉了多少噪声票". 逐候选整行会膨胀(同票每 5 分钟重复出现) ⇒ 按运行聚合.
PRICE_GATE_REJECTS = ROOT / "data" / "extended_hours_price_gate_rejects.jsonl"
EXEC_PRICE_SHADOW = ROOT / "data" / "ext_entry_exec_price_shadow.jsonl"
# 2026-09-19 用户令「按建议执行」: 入口预筛影子 — **只记录不拦**。
#   动机 (回放 60 日): 42% 的延长时段仓**从未出现 0.4% 浮盈**, 单独贡献 -132pp; 有肉的
#   25 笔合计 +116pp ⇒ "每笔净收益不亏损"的正解在**入口**, 不在出口。
#   第一版信号 (观察级 n=43): 进场涨幅>5% 有肉率 83%/均值+3.59%; 价格 $5~20 89%/+3.89;
#   >$20 仅 50%/-1.49。本影子把每个候选的 (涨幅/价格档/时段量) 与"现役闸 vs 更严闸"的
#   假设判定逐次落盘, 攒样后与真实结局 join 定阈值。零交易影响 (纯 append + 异常吞掉)。
PREFILTER_SHADOW = ROOT / "data" / "ext_entry_prefilter_shadow.jsonl"
PREFILTER_SHADOW_ON = os.getenv("EXT_PREFILTER_SHADOW", "false").lower() == "true"
PRETRADE_GATE_REJECTS = ROOT / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
# P0-2 earnings 闸拒绝台账 (2026-09-20, DEB-20260920 §10): 每次运行聚合一行,
#   counts=子原因计数, details=除 calendar_missing 外的逐票明细 (罕见且有诊断价值)。
#   §9 的"10 日 0 笔先归因" 判断依据此文件区分: 市场无事件(calendar_missing 主导) vs
#   源故障(calendar_source_error/finnhub_error) vs 相位字段不匹配(calendar_phase_*) vs
#   surprise 过期(surprise_stale)。没有它, 闸计数只有一个 earnings=N, 无法裁决放不放宽。
EARNINGS_GATE_REJECTS = ROOT / "data" / "extended_hours_earnings_gate_rejects.jsonl"
EARNINGS_CRITERION_SHADOW_PATH = ROOT / "data" / "ext_entry_earnings_criterion_shadow.jsonl"
# 2026-09-22 零成交复盘: 记录"价格/动量/量能/成交额已通过, 仅因 calendar_missing 被拒"
# 的 near-miss。按 (trade_date, session, symbol) 去重, 只做观察, 不参与交易判定。
EARNINGS_NEAR_MISSES = ROOT / "data" / "ext_entry_earnings_near_miss.jsonl"
EARNINGS_NEAR_MISS_ON = (
    os.getenv("EXT_ENTRY_EARNINGS_NEAR_MISS", "true").lower() == "true"
)
STATE = ROOT / "data" / "extended_hours_entry_state.json"
LOCK = ROOT / "data" / "extended_hours_entry_state.lock"
DB = ROOT / "data" / "ftseek_us.db"
SESSION_WINDOWS = {
    "premarket": (400, 929),
    "afterhours": (1600, 1959),
}
ENTRY_WINDOWS = {
    "premarket": {"A": (730, 915), "B": (815, 915)},
    "afterhours": {"A": (1620, 1830), "B": (1620, 1730)},
}
# P1-3 VWAP 锚点 (2026-09-20, DEB-20260920 §10.2/§10.7): 旧口径从 4:00 起算全时段 VWAP,
#   凌晨薄量 bar 把锚拖在旧水位 ⇒ 平稳爬升的强势票到成熟窗时距 VWAP 常 >0.8%,
#   被 vwap_extended 误拒 — 恰好拒掉动量延续型 (主盈利形态)。
#   锚点 = "流动性可接受的时段起点": 盘前 07:00 (排除 4:00-7:00 凌晨死区; 7:00+ 对
#   今日有财报的候选普遍有真实事件量), 盘后 16:00 (= 全时段起点: 盘后无凌晨病灶,
#   且 16:00-16:20 是发布后量最重/价格发现最真的 20 分钟, 排除它 VWAP 反而失代表性,
#   还会在 A/B 窗开头制造 bars<4 预热拒绝, 把盘后 B 有效窗压到 50min — 见 §10.7 用户质询)。
#   此锚点下四段入场窗的预热拒绝均归零 (盘前 A 07:30 时已有 ≥6 根 bar; 盘后 16:20 时 ≥4 根)。
#   回滚: EXT_VWAP_ANCHOR_MATURITY=false (回到全时段窗, 旧行为)。
VWAP_ANCHOR_MATURITY = os.getenv("EXT_VWAP_ANCHOR_MATURITY", "true").lower() == "true"
VWAP_ANCHOR_WINDOWS = {
    "premarket": (700, 929),
    "afterhours": (1600, 1959),
}

_EARNINGS_CALENDAR_CACHE = {}

from scripts.extended_hours_order_lifecycle import (
    ACTIVE_ORDER_STATUSES,
    query_order_rows,
    wait_order_fill,
)


def _now_hhmm_et():
    now = _now_et()
    return now.hour * 100 + now.minute


def _in_session_window(session):
    start, end = SESSION_WINDOWS[session]
    return start <= _now_hhmm_et() <= end


def _entry_window_reason(session, grade):
    windows = ENTRY_WINDOWS.get(session, {})
    start, end = windows.get(grade, (0, 0))
    if not start <= _now_hhmm_et() <= end:
        return "entry_window_not_mature"
    return None


def _entry_grade(price, volume, market_cap):
    market_cap = float(market_cap or 0)
    if market_cap <= 0:
        return None, "market_cap_missing"
    if market_cap < MIN_MARKET_CAP_USD:
        return None, "market_cap_too_low"
    turnover = float(price or 0) * float(volume or 0)
    if turnover < MIN_EXT_TURNOVER_USD:
        return None, "turnover_too_low"
    if turnover >= A_GRADE_TURNOVER_USD or volume >= 20_000_000:
        return "A", None
    return "B", None


def _is_trading_day():
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _today_et():
    return _now_et().strftime("%Y-%m-%d")


def _is_late_premarket(session: str) -> bool:
    """盘前单是否在 09:00 ET 后入场 (只读标记, 不拦单).

    2026-10-20 10-19 复查: 研究臂 NVDA 09:15 盘前入场后开盘反转, 唯一大亏。
    先记录晚入场 cohort, 20 日观察后再决定是否加时点闸。
    """
    if str(session) != "premarket":
        return False
    try:
        now = _now_et()
        return now.hour * 60 + now.minute >= 9 * 60
    except Exception:
        return False


def _et_hour_now() -> int:
    """当前 ET 小时 (0-23); 取不到返回 -1 (调用方据此回落默认门槛)。"""
    try:
        return int(_now_et().hour)
    except Exception:
        return -1


def _volume_buckets(raw=None) -> list:
    """解析 "4-7:20000,8-9:200000" ⇒ [(4,7,20000),(8,9,200000)]。

    解析失败的片段**忽略** (fail-safe ⇒ 回落到 MIN_VOLUME, 不会因手误把门槛调到 0)。
    """
    out = []
    text = MIN_VOLUME_BUCKETS_RAW if raw is None else raw
    for part in str(text or "").split(","):
        rng, _, thr = part.partition(":")
        lo, _, hi = rng.partition("-")
        parsed = _parse_bucket_triple(lo, hi, thr)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_bucket_triple(lo, hi, thr):
    """"4","7","20000" ⇒ (4,7,20000); 任一非法 ⇒ None (调用方跳过该片段)。

    刻意**不用** `except: continue` 形态: 仓库 `tests/test_except_pass_ratchet.py` 的棘轮
    把该形态计为新增静默异常 (§新增代码不得再增计数) ⇒ 这里 return None, 由调用方判定。
    """
    try:
        lo_i, hi_i, thr_i = int(lo), int(hi or lo), int(float(thr))
    except (TypeError, ValueError):
        return None
    return (lo_i, hi_i, thr_i) if (0 <= lo_i <= 23 and lo_i <= hi_i <= 23 and thr_i > 0) else None


def _min_volume_for(raw, hour) -> int:
    """按 ET 小时取门槛; 无命中/参数非法 ⇒ MIN_VOLUME (即未分档行为)。"""
    try:
        h = int(hour)
    except Exception:
        return MIN_VOLUME
    for lo, hi, thr in _volume_buckets(raw):
        if lo <= h <= hi:
            return thr
    return MIN_VOLUME


def _min_volume_now(session=None, hour=None) -> int:
    """当前应生效的量闸门槛 (RFC-100 分档; 无配置 ⇒ MIN_VOLUME)。"""
    return _min_volume_for(None, _et_hour_now() if hour is None else hour)


def _load_state():
    if not STATE.exists():
        return {"version": 2, "opened_today": {}, "attempts": {}}
    try:
        state = json.loads(STATE.read_text())
    except Exception as exc:
        raise RuntimeError(f"extended-hours entry state unreadable: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError("extended-hours entry state is not a JSON object")
    return state


def _save_state(s, state_path=None):
    state_path = Path(state_path or STATE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(f".{os.getpid()}.tmp")
    with tmp.open("w") as handle:
        json.dump(s, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, state_path)


@contextlib.contextmanager
def _state_lock(state_path=None):
    lock_path = (state_path or STATE).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _probe_env_only():
    """**显式**探针标志: 只认 `FUTSEEK_PROBE=1`, 不含 pytest 检测。

    与 `_probe_mode()`(推送静默, 含 pytest) 刻意分开: 下单口的短路必须只由人工显式置位触发,
    否则任何 pytest 进程都会封死下单路径 ⇒ 测试对下单链的断言全灭 (2026-09-23 实测)。
    """
    return os.getenv("FUTSEEK_PROBE", "").strip().lower() in ("1", "true", "yes")


def _probe_mode():
    """探针/测试运行的**静默开关** (四条推送出口共用的唯一判据)。

    2026-09-18 (§23.5): 用户看到的"连续推送虚拟标的"里有一类来自**人工/探针以 REAL 环境跑生产脚本**
    —— 生产 cron 从未设过该 SIM/REAL 环境键（历史键名 FUTU_TRD_ENV，现已废弃并由
    `TradingMode` 取代）、审计里也没有占位行。
    探针验证 REAL 分支时不该把生产 IM 当输出通道：置 `FUTSEEK_PROBE=1` 即静默（并留可见日志）。
    生产环境 unset ⇒ 行为完全不变（告警照常）。

    2026-09-19 (推送污染收口): 追加"**进程内在跑 pytest** 同样静默"。事故实证 —— 单测 fixture 的
    建仓成交富卡 (`US.FAKE` 100@10.0, tests/test_extended_hours_order_execution.py:136) 真发到生产 IM,
    而出口封锁只挂在 `tests/conftest.py` 上: 任何**绕过 conftest 的跑法** (`--noconftest`、裸 import
    测试模块) 都不受它保护。判据放在本函数 ⇒ `_push` / `_push_once` / `_push_filled` / `_dedup_reserve`
    同一开关, 不留第二处。
    (不用 pytest.ini 的 `env =`: 那需要 pytest-env 插件, 本机 venv 未装 ⇒ 写进 ini 只会得到
     "Unknown config option" 警告并**静默不生效** —— 看似修好实则空操作。)
    生产服务不加载 pytest (实测 `app/`、`scripts/` 均无 `import pytest`) ⇒ 生产行为零变化。
    """
    if os.getenv("FUTSEEK_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return True
    return "pytest" in sys.modules or bool(os.getenv("PYTEST_CURRENT_TEST"))


def _stage(session: str) -> str:
    """阶段中文名 — 委托 app/core/trade_identity.phase_cn (全库唯一 SSOT)。

    原实现把文案硬编码成「盘前…」——而本执行器 `--session afterhours` 时
    (实测 9/18 US.WBD 18:35 ET 建仓) 推送却说"盘前事件开仓", 阶段说错。
    """
    from app.core.trade_identity import phase_cn
    return phase_cn(session) or "延长时段"


def _push(text, level=None, category=None):
    """统一推送出口 — 走 PushHub (2026-09-18 用户裁「统一推送渠道」)。

    原实现直连 `FeishuNotifier().send_text()` ⇒ 与本系统其余推送
    (PushHub → 钉钉主通道) 分流: 同一笔交易"成交在飞书、平仓在钉钉",
    且丢掉 Hub 的去重/聚合/健康检查/失败兜底。
    """
    if _probe_mode():
        print(f"[ext_entry][probe] 推送抑制: {text}")
        return False
    try:
        from app.push import push_text_safe
        from app.push.message import Level, Category
        return push_text_safe(
            text,
            level=level if level is not None else Level.HIGH,
            category=category if category is not None else Category.RISK_ALERT,
        )
    except Exception:
        return False


def _crit_level():
    """CRITICAL 级别 (延迟导入 app.push — 与文件内其余 app.* 用法同风格)。"""
    from app.push.message import Level
    return Level.CRITICAL


def _push_filled(symbol, fqty, fpx, plan_px, session, earn, chg, vol, rec, pos_id):
    """建仓成交 → 统一交易富卡 (TRADE_ENTRY, 双发钉钉+飞书)。

    2026-09-18 (用户: 「统一推送渠道, 也要保持盘前和盘后以及盘中的真实内容」):
    原为飞书专属纯文本 4 行; 现走统一 `trade_entry` 富卡, 并把原有信息
    (财报超预期 / 阶段涨跌 / EXT 量 / initial_sl 与来源 / SIM 无券商 STOP 兜底)
    以结构化字段传入 —— 版式统一但内容一行不丢。
    """
    if _probe_mode():
        print(f"[ext_entry][probe] 成交推送抑制: {symbol} {fqty:g}@{fpx}")
        return False
    # 去重 (2026-09-19): 同一持仓的成交富卡每 ET 日只发一次。pos_id 每次建仓都变 ⇒ 同日真再入场
    #   不受影响; 会被收敛掉的只可能是**同一笔成交被反复演练/重放** (此前可无限外发 —— 与用户
    #   "今天上午还有这种推送" 的观察一致)。
    if not _dedup_reserve(f"ext_entry_filled:{symbol}:{pos_id}"):
        print(f"[ext_entry] 成交富卡本 ET 日已发过, 跳过: {symbol} {pos_id}")
        return False
    try:
        from app.push import get_push_hub
        from app.push.message import trade_entry as _m
        hub = get_push_hub()
        # 研究臂打标 (2026-09-22, DEB-20260920 §12 纪律②): 卡片 strategy 与 extra_lines
        #   双标, IM 里与严格臂一眼可分; 落库 owner_strategy 不变 (退出链复用)。
        gate_mode = rec.get("gate_mode") or "strict"
        is_research = gate_mode == RESEARCH_GATE_MODE
        extra_lines = [f"🆔 延长时段持仓: {pos_id}"]
        if is_research:
            extra_lines.insert(0, "🧪 研究臂·非财报动量 (SIM 样本, 不入严格臂台账)")
        # 方向文案跟实际 side 走 (2026-10-01 [hotfix]): 原硬编码 "BUY" ⇒ ETH 首笔空单
        #   (ACN 10/1, 券商 SELL/持仓 -23 股/DB SHORT 三源一致) 推送却显示"买入/做多"。
        #   rec["side"] ∈ {"LONG","SHORT"} (_execute 构造 rec 时必带); 缺失回退 "BUY" = 原字面行为。
        _is_short = str(rec.get("side") or "").upper() == "SHORT"
        _sl_sign = "+" if _is_short else "-"   # 空头 SL 在上方, 多头在下方 (与 initial_sl 几何同源)
        # 方向依据成行 (2026-10-13 用户裁「修」): RFC-099 方向由跳空幅度定、财报好坏不定
        #   方向 —— 同一张「财报超预期」上下文行下多空并存会被误读为方向矛盾 (C +1.32% 多
        #   vs GS +3.32% 空 实例)。仅严格臂: 研究臂判据是非财报动量 (已有 🧪 标), 不得
        #   冒用 RFC-099 依据。措辞与 _direction 三元式同源 (SHORT=跳空≥闸 / LONG=未及闸),
        #   不复述入场带 — 带是 LONG 资格闸, 到不了推送的必然已在带内。
        if not is_research:
            if _is_short:
                extra_lines.append(
                    f"📐 方向依据: 盘前跳空 {chg:+.2f}% ≥ 跳空闸 {SHORT_GAP_MIN:g}%"
                    " → 回落做空 (RFC-099 幅度定方向, 财报不定方向)")
            else:
                extra_lines.append(
                    f"📐 方向依据: 盘前 {chg:+.2f}% < 跳空闸 {SHORT_GAP_MIN:g}%"
                    " → 动量做多 (RFC-099 幅度定方向, 财报不定方向)")
        return bool(hub.push(_m(
            symbol=symbol, direction=("SELL_SHORT" if _is_short else "BUY"),
            qty=int(fqty), price=float(fpx),
            strategy=(
                "extended_hours_research" if is_research else "extended_hours_entry"
            ),
            trace_id=pos_id,
            sl=rec.get("initial_sl"), source_type="playbook",
            session=session,
            extra={
                "earnings_surprise_pct": earn.get("surprise_pct"),
                "ext_change_pct": chg,
                "ext_volume": vol,
                "gate_mode": gate_mode,
                # 符号跟 side 走 (同一 hotfix): 空头 SL=entry×1.05 在上方, 原文案恒 "-5%" 误导
                "stop_source": f"规则引擎 initial_sl ({_sl_sign}{abs(STOP_PCT):g}%)",
                "sim_no_broker_stop": True,
                # 本链路按构造仅 SIM (trd_env 守卫在 _execute 内), 口径标 sim 与落库一致
                "trade_mode": "sim",
                "planned_entry": plan_px,
                "entry_dev_pct": ((float(fpx) - float(plan_px)) / float(plan_px) * 100)
                                 if plan_px else None,
                "extra_lines": extra_lines,
            },
        )))
    except Exception as exc:  # 通知永不阻断建仓
        print(f"[ext_entry] 成交推送失败(不影响建仓): {exc}")
        return False


_PUSH_DEDUP = ROOT / "data" / "ext_entry_push_dedup.json"


def _dedup_reserve(key):
    """推送去重闸: 同一 key **每个 ET 日只放行一次** (True = 可发, 并**即刻占位**)。

    探针模式 (见 `_probe_mode`: `FUTSEEK_PROBE=1` 或进程内在跑 pytest) 下**连键都不写** ——
    否则一次探针/演练会把这个 key 的当日名额用掉, 反而把生产的真告警静默掉。

    2026-09-19: 由 `_push_once` 内联抽出 (原逻辑逐字保留), 供**成交富卡** (`_push_filled`) 复用 ——
    此前只有 `_push_once` 走去重, `_push_filled` 每次调用都真发一条。
    """
    if _probe_mode():
        return False
    today = _today_et()
    lock_path = _PUSH_DEDUP.with_suffix(".lock")
    try:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                data = json.loads(_PUSH_DEDUP.read_text()) if _PUSH_DEDUP.exists() else {}
            except Exception:
                data = {}
            if data.get(key) == today:
                return False
            data[key] = today
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{_PUSH_DEDUP.name}.", suffix=".tmp", dir=_PUSH_DEDUP.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    json.dump(data, tmp, ensure_ascii=False, sort_keys=True)
                os.replace(tmp_name, _PUSH_DEDUP)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    _log.warning("push dedup tmp unlink failed", exc_info=True)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception:
        return True
    return True


def _push_once(key, text):
    """同一 key **每个 ET 日只推一次** (2026-09-18)。

    病灶: _push 无去重, 而 US.TEST 这类**报废测试单**会被对账路径反复碰到 ⇒ 同一句拦截文案
    每轮都推 (用户反馈"一直持续推送虚拟标的")。本函数只做通知去重, 不改变拦截行为本身。
    """
    if not _dedup_reserve(key):
        if _probe_mode():
            print(f"[ext_entry][probe] 推送抑制(不写去重键): {text}")
        return False
    _push(text)
    return True


def _audit(rec):
    rec["ts"] = datetime.utcnow().isoformat() + "Z"
    with AUDIT.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _opened_today_total(opened_today, today):
    """当日(ET)已开仓计数 = **盘前 + 盘后合并** (key 形如 '2026-09-17:premarket').

    用户 9/17 令"配额提升到 5"针对的是**每日合计**, 不是单时段各 5 ⇒ 两个时段共享同一个上限。
    跨日残留 key 必须忽略 (state 里会留历史日期的 key, 用 startswith 过滤)。
    """
    return sum(value for key, value in opened_today.items() if key.startswith(today))


def prefilter_verdicts(change_pct, price, volume):
    """入口预筛影子用的**纯函数**判定 (无副作用, 便于测试)。

    返回每个候选在各档门槛下的假设判定 + 分档标签。**不参与交易逻辑**。
    """
    chg = float(change_pct or 0.0)
    px = float(price or 0.0)
    vol = float(volume or 0.0)
    price_band = ("penny_lt5" if px < 5 else "$5~20" if px < 20 else "$20~50"
                  if px < 50 else "$50~200" if px < 200 else "$200+")
    chg_band = ("<0" if chg < 0 else "0~2" if chg < 2 else "2~3" if chg < 3
                else "3~5" if chg < 5 else "5~10" if chg < 10 else "10+")
    return {
        "chg_band": chg_band,
        "price_band": price_band,
        "passes_current_gate": bool(_entry_chg_ok(chg) and vol >= MIN_VOLUME and px >= MIN_PRICE),
        "passes_chg_3": bool(chg >= 3.0),
        "passes_chg_5": bool(chg >= 5.0),
        "passes_price_5_20": bool(5.0 <= px < 20.0),
        # ── 2026-09-19 180 日分片样本 (n=89) 裁决后的候选判据 ──
        #  唯一两窗口同向为正的特征是**时段成交量**:
        #    vol ≥20M 档: P(峰值≥1.5%) 76% / 净赚 81% / 均值 +3.38% (n=34)
        #    vol ≥20M & chg ≥3%: 全样本均值 +2.74%, 前50% +3.63%, 后50% +1.41% (两窗口同向)
        #  ⚠️ 已被 180 日**证伪**的 60 日结论 (不得再当判据): "追涨 ≥5% 更好" (均值 -0.53%),
        #     "$5~20 更好" (均值 -0.49%), ">$20 更差" (>$50 档均值反而 +2.13%)。
        "passes_vol_20m": bool(vol >= 2.0e7),
        "passes_vol_20m_chg_3": bool(vol >= 2.0e7 and chg >= 3.0),
        "passes_negative_band_risk": bool(vol < 2.0e7 and chg < 2.0),  # 落在负贡献段的候选标记
    }


def _record_prefilter_shadow(session, movers):
    """一次运行落一行 (与 _record_price_gate_rejects 同风格; 异常绝不影响主流程)。"""
    if not PREFILTER_SHADOW_ON or not movers:
        return
    try:
        rows = []
        for item in movers:
            v = prefilter_verdicts(item.get("chg"), item.get("px"), item.get("vol"))
            v.update({"symbol": item.get("symbol"), "chg": round(float(item.get("chg") or 0), 2),
                      "price": item.get("px"), "volume": item.get("vol")})
            rows.append(v)
        PREFILTER_SHADOW.parent.mkdir(parents=True, exist_ok=True)
        with PREFILTER_SHADOW.open("a") as f:
            f.write(json.dumps({
                "recorded_at": datetime.utcnow().isoformat() + "Z",
                "session": session, "n": len(rows), "rows": rows,
                "thresholds": {"chg": ENTRY_CHANGE_PCT, "min_price": MIN_PRICE,
                               "min_volume": MIN_VOLUME},
            }, ensure_ascii=False) + "\n")
    except Exception as exc:      # 埋点禁静默: 宁可打一行日志, 也不要"影子悄悄不落盘"
        print(f"[ext_entry] prefilter_shadow 落盘失败: {type(exc).__name__}: {exc}")


def _record_price_gate_rejects(session, rejects):
    """价格闸拦下的候选打点 + 日志一行 (用户 2026-09-17 令: 量化"$5 闸挡掉多少噪声票").

    按**运行**聚合: 一行 = 一次运行, 字段 `n` + `rejects` 明细 (symbol/price/change_pct/volume)。
    这样既能按日求和(回答"挡了多少"), 又保留标的身份(回答"挡的是谁"), 且不会让 jsonl 因
    同一只票每 5 分钟重复出现而膨胀。异常不得影响主流程。
    """
    if not rejects:
        return
    try:
        PRICE_GATE_REJECTS.parent.mkdir(parents=True, exist_ok=True)
        with PRICE_GATE_REJECTS.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": session,
                "n": len(rejects),
                "rejects": rejects,
            }, ensure_ascii=False) + "\n")
    except Exception:
        _log.warning("price gate reject audit write failed", exc_info=True)
    head = ", ".join(f"{r['symbol']} ${r['price']:g}" for r in rejects[:8])
    more = f" (+{len(rejects) - 8})" if len(rejects) > 8 else ""
    print(f"[ext_entry] 价格闸(<${MIN_PRICE:g}) 拦下 {len(rejects)} 只: {head}{more}")


def _execution_price_shadow_threshold(session):
    if session == "afterhours":
        return EXEC_PRICE_SHADOW_MIN_AFTERHOURS
    return EXEC_PRICE_SHADOW_MIN_PREMARKET


def _record_execution_price_shadow(session, movers):
    """Record-only counterfactual for a higher EXT execution-pool price floor.

    This shadow never changes gate order, candidate supply, or order submission.
    It reuses the already-fetched rank/snapshot prices, so enabling it in cron
    adds zero Futu requests.
    """
    if not EXEC_PRICE_SHADOW_ON or not movers:
        return
    try:
        threshold = _execution_price_shadow_threshold(session)
        current_pool = []
        execution_pool = []
        prefiltered_out = []
        n_bad_rows = 0
        for item in movers:
            try:
                price = float(item.get("px") or 0)
                change_pct = round(float(item.get("chg") or 0), 2)
                volume = float(item.get("vol") or 0)
            except (TypeError, ValueError, OverflowError):
                n_bad_rows += 1   # 留痕 (except:pass 棘轮清剿 2026-09-30): 坏行计数披露, 控制流不变
                continue
            if not _price_in_range(price):
                continue
            detail = {
                "symbol": item.get("symbol"),
                "decision_price": price,
                "change_pct": change_pct,
                "volume": volume,
            }
            current_pool.append(detail)
            if price >= threshold:
                execution_pool.append(detail)
            else:
                prefiltered_out.append(detail)
        row = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "trade_date": _today_et(),
            "session": session,
            "mode": "record_only",
            "price_threshold": threshold,
            "price_unit": "USD",
            "volume_unit": "shares",
            "candidates_n": len(movers),
            "bad_rows_n": n_bad_rows,
            "current_price_pool_n": len(current_pool),
            "shadow_execution_pool_n": len(execution_pool),
            "prefiltered_out_n": len(prefiltered_out),
            "prefiltered_out": prefiltered_out,
            "shadow_execution_pool": execution_pool,
        }
        EXEC_PRICE_SHADOW.parent.mkdir(parents=True, exist_ok=True)
        with EXEC_PRICE_SHADOW.open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"[ext_entry] 执行池价格影子[{session}] 阈值=${threshold:g} "
            f"现行价池={len(current_pool)} 影子价池={len(execution_pool)}"
        )
    except Exception as exc:
        _log.warning("execution price shadow write failed", exc_info=True)
        print(f"[ext_entry] 执行池价格影子落盘失败: {type(exc).__name__}: {exc}")


def _price_in_range(price):
    """价格闸 (2026-09-17 用户裁方案A 加下限).

    下限 MIN_PRICE: 挡 $5 以下仙股/妖股 (实证 CTNT $0.073 / GIPR $0.435 曾透过前三闸)。
    上限 POSITION_USD: 单笔仓位 $5000, 超过则连 1 股都买不起。
    `price > 0` 在 MIN_PRICE 被 env 调成 0 时仍是必要的兜底。
    """
    return price > 0 and MIN_PRICE <= price <= POSITION_USD


def _snapshot_number(row, key):
    try:
        value = float(row.get(key, 0) or 0)
        return value if math.isfinite(value) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _liquidity_quote(qctx, symbol):
    from futu import RET_OK
    try:
        ret, snapshot = qctx.get_market_snapshot([symbol])
    except Exception:
        return None
    if ret != RET_OK or snapshot is None or not hasattr(snapshot, "iloc") or snapshot.empty:
        return None
    row = snapshot.iloc[0]
    bid = _snapshot_number(row, "bid_price")
    ask = _snapshot_number(row, "ask_price")
    bid_volume = _snapshot_number(row, "bid_vol")
    ask_volume = _snapshot_number(row, "ask_vol")
    market_cap = _snapshot_number(row, "total_market_val")
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    spread_pct = (ask - bid) / ((bid + ask) / 2.0) * 100.0
    return {
        "bid": bid,
        "ask": ask,
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "market_cap": market_cap,
        "spread_pct": round(spread_pct, 4),
    }


def _rounded_order_price(price):
    if price <= 0:
        return 0.0
    return round(price, 3 if price < 1.0 else 2)


def _pretrade_gate(
    decision_price, qty, quote, trade_date,
    max_spread_pct=None, min_ask_depth_ratio=None,
    side="LONG",
):
    if not quote:
        return None, "quote_missing"
    short = str(side or "").upper() in {"SHORT", "SELL_SHORT", "SELL"}
    bid = float(quote["bid"])
    ask = float(quote["ask"])
    executable_price = bid if short else ask
    executable_volume_key = "bid_volume" if short else "ask_volume"
    executable_volume = float(quote.get(executable_volume_key, 0) or 0)
    spread_pct = float(quote["spread_pct"])
    spread_limit = max_spread_pct if max_spread_pct is not None else MAX_SPREAD_PCT
    depth_ratio = (
        min_ask_depth_ratio if min_ask_depth_ratio is not None else MIN_ASK_DEPTH_RATIO
    )
    if spread_pct > spread_limit:
        return None, "spread_too_wide"
    if decision_price > 0:
        max_adverse_drift = decision_price * (1.0 + MAX_PRICE_DRIFT_PCT / 100.0)
        min_adverse_drift = decision_price * (1.0 - MAX_PRICE_DRIFT_PCT / 100.0)
        adverse_drift = (
            executable_price < min_adverse_drift
            if short
            else executable_price > max_adverse_drift
        )
        if adverse_drift:
            return None, "price_drift_too_high"
    if qty > 0 and executable_volume < qty * depth_ratio:
        depth_reason = "insufficient_bid_depth" if short else "insufficient_ask_depth"
        return None, depth_reason

    from app.execution.fee_model import estimate_round_trip
    fee = estimate_round_trip(qty, executable_price, executable_price, side, trade_date)
    notional = qty * executable_price
    fee_pct = fee["total"] / notional * 100.0 if notional > 0 else float("inf")
    if fee_pct > MAX_ROUND_TRIP_FEE_PCT:
        return None, "round_trip_fee_too_high"

    return {
        "order_price": _rounded_order_price(executable_price),
        "side": "SHORT" if short else "LONG",
        "bid": bid,
        "ask": ask,
        "bid_volume": float(quote.get("bid_volume", 0) or 0),
        "ask_volume": float(quote.get("ask_volume", 0) or 0),
        "executable_volume": executable_volume,
        "spread_pct": spread_pct,
        "round_trip_fee_est": fee["total"],
        "round_trip_fee_pct": round(fee_pct, 4),
    }, None


def _extended_vwap_metrics(qctx, symbol, session):
    try:
        from futu import KLType, RET_OK
        trade_date = _today_et()
        ret, bars, _ = qctx.request_history_kline(
            symbol,
            start=trade_date,
            end=trade_date,
            ktype=KLType.K_5M,
            autype=None,
            max_count=1000,
            extended_time=True,
        )
        if ret != RET_OK or bars is None or not hasattr(bars, "iterrows") or not len(bars):
            return None
        if VWAP_ANCHOR_MATURITY:
            session_start, session_end = VWAP_ANCHOR_WINDOWS[session]
        else:
            session_start, session_end = SESSION_WINDOWS[session]
        rows = []
        for _, row in bars.iterrows():
            time_key = str(row.get("time_key", ""))
            if len(time_key) < 11:
                continue
            try:
                hhmm = int(time_key[11:13]) * 100 + int(time_key[14:16])
                close = float(row.get("close", 0) or 0)
                volume = float(row.get("volume", 0) or 0)
            except (TypeError, ValueError):
                _log.warning("VWAP data parse: bad close/volume", exc_info=True)
                continue
            if session_start <= hhmm <= session_end and close > 0 and volume > 0:
                rows.append({"close": close, "volume": volume})
        if len(rows) < 4:
            return None
        notional_volume = sum(row["close"] * row["volume"] for row in rows)
        total_volume = sum(row["volume"] for row in rows)
        vwap = notional_volume / total_volume if total_volume > 0 else 0
        if vwap <= 0:
            return None
        last = rows[-1]
        prior = rows[-4:-1]
        prior_avg = sum(row["volume"] for row in prior) / len(prior)
        volume_ratio = last["volume"] / prior_avg if prior_avg > 0 else 0
        vwap_dev_pct = (last["close"] - vwap) / vwap * 100.0
        return {
            "vwap": round(vwap, 4),
            "last_close": round(last["close"], 4),
            "last_volume": last["volume"],
            "volume_ratio": round(volume_ratio, 4),
            "vwap_dev_pct": round(vwap_dev_pct, 4),
            "bars": len(rows),
            "anchor": "maturity" if VWAP_ANCHOR_MATURITY else "full_session",
        }
    except Exception as exc:
        print(f"[ext_entry] extended VWAP metrics failed: {type(exc).__name__}: {exc}")
        return None


def _entry_structure_gate(qctx, symbol, session, price):
    """返回 (metrics|None, reason|None, metrics|None)。

    被拒时第 3 元携带实测 metrics (数据缺失时为 None), 调用方须落台账 ——
    被拒候选的 vwap_dev/volume_ratio 影子分布由此积累 (P1-3: 先攒拒绝分布
    样本, 再回看 0.8% 距离 / 0.5 量比阈值是否误杀动量延续型)。
    """
    metrics = _extended_vwap_metrics(qctx, symbol, session)
    if not metrics:
        return None, "structure_data_missing", None
    vwap = float(metrics["vwap"])
    if price <= vwap:
        return None, "below_vwap", metrics
    vwap_dev_pct = (price - vwap) / vwap * 100.0
    if abs(vwap_dev_pct) > MAX_VWAP_DISTANCE_PCT:
        return None, "vwap_extended", metrics
    if float(metrics["volume_ratio"]) < MIN_VOLUME_CONFIRM_RATIO:
        return None, "volume_confirmation_missing", metrics
    return metrics, None, metrics


def _record_pretrade_gate_rejects(session, rejects):
    if not rejects:
        return
    try:
        PRETRADE_GATE_REJECTS.parent.mkdir(parents=True, exist_ok=True)
        with PRETRADE_GATE_REJECTS.open("a") as handle:
            handle.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": session,
                "n": len(rejects),
                "rejects": rejects,
            }, ensure_ascii=False) + "\n")
    except Exception:
        _log.warning("spread gate reject audit write failed", exc_info=True)


def _calendar_coverage(session, candidates):
    """当日日历覆盖 + 候选∩日历交集 (2026-09-22 零成交归因缺口补齐)。

    缺口实证: 台账 421 条 100% 落 `calendar_missing` —— 该桶把三件完全不同的事压成一个读数:
      (a) 今日确无该票事件 (财报季空窗);
      (b) 日历源当日覆盖过窄 (2026-09-22 实测: 主源 futu 当日 6 只 / 备源 AV 18 只);
      (c) 候选池与事件池不相交 (异动榜 50 只 ∩ 两侧日历 = ∅, 19 只 ≥$5 无一在册)。
    没有这组读数时"季空窗"与"口径互斥"在台账上不可区分 —— 当天必须手工跑三方交集探针
    (movers ∩ futu日历 ∩ AV日历) 才能定性, 而探针不落盘 ⇒ 下次归零又要重跑一遍。
    只读、无副作用、无网络增量 (`_earnings_calendar_rows` 进程内已缓存); 异常一律吞掉
    (观测面不得影响交易主流程), 失败时写 coverage_error 供归因。
    """
    try:
        rows, error = _earnings_calendar_rows()
        today = _today_et()
        day_rows = [r for r in rows if str(r.get("reportDate")) == today]
        by_source = {}
        same_phase = set()
        for row in day_rows:
            source = str(row.get("source") or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
            before, after = _parse_earnings_phase(row)
            if (session == "premarket" and before) or (session == "afterhours" and after):
                same_phase.add(str(row.get("symbol") or "").upper())
        all_symbols = {str(r.get("symbol") or "").upper() for r in day_rows}
        cand = {str(c or "").replace("US.", "").upper() for c in (candidates or [])}
        cand.discard("")
        return {
            # 日历规模: 该日有几行 / 各源几行 / 与本 session 同相位几行
            "calendar_rows_today": len(day_rows),
            "calendar_by_source": by_source,
            "calendar_same_phase": len(same_phase),
            # 交集: 候选池规模 / 落在日历里的候选数 / 落在"同相位日历"里的候选数
            #   (后两项恒 0 = 门对本 session 结构性不可能通过, 与"市场空窗"是两回事)
            "candidates": len(cand),
            "candidates_in_calendar": len(cand & all_symbols),
            "candidates_in_calendar_same_phase": len(cand & same_phase),
            "calendar_source_error": error,
        }
    except Exception as exc:
        return {"coverage_error": f"{type(exc).__name__}: {exc}"}


def _record_earnings_gate_rejects(session, trade_date, reason_counts, details, candidates=None):
    """earnings 闸拒绝台账 (P0-2, DEB-20260920 §10). 每次运行聚合一行。

    details 只收非 calendar_missing 的子原因 (calendar_missing 是常态: 多数异动票
    今日不发财报, 逐票记录会每 5 分钟膨胀一次; 计数已足够)。异常不得影响主流程。

    2026-09-22 补 `coverage` 块 (见 `_calendar_coverage`): `calendar_missing` 一个桶
    同时装了"季空窗/源覆盖窄/候选与事件池不相交"三种情形, 单纯 counts 无法归因。
    candidates 可选 (不传则交集读数记 0), 缺席不得影响原有字段。
    """
    if not reason_counts:
        return
    try:
        EARNINGS_GATE_REJECTS.parent.mkdir(parents=True, exist_ok=True)
        with EARNINGS_GATE_REJECTS.open("a") as handle:
            handle.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "session": session,
                "trade_date": trade_date,
                "counts": reason_counts,
                "details": details[:50],
                "coverage": _calendar_coverage(session, candidates),
            }, ensure_ascii=False) + "\n")
    except Exception:
        _log.warning("calendar gate audit write failed", exc_info=True)


def _record_earnings_near_misses(session, trade_date, candidates):
    """Record deterministic base-gate passes rejected by a missing calendar event."""
    if not candidates or not EARNINGS_NEAR_MISS_ON:
        return
    try:
        EARNINGS_NEAR_MISSES.parent.mkdir(parents=True, exist_ok=True)
        lock_path = EARNINGS_NEAR_MISSES.with_suffix(".lock")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            seen = set()
            if EARNINGS_NEAR_MISSES.exists():
                with EARNINGS_NEAR_MISSES.open() as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            _log.warning("earnings near miss jsonl parse", exc_info=True)
                            continue
                        key = (
                            row.get("trade_date"), row.get("session"),
                            row.get("symbol")
                        )
                        if (
                            all(key)
                            and row.get("base_gate")
                            == "price_momentum_volume_turnover_passed"
                        ):
                            seen.add(key)

            new_rows = []
            for candidate in candidates:
                symbol = str(candidate.get("symbol") or "")
                if not symbol:
                    continue
                price = float(candidate.get("price") or 0)
                change_pct = float(candidate.get("change_pct") or 0)
                volume = float(candidate.get("volume") or 0)
                turnover_usd = price * volume
                if (
                    not _price_in_range(price)
                    or not _entry_chg_ok(change_pct)
                    or volume < MIN_VOLUME
                    or turnover_usd < MIN_EXT_TURNOVER_USD
                ):
                    continue
                key = (trade_date, session, symbol)
                if key in seen:
                    continue
                seen.add(key)
                new_rows.append({
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "trade_date": trade_date,
                    "session": session,
                    "symbol": symbol,
                    "base_gate": "price_momentum_volume_turnover_passed",
                    "reject_reason": "calendar_missing",
                    "price": price,
                    "change_pct": round(change_pct, 2),
                    "volume": volume,
                    "turnover_usd": round(turnover_usd, 2),
                })
            if not new_rows:
                return
            with EARNINGS_NEAR_MISSES.open("a") as handle:
                for row in new_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception:
        _log.warning("earnings near miss file I/O failed", exc_info=True)


def _research_downstream_gates(item, session, today, qctx, counts, pretrade_rejects):
    """SIM 研究臂: earnings 闸之后的全部下游闸 (与严格臂内联管线**同构同阈值**)。

    2026-09-22 用户裁「SIM 研究臂」(DEB-20260920 §12)。本函数是严格臂内联管线的
    **有界复制** — 研究臂 2026-10-31 死线自动退役, 退役时整块删除即可, 严格臂内联
    代码全程零改动 (样本隔离纪律同样适用于代码面)。存活期内严格臂下游闸若有变更,
    本函数必须同步 (§12 台账已登记该维护义务)。
    计数键全部 research_* 前缀, 拒绝明细带 gate_mode 落 pretrade 台账 — 严格臂读数零污染。
    返回 (qty, order_price, position_usd, grade, structure, pretrade_gate) 或 None。
    """
    symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
    source = str(item.get("_source") or "")
    quote = _liquidity_quote(qctx, symbol)
    grade, grade_reason = _entry_grade(price, volume, quote.get("market_cap") if quote else 0)
    if grade is None:
        counts[f"research_{grade_reason}"] = counts.get(f"research_{grade_reason}", 0) + 1
        pretrade_rejects.append({
            "symbol": symbol, "decision_price": price,
            "reason": grade_reason, "quote": quote, "gate_mode": RESEARCH_GATE_MODE,
            "source": source,
        })
        return None
    window_reason = _entry_window_reason(session, grade)
    if window_reason:
        counts["research_entry_window"] = counts.get("research_entry_window", 0) + 1
        pretrade_rejects.append({
            "symbol": symbol, "decision_price": price, "reason": window_reason,
            "grade": grade, "gate_mode": RESEARCH_GATE_MODE, "source": source,
        })
        return None
    position_usd = POSITION_USD if grade == "A" else POSITION_B_USD
    qty = int(position_usd / price)
    spread_limit = MAX_SPREAD_PCT_A if grade == "A" else MAX_SPREAD_PCT
    depth_ratio = MIN_ASK_DEPTH_RATIO_A if grade == "A" else MIN_ASK_DEPTH_RATIO
    gate, reject_reason = _pretrade_gate(
        price, qty, quote, today,
        max_spread_pct=spread_limit,
        min_ask_depth_ratio=depth_ratio,
        side="LONG",
    )
    if gate is None:
        counts["research_liquidity"] = counts.get("research_liquidity", 0) + 1
        pretrade_rejects.append({
            "symbol": symbol, "decision_price": price, "qty": qty,
            "reason": reject_reason, "quote": quote, "gate_mode": RESEARCH_GATE_MODE,
            "source": source,
        })
        return None
    order_price = gate["order_price"]
    structure, structure_reason, structure_metrics = _entry_structure_gate(
        qctx, symbol, session, order_price
    )
    if structure is None:
        counts["research_structure"] = counts.get("research_structure", 0) + 1
        pretrade_rejects.append({
            "symbol": symbol, "decision_price": price, "qty": qty,
            "reason": structure_reason, "grade": grade,
            "structure_metrics": structure_metrics, "gate_mode": RESEARCH_GATE_MODE,
            "source": source,
        })
        return None
    qty = int(position_usd / order_price)
    if qty < 1:
        counts["research_position_too_small"] = (
            counts.get("research_position_too_small", 0) + 1
        )
        return None
    return qty, order_price, position_usd, grade, structure, gate


def _research_pass_dry(pool, session, today, qctx, counts, results, pretrade_rejects):
    """研究臂 dry 通道: 只列候选, 不写 state 不下单 (与严格臂 dry 对称, 无配额模拟)。"""
    ordered = 0
    try:
        held = _held_symbols()
    except Exception:
        held = set()
    for item in pool:
        symbol = item["symbol"]
        source = str(item.get("_source") or "?")
        if symbol in held:
            _record_research_funnel(session, today, item, "reject", "held")
            continue
        before = len(pretrade_rejects)
        passed = _research_downstream_gates(
            item, session, today, qctx, counts, pretrade_rejects
        )
        if passed is None:
            reason = (pretrade_rejects[-1].get("reason", "")
                      if len(pretrade_rejects) > before else "not_reached")
            _record_research_funnel(session, today, item, "reject", reason)
            continue
        qty, order_price, position_usd, grade, structure, gate = passed
        ordered += 1
        counts[f"research_src_{source}_ordered"] = (
            counts.get(f"research_src_{source}_ordered", 0) + 1
        )
        _record_research_funnel(session, today, item, "ordered")
        results.append({
            "ok": True, "dry": True, "symbol": symbol,
            "qty": qty, "price": order_price, "decision_price": item["px"],
            "session": session, "change_pct": round(item["chg"], 2),
            "volume": item["vol"], "earnings": None,
            "gate_mode": RESEARCH_GATE_MODE, "research_source": source,
            "position_usd": position_usd, "grade": grade,
            "structure": structure, "pretrade": gate,
        })
    counts["research_ordered"] = ordered


def _research_pass_real(
    pool, session, today, qctx, state, attempts, opened_today, day_key,
    total_today, session_today, premarket_limit, results, pretrade_rejects, counts,
):
    """研究臂真单通道: 严格臂循环收口后跑, 只吃剩余配额 (纪律①)。

    配额与在仓帽**每单前复查** (在仓帽严于严格臂的每轮一次 — 见 §12/配置块注释)。
    attempt 键与严格臂同机制 (每标的每日一 attempt, failed 不占配额), 记录带 gate_mode。
    返回 (total_today, session_today)。
    """
    ordered = 0
    try:
        held = _held_symbols()
    except Exception:
        held = set()
    for item in pool:
        if total_today >= MAX_PER_DAY or (
            session == "premarket" and session_today >= premarket_limit
        ):
            break
        symbol = item["symbol"]
        source = str(item.get("_source") or "?")
        if symbol in held:
            _record_research_funnel(session, today, item, "reject", "held")
            continue
        attempt_key = f"{today}:{symbol}"
        if attempt_key in attempts:
            _record_research_funnel(session, today, item, "reject", "attempt_reserved")
            continue
        exposure_gate = _open_exposure_gate()
        if exposure_gate:
            exposure_gate = dict(exposure_gate)
            exposure_gate["gate_mode"] = RESEARCH_GATE_MODE
            counts["research_exposure_blocked"] = (
                counts.get("research_exposure_blocked", 0) + 1
            )
            _record_research_funnel(session, today, item, "reject", "exposure_blocked")
            results.append(exposure_gate)
            break
        before = len(pretrade_rejects)
        passed = _research_downstream_gates(
            item, session, today, qctx, counts, pretrade_rejects
        )
        if passed is None:
            reason = (pretrade_rejects[-1].get("reason", "")
                      if len(pretrade_rejects) > before else "not_reached")
            _record_research_funnel(session, today, item, "reject", reason)
            continue
        qty, order_price, position_usd, grade, structure, gate = passed
        ordered += 1   # 与严格臂 counts["ordered"] 同口径: 发起下单即计数
        counts[f"research_src_{source}_ordered"] = (
            counts.get(f"research_src_{source}_ordered", 0) + 1
        )
        _record_research_funnel(session, today, item, "ordered")
        attempts[attempt_key] = {
            "status": "reserved", "session": session, "symbol": symbol,
            "trade_date": today, "gate_mode": RESEARCH_GATE_MODE,
            "research_source": source,
            "reserved_at": datetime.utcnow().isoformat() + "Z",
        }
        _save_state(state)
        # earnings 位: 研究臂无财报事件 — 存根显式标注 gate_mode, surprise 字段为 None
        #   (下游 metadata_json.earnings_* 落 null, 分析面按 gate_mode 过滤, 不混算)。
        earnings_stub = {
            "gate_mode": RESEARCH_GATE_MODE, "research_source": source,
            "late_premarket": _is_late_premarket(session),
            "surprise_pct": None, "period": None,
            "period_age_days": None, "report_date": None, "report_phase": None,
        }
        try:
            result = _execute(
                symbol, qty, order_price, session, item["chg"], item["vol"],
                earnings_stub, False,
                position_usd=position_usd, grade=grade, structure=structure,
                gate_mode=RESEARCH_GATE_MODE,
            )
            result["decision_price"] = item["px"]
            result["pretrade"] = gate
        except Exception as exc:
            result = {
                "ok": False, "symbol": symbol, "session": session,
                "error": f"executor_exception:{exc}",
            }
            _push(f"❌ 延长时段开仓执行器异常 (研究臂)\n{symbol}\n{exc}")
            _audit(result)
        result["symbol"] = symbol
        result["session"] = session
        result["gate_mode"] = RESEARCH_GATE_MODE
        result["research_source"] = source
        result["late_premarket"] = _is_late_premarket(session)
        attempts[attempt_key] = {
            "status": "opened" if result.get("ok") else "failed",
            "session": session, "trade_date": today,
            "gate_mode": RESEARCH_GATE_MODE,
            "research_source": source,
            "late_premarket": result["late_premarket"],
            "order_id": result.get("buy_order_id"),
            "stop_order_id": result.get("stop_order_id"),
            "filled_qty": result.get("filled_qty"),
            "avg_fill_price": result.get("avg_fill_price"),
            "error": result.get("error") or result.get("skip"),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        results.append(result)
        if result.get("ok"):
            total_today += 1
            session_today += 1
            opened_today[day_key] = session_today
        _save_state(state)
    counts["research_ordered"] = counts.get("research_ordered", 0) + ordered
    return total_today, session_today


def _scan_movers(session, qctx):
    """全市场盘前/盘后榜 (富途官方接口, 2026-09-17 实测). 返回 [{symbol,px,chg,vol,prev_close}].
    替代旧"近5日活跃股"扫描 — 覆盖面从 15 只 → 全市场真实盘前/盘后异动榜.
    字段: security / pre_market_price / pre_market_change_ratio / pre_market_volume / close_price."""
    from futu import RET_OK
    try:
        if session == "premarket":
            ret, rd = qctx.get_us_pre_market_rank(count=SCAN_TOP_N)
            pk, ck, vk = "pre_market_price", "pre_market_change_ratio", "pre_market_volume"
        else:
            ret, rd = qctx.get_us_after_hours_rank(count=SCAN_TOP_N)
            pk, ck, vk = "after_hours_price", "after_hours_change_ratio", "after_hours_volume"
        if ret != RET_OK or not isinstance(rd, tuple) or len(rd) < 2:
            return []
        df = rd[1]
        if df is None or not hasattr(df, "columns") or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            sec = str(r.get("security", ""))
            if not sec.startswith("US."):
                continue
            try:
                out.append({
                    "symbol": sec,
                    "name": str(r.get("name", "")),
                    "px": float(r.get(pk, 0) or 0),
                    "chg": float(r.get(ck, 0) or 0),
                    "vol": float(r.get(vk, 0) or 0),
                    "prev_close": float(r.get("close_price", 0) or 0),
                })
            except Exception:
                _log.warning("earnings beat row parse failed", exc_info=True)
                continue
        return out
    except Exception:
        return []


def _beat_rank_symbols(qctx):
    """事件源: 财报**超预期**榜 → 候选代码 (2026-09-17 实弹探针).

    实测: `get_earnings_beat_rank('US', BeatType.EPS)` total=1455 条, 字段含
    `released_date` / `market_cap` / `beat_ratio` / `earning_day_chg` / `detail_post_period`(BEFORE|AFTER)。
    取到的 200 条里 200/200 价格≥$5 且市值≥$10B ⇒ 该榜天然是大中盘 (与"异动榜=仙股盘"互补)。
    两道必须的服务端外过滤: ①`released_date` 近 BEAT_RECENT_DAYS 日 —— 不加就是跟过期事件交易
    (实测默认前几条是 1-2 个月前披露的 GOOGL/AAPL/NVDA); ②市值下限 —— 挡微盘噪声。
    """
    from futu import RET_OK, BeatType
    try:
        ret, rd = qctx.get_earnings_beat_rank("US", BeatType.EPS, count=BEAT_TOP_N)
        if ret != RET_OK or not isinstance(rd, tuple) or len(rd) < 2:
            return []
        df = rd[1]
        if df is None or not hasattr(df, "columns") or df.empty:
            return []
        cutoff = (_now_et().date() - timedelta(days=BEAT_RECENT_DAYS)).strftime("%Y-%m-%d")
        out = []
        for _, r in df.iterrows():
            sec = str(r.get("security", ""))
            if not sec.startswith("US."):
                continue
            if str(r.get("released_date", "")) < cutoff:
                continue
            try:
                if float(r.get("market_cap", 0) or 0) < BEAT_MIN_MCAP:
                    continue
            except Exception:
                _log.warning("earnings market_cap parse failed", exc_info=True)
                continue
            out.append(sec)
        return out
    except Exception:
        return []


def _watchlist_symbols(qctx):
    """自选股源: 用户在富途 App 里的自选分组 (用户 curation, 非发现机制).

    实测: `get_user_security('美股')` ret=0, 51 只, 抽样 50/50 全部 ≥$5 (AMD/NVDA/AAPL/TSLA/MRNA/LITX…)
    ⇒ 零仙股噪声。注意选择性偏差: 它是你的注意力集合, 适合做"信任池"。
    """
    try:
        ret, df = qctx.get_user_security(WATCHLIST_GROUP)
        if ret != 0 or df is None or not hasattr(df, "columns") or df.empty:
            return []
        col = "code" if "code" in df.columns else df.columns[0]
        return [str(s) for s in df[col].tolist() if str(s).startswith("US.")]
    except Exception:
        return []


def _snapshot_candidates(session, qctx, symbols):
    """把任意代码列表 → 候选结构 {symbol,px,chg,vol,prev_close} (盘前取 pre_* 字段).

    与 `_scan_movers` 的口径一致: px = 盘前/盘后价, chg = vs 昨收 %, vol = 盘前/盘后量。
    """
    from futu import RET_OK
    out, syms = [], [s for s in dict.fromkeys(symbols) if s.startswith("US.")]
    for i in range(0, len(syms), SNAPSHOT_BATCH):
        batch = syms[i:i + SNAPSHOT_BATCH]
        try:
            ret, snap = qctx.get_market_snapshot(batch)
        except Exception:
            _log.warning("get_market_snapshot batch failed", exc_info=True)
            continue
        if ret != RET_OK or snap is None or not hasattr(snap, "columns"):
            continue
        for _, r in snap.iterrows():
            sec = str(r.get("code", ""))
            if not sec.startswith("US."):
                continue
            try:
                prev = float(r.get("prev_close_price", 0) or 0)
                if session == "premarket":
                    px = float(r.get("pre_price", 0) or 0) or float(r.get("last_price", 0) or 0)
                    vol = float(r.get("pre_volume", 0) or 0)
                else:
                    px = float(r.get("after_price", 0) or 0) or float(r.get("last_price", 0) or 0)
                    vol = float(r.get("after_volume", 0) or 0)
                chg = ((px / prev - 1) * 100) if (px > 0 and prev > 0) else 0.0
                out.append({"symbol": sec, "name": str(r.get("name", "")), "px": px,
                            "chg": chg, "vol": vol, "prev_close": prev})
            except Exception:
                _log.warning("snapshot row parse failed for %s", sec, exc_info=True)
                continue
    return out


def _calendar_symbols(session):
    """事件源: **当日同相位财报日历名单** → 候选代码 (2026-09-23 供给归因).

    相位判据与 `_earnings_calendar_event` **逐字同式** (两处共用 `_session_event_pairs` 配对表,
    由 `tests/test_ext_entry_calendar_source.py` 的源码契约钉住) ⇒ 本源入选者必然通过 earnings 门
    第①道, 只在 ②beat 证明 / ③actual 可得性 上才可能被拒 —— 即"让合格票有机会进闸",
    而不是让不合格票进闸。RFC-102 开配对后, 本源同时供给**昨日 AMC** 票 (盘前)。

    只读、无网络增量 (`_earnings_calendar_rows` 进程内已缓存, 与判定同源); 源故障时返回 []
    并打显式警告 (fail-open 跳过本源, 不影响异动榜等其他源)。
    """
    rows, error = _earnings_calendar_rows()
    if error:
        print(f"[ext_entry] 日历候选源不可用 (跳过本源, 其他源不受影响): {error}")
        return []
    today = _today_et()
    allowed = {}
    for offset, phase in _session_event_pairs(session):
        allowed.setdefault(_shift_et_day(today, offset), set()).add(phase)
    out = []
    for row in rows:
        phases = allowed.get(str(row.get("reportDate", "")))
        if not phases:
            continue
        before, after = _parse_earnings_phase(row)
        if before == ("before" in phases) and after == ("after" in phases):
            ticker = str(row.get("symbol", "")).upper()
            if ticker:
                out.append(f"US.{ticker}")
    return out


def _finnhub_calendar_symbols(session):
    """覆盖补强源: 当日 Finnhub 日历中**同相位**的票 → 候选代码 (2026-09-23 供给归因)。

    为什么需要: 生产主源 (futu 原生日历) 覆盖只有 Finnhub 的 39%, 且 `pub_type=REGULAR`
    (测得 7 日窗 4/34 = 11.8%) 被映射成空相位 ⇒ 这些票连候选池都进不去, 也就无法被观测。
    **零资格变更**: 让票进闸 ≠ 放行 —— 判据① 仍只认 futu/AV 日历的相位, so 本源不会产生新入场;
    其价值是让 `calendar_phase_unknown`/`calendar_missing` 的票进入 reject 台账 (实测证据源)。

    只读、零新增网络 (复用 `_finnhub_today_calendar` 的进程内缓存, 与判据②同源)。
    """
    today = _today_et()
    out = []
    pairs = _session_event_pairs(session)
    if not pairs:
        return []
    for offset, phase in pairs:
        day = _shift_et_day(today, offset)
        want = "bmo" if phase == "before" else "amc"   # 相位→hour 映射由**配对相位**决定
        cal, error = _finnhub_today_calendar(day)
        if not cal:
            if error:
                print(f"[ext_entry] Finnhub 日历候选源不可用 (跳过本源, 其他源不受影响): {error}")
            continue
        for tick, row in cal.items():
            if str(row.get("hour") or "").strip().lower() != want:
                continue
            if str(row.get("date") or day) != day:   # 双保险 (端点已按该日查, 契约可能漂)
                continue
            if tick:
                out.append(f"US.{tick}")
    return out


def _scan_candidates(session, qctx):
    """多源候选合并 + 去重 (2026-09-17). 源顺序 = 优先级 (先到先得, 保留先出现的结构).

    这是"换源"的唯一改动点: 下游四闸 / 价格下限 / 配额 / 下单 / 平仓 / 台账全部复用, 一行不改。
    """
    merged, tag_of = [], {}
    skipped_test = []
    skipped_etf = []

    def _add(items, tag):
        for it in items:
            sym = it.get("symbol")
            if not sym or sym in tag_of:
                continue
            # 2026-09-18 (DEB-20260918 §23.5, 源头修): 占位标的 (TEST / US.TEST / *.TEST)
            #   在**候选源汇合口**就挡掉 —— 它们不是真候选股, 只可能来自单测 artifact 或券商
            #   账户里的报废测试单 (如 US.TEST order 8185070)。此前只在 `_execute` 深处挡,
            #   占位标的仍会走完四闸、占 attempt 键、进审计/推送路径。
            if is_test_symbol(sym):
                skipped_test.append(sym)
                continue
            # 2026-09-22 (零成交归因): ETF 在"必须有当日同相位财报事件"的 earnings 门下是
            #   **结构性死票** —— ETF 永远没有财报事件, 必然走完四闸后被 calendar_missing 拒。
            #   实测当日异动榜 50 只里 6 只是 ETF (IMVP/GRAG/WRTH/ARGT/KSA/RKX), 每轮白白
            #   占用 12% 候选位并把 dead-count 灌进 earnings 台账 (污染 coverage 归因)。
            #   判据 = 榜单自带 name 字段含 "ETF" (futu 美股 ETF 命名惯例; 实测 6/6 命中,
            #   同榜个股 0 误伤)。与 is_test_symbol 同级, 放在汇合口。
            if "ETF" in str(it.get("name") or "").upper():
                skipped_etf.append(sym)
                continue
            tag_of[sym] = tag
            it["_source"] = tag
            merged.append(it)

    if SOURCE_RANK_BOARD:
        _add(_scan_movers(session, qctx), "rank")
    extra = []
    if SOURCE_BEAT_RANK:
        extra += _beat_rank_symbols(qctx)
    if SOURCE_WATCHLIST:
        extra += _watchlist_symbols(qctx)
    cal_syms = _calendar_symbols(session) if SOURCE_CALENDAR else []
    if cal_syms:
        extra += cal_syms
        print(f"[ext_entry] 日历候选源 (当日同相位财报): {len(set(cal_syms))} 只 "
              f"{', '.join(sorted(set(cal_syms))[:8])}")
    new_syms = [s for s in dict.fromkeys(extra) if s not in tag_of]
    if SOURCE_FINNHUB_CALENDAR:
        fh_syms = _finnhub_calendar_symbols(session)
        if fh_syms:
            extra += fh_syms
            print(f"[ext_entry] Finnhub 日历候选源 (当日同相位, 覆盖补强): {len(set(fh_syms))} 只 "
                  f"{', '.join(sorted(set(fh_syms))[:8])}")
            new_syms = [s for s in dict.fromkeys(extra) if s not in tag_of]
    if new_syms:
        _add(_snapshot_candidates(session, qctx, new_syms), "snapshot")
    if skipped_test:
        print(f"[ext_entry] 候选源忽略占位标的: {', '.join(sorted(set(skipped_test)))}")
    if skipped_etf:
        print(f"[ext_entry] 候选源忽略 ETF: {len(set(skipped_etf))} 只 "
              f"(无财报事件 ⇒ 结构性死票, 不进四闸/台账)")
    if merged:
        n_extra = sum(1 for v in tag_of.values() if v != "rank")
        print(f"[ext_entry] 候选源合并: {len(merged)} 只 (异动榜 {len(merged) - n_extra} / "
              f"财报超预期榜+自选股+日历 {n_extra}) [开关 rank={SOURCE_RANK_BOARD} "
              f"beat={SOURCE_BEAT_RANK} watchlist={SOURCE_WATCHLIST} "
              f"calendar={SOURCE_CALENDAR} finnhub_calendar={SOURCE_FINNHUB_CALENDAR}]")
    return merged


def _held_symbols():
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        rows = conn.execute("SELECT symbol FROM open_positions WHERE size > 0").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as exc:
        raise RuntimeError(f"open_positions read failed, refusing entry: {exc}") from exc


def _extended_open_exposure():
    """Count/notional owned by the extended-hours entry strategy only."""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        rows = conn.execute(
            "SELECT symbol, size, avg_entry_price FROM open_positions "
            "WHERE size != 0 AND (owner_strategy='extended_hours_entry' "
            "OR strategy_id='extended_hours_entry' OR position_id LIKE 'EXT-%') "
            "ORDER BY symbol"
        ).fetchall()
        conn.close()
    except Exception as exc:
        raise RuntimeError(f"extended open_positions read failed, refusing entry: {exc}") from exc

    exposure = 0.0
    for symbol, size, avg_entry_price in rows:
        size_value = float(size or 0)
        price_value = float(avg_entry_price or 0)
        if size_value == 0 or price_value <= 0:
            return rows, None
        exposure += abs(size_value * price_value)
    return rows, exposure


def _open_exposure_gate():
    rows, exposure = _extended_open_exposure()
    if len(rows) >= MAX_OPEN:
        return {
            "skip": "ext_open_position_limit",
            "open_positions": len(rows),
            "limit": MAX_OPEN,
        }
    if exposure is None:
        return {"skip": "ext_open_notional_unavailable", "open_positions": len(rows)}
    if exposure + POSITION_USD > MAX_OPEN_NOTIONAL_USD:
        return {
            "skip": "ext_open_notional_limit",
            "open_notional_usd": round(exposure, 2),
            "next_position_usd": POSITION_USD,
            "limit_usd": MAX_OPEN_NOTIONAL_USD,
        }
    return None


def _open_trade_context():
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm

    return OpenSecTradeContext(
        host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPD_PORT", "11111")),
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.NONE,
    )


def _order_et_date(create_time):
    """broker 订单 create_time → ET 日期串.

    2026-09-17 实测钉死时区: 订单 8184987 的 broker create_time = '2026-09-16 23:14:45',
    对应审计 jsonl 的 UTC '2026-09-17T03:16:16Z' → 两者相差 4h15m ≈ ET(-4) 语义, 故 create_time 是 **ET**。
    解析失败返回 "" ⇒ 视为非当日 (绝不写 attempt, 宁可漏记不可误封锁)。
    """
    try:
        return datetime.strptime(str(create_time).strip()[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _order_session(row, fallback_session):
    create_time = row.get("create_time", "")
    try:
        hour = int(create_time[11:13])
        if 4 <= hour < 12:
            return "premarket"
        if 16 <= hour < 20:
            return "afterhours"
    except (ValueError, IndexError):
        _log.warning("_classify_session: bad create_time %r", create_time, exc_info=True)
    return fallback_session


def _reconcile_broker_entry_orders(
    tctx, env, acc_id, fallback_session, state=None, today=None
):
    """Recover terminal ext-entry fills and expose active ext-entry orders.

    This is read-only for broker state. It never cancels an order. Filled orders
    without a local owner row are written once; conflicts are returned for triage.
    """
    rows = []
    skipped_test = []
    for row in query_order_rows(tctx, env, acc_id):
        if row.get("remark") != "ext_entry":
            continue
        # 2026-09-18 (§23.5): 占位标的订单在**对账口**就丢弃 —— 否则它们会被写成当日 attempt
        #   (静默封锁该标的)、计入 active_orders/conflicts、或在成交路径触发建仓回补。
        #   实测 US.TEST order 8185070 的报废单每天都出现在这里的全量订单表。
        if is_test_symbol(row.get("symbol")):
            skipped_test.append(f"{row.get('symbol')}:{row.get('order_id')}")
            continue
        rows.append(row)
    if skipped_test:
        print(f"[ext_entry] 对账忽略占位标的订单: {', '.join(skipped_test)}")
    active_orders = []
    conflicts = []
    for row in rows:
        # 2026-09-17 修复 (Hermes 复查实证): 跨日旧单不得写 {today}:{symbol} attempt.
        #   病灶: 本函数此前对**所有** remark=ext_entry 的历史单无条件写当日 attempt,
        #   而采集循环 431 行 `if attempt_key in attempts: continue` ⇒ 只要该标的历史上被下过
        #   ext_entry 单, 今天就被**静默跳过**(实测 US.NOK 因 ET 9/16 23:14 的旧测试单在 9/17 被封锁,
        #   且因每日重复回填而永久封锁 = 供给静默饿死). 修复: 只看订单自身 ET 日期是否为当日.
        #   活动单仍**无条件**计入 active_orders — 活单本来就该拦住新开仓, 与日期无关.
        # today 为空 = 调用方未要求按日过滤 (如 _execute 下单前的安全闸, 不传 state/today)
        #   ⇒ 保持旧行为(全量处理), 否则会把当天单也误判"跨日"而跳过。
        row_is_today = (not today) or (_order_et_date(row.get("create_time")) == today)
        attempt = None
        if row_is_today and isinstance(state, dict) and today:
            attempts = state.setdefault("attempts", {})
            attempt_key = f"{today}:{row['symbol']}"
            attempt = attempts.setdefault(attempt_key, {})
            attempt.update({
                "session": _order_session(row, fallback_session),
                "symbol": row["symbol"],
                "trade_date": today,
                "order_id": row["order_id"],
                "broker_status": row["status"],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            })
        if row["status"] in ACTIVE_ORDER_STATUSES:
            if attempt is not None:
                attempt["status"] = "broker_active"
            active_orders.append(
                {
                    "symbol": row["symbol"],
                    "order_id": row["order_id"],
                    "status": row["status"],
                    "qty": row["qty"],
                    "filled_qty": row["filled_qty"],
                }
            )
            continue
        if not row_is_today:
            # 跨日旧单: 不写 attempt(防静默封锁), 也不按今日回填建仓(entry_time 会是错的).
            #   本链路未成交单 90s 内即撤, 正常不会有跨日成交; 真出现则只留痕, 由人工处置.
            print(f"[ext_entry] 对账跳过跨日旧单: {row['symbol']} {row['order_id']} "
                  f"status={row['status']} create_time={row.get('create_time')}")
            continue
        if row["filled_qty"] <= 0 or row["avg_fill_price"] <= 0:
            if attempt is not None:
                attempt["status"] = "failed"
                attempt["error"] = f"broker_{row['status']}"
            continue
        conn = sqlite3.connect(DB, timeout=30)
        try:
            existing = conn.execute(
                "SELECT id FROM trade_records WHERE futu_order_id=? "
                "AND owner_strategy='extended_hours_entry' LIMIT 1",
                (row["order_id"],),
            ).fetchone()
        finally:
            conn.close()
        if existing:
            if attempt is not None:
                attempt["status"] = "opened"
                attempt["filled_qty"] = row["filled_qty"]
                attempt["avg_fill_price"] = row["avg_fill_price"]
            continue
        try:
            position_id = _write_open_position(
                symbol=row["symbol"],
                qty=row["filled_qty"],
                entry_px=row["avg_fill_price"],
                decision_px=row["price"],
                session=_order_session(row, fallback_session),
                earn={},
                chg=0.0,
                vol=0.0,
                buy_order_id=row["order_id"],
            )
            if position_id and isinstance(state, dict) and today:
                reconciled_session = _order_session(row, fallback_session)
                state.setdefault("opened_today", {})
                reconciled_key = f"{today}:{reconciled_session}"
                state["opened_today"][reconciled_key] = (
                    state["opened_today"].get(reconciled_key, 0) + 1
                )
        except Exception as exc:
            if attempt is not None:
                attempt["status"] = "reconcile_conflict"
                attempt["error"] = str(exc)
            conflicts.append(
                {
                    "symbol": row["symbol"],
                    "order_id": row["order_id"],
                    "error": f"reconcile_write_failed:{exc}",
                }
            )
    return active_orders, conflicts


def _earnings_calendar_rows():
    """当日财报日历行 (进程内缓存; cron 每 tick 新进程 ⇒ 源故障只影响本 tick, 下轮自动重试).

    返回 (rows, error)。源异常时**不再静默等同于"今日无财报"** (旧病灶: except → [],
    "源挂了"与"真没事件"在读数上不可区分) — 返回 error 供子原因归因, 并打显式警告。
    """
    today = _today_et()
    cached = _EARNINGS_CALENDAR_CACHE.get(today)
    if cached is None:
        try:
            from app.data.earnings_calendar import EarningsCalendar
            rows, source_error = EarningsCalendar().get_earnings_calendar_with_status()
            cached = {"rows": rows or [], "error": source_error}
        except Exception as exc:
            cached = {"rows": [], "error": f"{type(exc).__name__}: {exc}"}
            print(
                "[ext_entry] earnings calendar SOURCE FAILED "
                f"(fail-closed this tick, will retry next run): {cached['error']}"
            )
        _EARNINGS_CALENDAR_CACHE[today] = cached
    return cached.get("rows") or [], cached.get("error")


def _parse_earnings_phase(row):
    """日历相位 → (before, after)。空/未知相位两者皆 False (fail-closed, 子原因单列)。"""
    phase = str(row.get("timeOfTheDay", "") or row.get("pub_type", "")).lower()
    before = "before" in phase or "pre" in phase or "bmo" in phase
    after = "after" in phase or "post" in phase or "amc" in phase
    return before, after


def _earnings_calendar_event(symbol, session):
    """同日同相位财报事件。返回 (row|None, sub_reason|None)。

    子原因 (P0-2, 落 earnings 拒绝台账):
      calendar_source_error   — 日历源本 tick 拉取失败 (fail-closed)
      calendar_missing        — 今日日历无此票 (常态)
      calendar_phase_mismatch — 今日有财报但相位与本 session 不符
      calendar_phase_unknown  — 今日有财报但相位字段为空/不可识别 (数据源契约漂移的信号)
    配对表 (RFC-102): 由 `_session_event_pairs(session)` 唯一决定 —— 默认「当日同相位」
    (盘前↔当日 BMO / 盘后↔当日 AMC); 开 `EXT_ENTRY_PREMARKET_AMC_PAIRING` 后, 盘前同时接受
    **昨日 AMC** (其 actual 次日早晨已入库, 与盘前窗的数据可得性对齐)。
    与旧版差异: 扫完所有 symbol+日期匹配行再定论 (旧版首行相位不符即拒, 不看后续行)。
    """
    rows, error = _earnings_calendar_rows()
    if error:
        return None, "calendar_source_error"
    today = _today_et()
    ticker = symbol.replace("US.", "").upper()
    allowed = {}
    for offset, phase in _session_event_pairs(session):
        allowed.setdefault(_shift_et_day(today, offset), set()).add(phase)
    saw_mismatch = False
    saw_unknown = False
    for row in rows:
        if str(row.get("symbol", "")).upper() != ticker:
            continue
        phases = allowed.get(str(row.get("reportDate", "")))
        if not phases:
            continue
        before, after = _parse_earnings_phase(row)
        if not before and not after:
            saw_unknown = True
            continue
        if before == ("before" in phases) and after == ("after" in phases):
            _EVENT_SRC_OBS["futu"].add(ticker)
            return row, None
        saw_mismatch = True
    if saw_mismatch:
        return None, "calendar_phase_mismatch"
    result = (None, "calendar_phase_unknown" if saw_unknown else "calendar_missing")
    if EARNINGS_EVENT_SOURCE == "calendar_finnhub_union":
        return _finnhub_event_fallback(ticker, session, today, result[1])
    return result


def _finnhub_event_fallback(ticker, session, today, futu_reason):
    """判据① 回落源 (union 模式): futu 无行/相位未标定时, 用 Finnhub 当日日历行定存在性与相位。

    返回与 futu 行**同形**的合成行 (带 `_source="finnhub"`), 供判据②取 reportDate/相位。
    相位非同窗 ⇒ mismatch; 相位缺失 (dmh/None) ⇒ unknown; 票不在 Finnhub 当日日历 ⇒ 保留 futu 原判
    (calendar_missing/unknown) —— 这样台账口径不变, 只是"少拦一部分"。
    """
    unknown = False
    mismatch = False
    for offset, want in _session_event_pairs(session):
        day = _shift_et_day(today, offset)
        cal, err = _finnhub_today_calendar(day)
        if cal is None:
            return None, "calendar_source_error"
        row = cal.get(ticker)
        if row is None:
            continue
        hour = str(row.get("hour") or "").strip().lower()
        if hour not in ("bmo", "amc"):
            unknown = True
            continue
        if hour != ("bmo" if want == "before" else "amc"):
            mismatch = True
            continue
        _EVENT_SRC_OBS["finnhub"].add(ticker)
        return ({"symbol": ticker, "reportDate": day, "pub_type": hour.upper(), "_source": "finnhub",
                 "timeOfTheDay": "pre-market" if hour == "bmo" else "post-market"}, None)
    if unknown:
        return None, "calendar_phase_unknown"
    if mismatch:
        return None, "calendar_phase_mismatch"
    return None, futu_reason


def _afterhours_earnings_expected():
    """当日日历是否存在 after/post 相位财报 (P1-4 盘前保留额度的条件化依据)。

    源故障/空日历 → 视为"有" (保守: 宁可放弃盘前第 3 笔的机会成本, 不改变暴露节奏)。
    """
    rows, error = _earnings_calendar_rows()
    if error or not rows:
        return True
    today = _today_et()
    for row in rows:
        if str(row.get("reportDate", "")) != today:
            continue
        _before, after = _parse_earnings_phase(row)
        if after:
            return True
    return False


def _surprise_period_age_days(period):
    """Finnhub period (财季末日期) 距今 ET 天数; 缺失/不可解析返回 None (调用侧 fail-closed)."""
    try:
        period_date = datetime.strptime(str(period)[:10], "%Y-%m-%d").date()
        today_date = datetime.strptime(_today_et(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (today_date - period_date).days


def _classify_legacy_stale_state(legacy_reason, legacy_result):
    """旧判据 surprise_stale 四态分类 (DEB-20260923 §13.3.2 / §14.2, DEB-20260924 裁决 A)。

    为什么需要: 旧判据的 `surprise_stale` 混装三态 (period 缺失/不可解析、age>150、age<-7)
    共用同一 sub_reason 与同一句打印。10 月财报季对照期间若 `surprise_stale` 上升, 无法区分
    "Finnhub 端点不稳定" 与 "口径在误拦" ⇒ 观测失真。本函数把四态拆成显式标签, 写入
    对照台账的 `legacy_stale_state` 字段; 主闸原因仍为 `surprise_stale` (零行为变更)。

    四态:
      endpoint_empty       — company_earnings 空返回 (端点不稳定/回填中, §14.2 KBH 实测)
      period_unparseable   — period 字段缺失或不可解析为日期 (源数据质量问题)
      stale_gt_150         — 龄 > 150 天 (真陈旧, 守卫原始目标: 上季 beat 透旧闸)
      future_lt_-7         — 龄 < -7 天 (未来财季, **口径误拦**的主体)
      other                — 非 stale 类原因 (直接返回 None, 不分类)
    """
    if legacy_reason != "surprise_stale":
        return None
    period = (legacy_result or {}).get("period")
    age_days = (legacy_result or {}).get("period_age_days")
    # 端点空返回: company_earnings 返回空列表 ⇒ period/age 都 None, 且 _LEGACY_PERIOD_OBS
    #   不会被写入 (在 company_earnings 空分支已返回 finnhub_no_data); 但 _LEGACY_PERIOD_OBS
    #   里的 period=None 也可能是 period 字段本身缺失 ⇒ 用 age_days 与 period 的组合分辨。
    if period is None and age_days is None:
        return "endpoint_empty"
    if age_days is None:
        return "period_unparseable"
    if age_days > EARNINGS_MAX_PERIOD_AGE_DAYS:
        return "stale_gt_150"
    if age_days < -7:
        return "future_lt_-7"
    return "other"


def _earnings_surprise_legacy_finnhub(ticker):
    """旧判据②的 Finnhub 部分: company_earnings[0] surprise>0 + period 龄窗 [-7, 150]。

    2026-09-23 起只作回滚路径 (EXT_EARNINGS_CRITERION=legacy_period) 与对照台账的 legacy 读数。
    旧口径的自伤 (period = Finnhub 归一化**日历**季度末被当披露锚 ⇒ off-calendar 财年票
    被判 age_days < -7) 见 DEB-20260923 §2.1/§14.3。
    """
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        c = _get_finnhub_client()
        if c is None:
            return None, "finnhub_client_unavailable"
        e = c.company_earnings(ticker, limit=1)
        if not (e and isinstance(e, list)):
            return None, "finnhub_no_data"
        sp = e[0].get("surprisePercent")
        period = e[0].get("period")
        age_days = _surprise_period_age_days(period)
        _LEGACY_PERIOD_OBS[ticker] = {"period": period, "period_age_days": age_days}
        if sp is None or sp <= 0:
            return None, "surprise_nonpositive"
        if age_days is None or age_days > EARNINGS_MAX_PERIOD_AGE_DAYS or age_days < -7:
            print(
                f"[ext_entry] {ticker} surprise STALE rejected (legacy): period={period!r} "
                f"age_days={age_days} limit={EARNINGS_MAX_PERIOD_AGE_DAYS}"
            )
            return None, "surprise_stale"
        return {"surprise_pct": sp, "period": period, "period_age_days": age_days}, None
    except Exception as exc:
        print(f"[ext_entry] {ticker} finnhub earnings failed: {type(exc).__name__}: {exc}")
        return None, "finnhub_error"


_FINNHUB_TODAY_CAL_CACHE = {}


def _finnhub_today_calendar(day=None):
    """指定 ET 日历日的 Finnhub 全市场财报日历 (per-process, 按日缓存)。

    返回 (by_ticker|None, sub_reason|None)。默认当日; 传 `day` 可读历史日 —— RFC-102:
    盘前配对「昨日 AMC」时读昨日行取 actual (该日 actual 次日早晨通常已入库)。
    一次调用覆盖该日全市场 (替代逐票 company_earnings 的新判据②数据源), cron 每 tick 新进程
    ⇒ 缓存天然按 tick 失效。源侧失败 fail-closed。
    """
    today = day or _today_et()
    cached = _FINNHUB_TODAY_CAL_CACHE.get(today)
    if cached is not None:
        return cached
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        c = _get_finnhub_client()
        if c is None:
            return None, "finnhub_client_unavailable"
        resp = c.earnings_calendar(today, today, None)
        rows = (resp or {}).get("earningsCalendar") or []
    except Exception as exc:
        print(f"[ext_entry] finnhub earnings_calendar failed: {type(exc).__name__}: {exc}")
        return None, "finnhub_error"
    by_ticker = {}
    for row in rows:
        tick = str(row.get("symbol") or "").upper()
        if tick and tick not in by_ticker:
            by_ticker[tick] = row
    _FINNHUB_TODAY_CAL_CACHE[today] = (by_ticker, None)
    return by_ticker, None


def _earnings_surprise_calendar_actual(ticker, day=None):
    """新判据② (2026-09-23 口径修正): Finnhub 日历行的 epsActual vs epsEstimate > 0。

    `day` = 事件的 reportDate (RFC-102: 昨日 AMC 事件读昨日行), 缺省当日。
    fail-close 三态 (DEB-20260923 §14.4): actual_missing / estimate_missing / estimate_zero。
    """
    cal, err = _finnhub_today_calendar(day)
    if cal is None:
        return None, err
    row = cal.get(ticker)
    if row is None:
        return None, "actual_missing"
    actual = row.get("epsActual")
    est = row.get("epsEstimate")
    if actual is None:
        return None, "actual_missing"
    if est is None or isinstance(est, bool) or not isinstance(est, (int, float)):
        return None, "estimate_missing"
    if float(est) == 0:
        return None, "estimate_zero"
    try:
        surp = (float(actual) - float(est)) / abs(float(est)) * 100.0
    except (TypeError, ValueError):
        return None, "actual_missing"
    if surp <= 0:
        return None, "surprise_nonpositive"
    quarter, year = row.get("quarter"), row.get("year")
    return {
        "surprise_pct": surp,
        "actual": float(actual),
        "estimate": float(est),
        "period": f"{year}-Q{quarter}" if quarter and year else None,
        "period_age_days": None,
        "report_phase": row.get("hour"),
    }, None


def _actual_with_day_union(ticker, session, event_day):
    """判据② 取 actual: **事件日优先, 配对表里的其它日期兜底** (RFC-102 §2.6)。

    为什么需要: 源之间会对同一事件的**日期/相位**打架。实测 2026-09-23: futu 日历把 WOR 记成
    "当日 BMO", 而 Finnhub(9/22 行) 与 Nasdaq(9/22 日历) **一致记为 9/22 AMC**
    (actual 0.82 vs est 0.7575 ≈ +8.3%) ⇒ 老逻辑取"事件日=今日"的 actual 落空, 把一只
    "+8% beat + 盘前 +19% 跳空" 的票读成 actual_missing 拦掉。取数日只在**配对表内**兜底
    (默认单日 ⇒ 与旧行为 byte 级一致; 盘前开配对 ⇒ {今日, 昨日}), 命中日写入 `actual_day` 供审计。
    返回 (res|None, reason) —— 与 `_earnings_surprise_calendar_actual` **同形**, 便于下游无缝替换;
    命中时 `res["actual_day"]` = 真实取数日 (审计用)。reason 取**首个**失败原因 (事件日的最重要)。
    """
    days = []
    for offset, _phase in _session_event_pairs(session):
        day = _shift_et_day(_today_et(), offset)
        if day not in days:
            days.append(day)
    if event_day and event_day not in days:
        days.insert(0, event_day)
    reason = None
    for day in days:
        res, sub = _earnings_surprise_calendar_actual(ticker, day)
        if res is not None:
            res["actual_day"] = day
            res["event_date"] = event_day
            return res, None
        if reason is None:
            reason = sub
    return None, reason


_EARNINGS_CRITERION_SHADOW_KEYS = None
# 旧判据最近一次观测到的 period/龄 (即使被判 stale 也留读数, 供对照台账诊断"为什么被拦")。
_LEGACY_PERIOD_OBS = {}
# 入场侧上下文 (票, 相位) → {change_pct, volume, in_band, vol_ok} — 由 collect() 在调用
# 判据②**之前**写入, 供对照台账落库。用旁路而非改函数签名: 既有测试对 `_earnings_surprise`
# 打的是 2 参 stub, 改签名会一次性炸掉 5 个测试文件里的桩 (2026-09-23 实测 23 红)。
_ENTRY_CTX_OBS = {}


def _record_earnings_criterion_shadow(symbol, session, ticker, legacy, new, extra=None,
                                        event_date=None):
    """新旧判据②对照台账 (2026-09-23, 按 票-ET日 去重一行)。

    为什么需要: "旧判据在生产上误拦多少" 目前只有人群几何读数 (83.7% = 日历标签口径的**上界**),
    生产口径 (company_earnings) 从未实测 ⇒ 本台账给「legacy 拦而 new 放」的真实日计数,
    供 10 月财报季裁决修口径的收益 (DEB-20260923 §7.1 对照计数的落地载体)。
    异常必须留痕不得静默 (except:pass 棘轮守卫)。
    """
    global _EARNINGS_CRITERION_SHADOW_KEYS
    try:
        today = _today_et()
        if _EARNINGS_CRITERION_SHADOW_KEYS is None:
            keys = set()
            if EARNINGS_CRITERION_SHADOW_PATH.exists():
                with EARNINGS_CRITERION_SHADOW_PATH.open() as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            print(f"[ext_entry] criterion shadow 行解析失败: {type(exc).__name__}")
                            continue
                        keys.add(f"{row.get('symbol')}|{row.get('et_date')}")
            _EARNINGS_CRITERION_SHADOW_KEYS = keys
        key = f"{ticker}|{today}"
        if key in _EARNINGS_CRITERION_SHADOW_KEYS:
            return
        legacy_res, legacy_reason = legacy
        new_res, new_reason = new
        EARNINGS_CRITERION_SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EARNINGS_CRITERION_SHADOW_PATH.open("a") as handle:
            handle.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "et_date": today,
                "session": session,
                "symbol": symbol,
                "criterion": EARNINGS_CRITERION,
                "legacy_pass": legacy_res is not None,
                "legacy_reason": legacy_reason,
                "new_pass": new_res is not None,
                "new_reason": new_reason,
                "legacy_blocked_new_pass": bool(legacy_res is None and new_res is not None),
                "report_date": event_date,
                "actual_day": (new_res or {}).get("actual_day"),
                "new_surprise_pct": (new_res or {}).get("surprise_pct"),
                "actual": (new_res or {}).get("actual"),
                "estimate": (new_res or {}).get("estimate"),
                "legacy_period": (legacy_res or _LEGACY_PERIOD_OBS.get(ticker) or {}).get("period"),
                "legacy_period_age_days": (legacy_res or _LEGACY_PERIOD_OBS.get(ticker) or {}).get("period_age_days"),
                # 旧判据 stale 四态 (DEB-20260923 §13.3.2/§14.2, DEB-20260924 裁决 A):
                #   endpoint_empty / period_unparseable / stale_gt_150 / future_lt_-7。
                #   非 stale 原因时为 None。10 月财报季对照裁决必需字段。
                "legacy_stale_state": _classify_legacy_stale_state(
                    legacy_reason, legacy_res or _LEGACY_PERIOD_OBS.get(ticker) or {}
                ),
                # 入场侧上下文 (2026-09-23): 带/量闸状态 — 让 10 月能**分离**评估
                # "旧判据误拦" 与 "涨幅带挡掉", 二者否则混在一个样本里不可归因。
                **(extra or {}),
            }, ensure_ascii=False) + "\n")
        _EARNINGS_CRITERION_SHADOW_KEYS.add(key)
    except Exception as exc:
        print(f"[ext_entry] criterion shadow 写入失败: {type(exc).__name__}: {exc}")


def _earnings_surprise(symbol, session):
    """同日同相位 earnings beat 判定 (2026-09-23 判据②口径修正, RFC-097)。

    结构: 判据①事件存在 (同日同相位, futu/AV 日历) → 判据② beat 证明
      · calendar_actual (默认/新): 当日 Finnhub 日历行 epsActual vs epsEstimate > 0
      · legacy_period (回滚): company_earnings[0] surprise>0 + period 龄窗 [-7,150]

    返回 (earnings|None, sub_reason|None)。判据①子原因见 `_earnings_calendar_event`;
    判据②新子原因: finnhub_client_unavailable / finnhub_error / actual_missing /
      estimate_missing (一致预期缺失) / estimate_zero (预期为 0, 防除零) / surprise_nonpositive。
    """
    event, calendar_reason = _earnings_calendar_event(symbol, session)
    if event is None:
        return None, calendar_reason
    ticker = symbol.replace("US.", "").upper()
    # RFC-102: actual 一律取**事件所属日**的日历行 (昨日 AMC 的 actual 在昨日行里, 今日行没有)。
    event_day = str(event.get("reportDate") or _today_et())
    if EARNINGS_CRITERION == "legacy_period":
        legacy = _earnings_surprise_legacy_finnhub(ticker)
        res, reason = legacy
        new = _actual_with_day_union(ticker, session, event_day) if EARNINGS_CRITERION_SHADOW else (None, None)
    else:
        new = _actual_with_day_union(ticker, session, event_day)
        legacy = _earnings_surprise_legacy_finnhub(ticker) if EARNINGS_CRITERION_SHADOW else (None, None)
        res, reason = new
    if res is not None:
        res.setdefault("report_date", event.get("reportDate"))
        res.setdefault("report_phase", event.get("timeOfTheDay") or event.get("pub_type"))
    if EARNINGS_CRITERION_SHADOW:
        _record_earnings_criterion_shadow(symbol, session, ticker, legacy, new,
                                             extra=_ENTRY_CTX_OBS.get((symbol, session)),
                                             event_date=event_day)
    return res, reason


def _print_gate_counts(session, total, counts):
    """每轮一行 per-gate 计数 (2026-09-18 用户令「需要」; 纯观测, 不参与判定).

    为什么需要: 本链路只有价格闸与盘前流动性闸留台账 (jsonl), 而 attempt 已占 / 已持仓 /
    动量量能门 / 财报门都是**静默 continue** ⇒ 日志里"连续多轮 0 笔"无法与"某一闸卡死"区分
    (实测 `extended_hours_pretrade_gate_rejects.jsonl` 历史 0 行, 归因一度只能靠外部只读探针).
    异常不得影响主流程。
    """
    try:
        keys = (
            "attempt_reserved", "held", "price_gate", "momentum_volume",
            "earnings", "earnings_phase", "earnings_stale", "earnings_src_err",
            "market_cap_missing", "market_cap_too_low",
            "turnover_too_low", "entry_window", "liquidity", "structure",
            "position_too_small", "ordered",
            # 研究臂摘要 (2026-09-22): 池大小与实际下单数; 细分类拒绝在 research_* 计数键
            "research_pool", "research_ordered",
        )
        body = " ".join(f"{k}={int(counts.get(k, 0))}" for k in keys)
        print(f"[ext_entry] 闸计数[{session}] 候选={total} {body}")
        # 事件源可见性 (2026-09-23 用户令「先可见」): 独立一行, 不进主行 (主行是既有解析契约)。
        #   union 模式下的 finnhub 计数 = "靠回落源才认出来的票" ⇒ 直接反映并源到底多放了多少。
        if EARNINGS_EVENT_SOURCE != "calendar":
            print(f"[ext_entry] 事件源[{session}] mode={EARNINGS_EVENT_SOURCE} "
                  f"futu={len(_EVENT_SRC_OBS['futu'])} finnhub={len(_EVENT_SRC_OBS['finnhub'])}")
        # 影子档放行的票里有多少**同时是事件票** —— 只有这部分才可能真的走到下单
        #   (量闸只是门链中间一道; 非事件票在判据① 必死)。不区分会让读数被误读成"放宽能放行这么多票"
        #   (实测 08:1x 一轮影子放行 5 只, 全是异动榜小票)。
        _evt_names = set(_EVENT_SRC_OBS["futu"]) | set(_EVENT_SRC_OBS["finnhub"])
        _EVENT_SRC_OBS["futu"].clear()
        _EVENT_SRC_OBS["finnhub"].clear()
        # RFC-100 量闸分档读数 (独立一行; 主行的"空格分隔 k=int"契约不动)。
        if _VOL_BUCKET_CF["live"] or _VOL_BUCKET_CF["shadow"]:
            print(f"[ext_entry] 量闸分档[{session}] live门槛={_VOL_BUCKET_CF['thr'] or _min_volume_now()} "
                  f"旧门槛={int(MIN_VOLUME)} 分档放行={_VOL_BUCKET_CF['live']} "
                  f"影子分档会放行={_VOL_BUCKET_CF['shadow']} "
                  f"标的={','.join(sorted(set(_VOL_BUCKET_CF['symbols']))[:8])} "
                  f"影子标的={','.join(sorted(set(_VOL_BUCKET_CF['shadow_symbols']))[:8])} "
                  f"影子放行中事件票={sum(1 for x in set(_VOL_BUCKET_CF['shadow_symbols']) if x.replace('US.', '') in _evt_names)} "
                  f"影子放行且在带内={_VOL_BUCKET_CF['shadow_in_band']}")
        _VOL_BUCKET_CF["live"] = 0
        _VOL_BUCKET_CF["shadow"] = 0
        _VOL_BUCKET_CF["symbols"].clear()
        _VOL_BUCKET_CF["shadow_symbols"].clear()
        _VOL_BUCKET_CF["shadow_in_band"] = 0
        # 2026-09-23 (盘后核查): 研究臂的**细分类**拒绝键此前只写不报 ⇒ "研究臂开着却 0 单"
        #   与"研究臂卡死"在日志面不可区分 (复算只能自己 import 手调 `_research_downstream_gates`)。
        #   细分类键集是动态的 (`research_<grade_reason>`), 无法静态列进 keys, 且把 `{...}` 塞进
        #   上面那行会破坏其"空格分隔 k=int"契约 (用例按此解析) ⇒ 单独一行, 键名与主行不交叉。
        detail = {
            k: int(v) for k, v in counts.items()
            if k.startswith("research_") and k not in ("research_pool", "research_ordered")
        }
        if detail:
            print(f"[ext_entry] 研究臂拒绝细分[{session}] 池={int(counts.get('research_pool', 0))} "
                  f"下单={int(counts.get('research_ordered', 0))} "
                  f"rejects={json.dumps(detail, ensure_ascii=False, sort_keys=True)}")
    except Exception:
        _log.warning("research arm reject detail print failed", exc_info=True)


def collect(session, dry=False):
    """扫描事件驱动开仓信号；当前 SIM 无券商 STOP 时 fail-closed。"""
    if session not in SESSION_WINDOWS:
        return [{"ok": False, "skip": "invalid_session"}]
    if not _is_trading_day() or not _in_session_window(session):
        return [{"ok": False, "skip": "outside_trading_window"}]

    # Futu SIM 已实测拒绝 STOP 与 STOP_LIMIT（DEB-20260805 §5.1）。
    # 2026-09-17 用户裁方案1: 接受规则引擎兜底止损, ALLOW_SIM_ENTRY 开关恢复下单.
    #   券商 STOP 不可用 → 止损改由 ①开盘规则引擎读 initial_sl ②盘前 tighten 由 exit executor 平仓.
    if not dry and not ALLOW_SIM_ENTRY and _mode().is_simulate:
        return [{"ok": False, "skip": "simulate_stop_unsupported_fail_closed"}]

    if dry:
        held = _held_symbols()
        results = []
        pretrade_rejects = []
        today = _today_et()
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        price_gate_rejects = []
        try:
            movers = _scan_candidates(session, qctx)
            counts = {}
            earnings_reason_counts = {}
            earnings_reject_details = []
            earnings_near_misses = []
            research_pool = []
            research_active = _research_arm_active()
            for item in movers:
                symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
                if symbol in held:
                    counts["held"] = counts.get("held", 0) + 1
                    continue
                if not _price_in_range(price):
                    counts["price_gate"] = counts.get("price_gate", 0) + 1
                    price_gate_rejects.append({"symbol": symbol, "price": price,
                                               "change_pct": round(change_pct, 2), "volume": volume})
                    continue
                # 观测与决策解耦 (2026-09-23): 判据②评估**先于**涨幅带/量闸。
                # 带只该挡下单, 不该挡观测 —— 否则对照台账 (旧判据在生产误拦多少) 的样本
                # 被带截断, 且最典型的"当日财报 pop 票"(跳空 >2%) 全看不到。
                # 计数语义不变: 带/量不达标仍在 momentum_volume 分支 continue。
                _min_vol = _min_volume_now(session)   # RFC-100: 分档门槛 (未配置=MIN_VOLUME 原值)
                _chg_ok, _vol_ok = _entry_chg_ok(change_pct), volume >= _min_vol
                # RFC-100 反事实观测 (纯观测, 不参与判定): 分档放行了多少票次、影子分档还会放行多少。
                #   位置在"判完门槛"处、与后续走哪条支路无关 —— 放在分支里会漏掉"过闸后死在
                #   下一道门"的票 (实测 05:2x 一轮分档放行 0 条读数 = 恰恰是这种漏计)。
                if _vol_ok and volume < MIN_VOLUME:
                    _VOL_BUCKET_CF["live"] += 1
                    _VOL_BUCKET_CF["thr"] = _min_vol
                    _VOL_BUCKET_CF["symbols"].append(symbol)
                elif not _vol_ok and volume >= _min_volume_for(
                        MIN_VOLUME_BUCKETS_SHADOW_RAW, _et_hour_now()):
                    _VOL_BUCKET_CF["shadow"] += 1
                    _VOL_BUCKET_CF["shadow_symbols"].append(symbol)
                    if _chg_ok:   # 影子放行后**还过不过涨幅带** (RFC-103 Q2: 放宽不能只把人送到下一堵墙)
                        _VOL_BUCKET_CF["shadow_in_band"] += 1
                _ENTRY_CTX_OBS[(symbol, session)] = {
                    "change_pct": round(change_pct, 2), "volume": volume,
                    "in_band": bool(_chg_ok), "vol_ok": bool(_vol_ok),
                }
                # 方向判定与 live 臂同步 (RFC-099, 2026-09-23 复查批 P1-1): 生产已开
                # SHORT_ALLOW ⇒ dry 观测臂必须看到同一人群, 否则研究影子/日报/闸计数
                # 的空侧全盲 (违背「先可见再打磨优化」)。默认 OFF ⇒ 恒 LONG ⇒ 语义不变。
                # RFC-167: 判定与近失带观测合并到 SSOT 助手 (两条扫描臂共用, 防内联式漂移)。
                _direction = _classify_ext_direction(symbol, session, change_pct, price, volume)
                earnings, earnings_reason = _earnings_surprise(symbol, session)
                if (_direction == "LONG" and not _chg_ok) or not _vol_ok:
                    counts["momentum_volume"] = counts.get("momentum_volume", 0) + 1
                    continue
                # 空侧事件门 (与 live 臂同一口径): 财报事件必须存在, 不看 beat/miss。
                if _direction == "SHORT":
                    event, event_reason = _earnings_calendar_event(symbol, session)
                    if event is None:
                        earnings_reason = event_reason or earnings_reason or "calendar_missing"
                        counts["earnings"] = counts.get("earnings", 0) + 1
                        earnings_reason_counts[earnings_reason] = (
                            earnings_reason_counts.get(earnings_reason, 0) + 1
                        )
                        if earnings_reason != "calendar_missing":
                            earnings_reject_details.append({
                                "symbol": symbol,
                                "sub_reason": earnings_reason,
                                "price": price,
                                "change_pct": round(change_pct, 2),
                                "volume": volume,
                            })
                        continue
                if _direction == "LONG" and (
                        not earnings or not earnings.get("surprise_pct")
                        or earnings["surprise_pct"] <= 0):
                    earnings_reason = earnings_reason or "surprise_nonpositive"
                    counts["earnings"] = counts.get("earnings", 0) + 1
                    earnings_reason_counts[earnings_reason] = (
                        earnings_reason_counts.get(earnings_reason, 0) + 1
                    )
                    if earnings_reason != "calendar_missing":
                        earnings_reject_details.append({
                            "symbol": symbol,
                            "sub_reason": earnings_reason,
                            "price": price,
                            "change_pct": round(change_pct, 2),
                            "volume": volume,
                        })
                    else:
                        earnings_near_misses.append({
                            "symbol": symbol,
                            "price": price,
                            "change_pct": change_pct,
                            "volume": volume,
                        })
                        # 研究臂池 (纪律③人群): 仅 calendar_missing, 与 near-miss 台账同人群。
                        if research_active:
                            research_pool.append(item)
                            _rsrc = str(item.get("_source") or "?")
                            counts[f"research_src_{_rsrc}_pool"] = (
                                counts.get(f"research_src_{_rsrc}_pool", 0) + 1
                            )
                            _record_research_funnel(session, today, item, "selected")
                    continue
                quote = _liquidity_quote(qctx, symbol)
                grade, grade_reason = _entry_grade(price, volume, quote.get("market_cap") if quote else 0)
                if grade is None:
                    counts[grade_reason] = counts.get(grade_reason, 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "reason": grade_reason,
                        "quote": quote,
                    })
                    continue
                window_reason = _entry_window_reason(session, grade)
                if window_reason:
                    counts["entry_window"] = counts.get("entry_window", 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "reason": window_reason,
                        "grade": grade,
                    })
                    continue
                position_usd = POSITION_USD if grade == "A" else POSITION_B_USD
                qty = int(position_usd / price)
                spread_limit = MAX_SPREAD_PCT_A if grade == "A" else MAX_SPREAD_PCT
                depth_ratio = MIN_ASK_DEPTH_RATIO_A if grade == "A" else MIN_ASK_DEPTH_RATIO
                gate, reject_reason = _pretrade_gate(
                    price, qty, quote, today,
                    max_spread_pct=spread_limit,
                    min_ask_depth_ratio=depth_ratio,
                    side=_direction,
                )
                if gate is None:
                    counts["liquidity"] = counts.get("liquidity", 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "qty": qty,
                        "reason": reject_reason,
                        "quote": quote,
                    })
                    continue
                order_price = gate["order_price"]
                structure, structure_reason, structure_metrics = _entry_structure_gate(
                    qctx, symbol, session, order_price
                )
                if structure is None:
                    counts["structure"] = counts.get("structure", 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "qty": qty,
                        "reason": structure_reason,
                        "grade": grade,
                        "structure_metrics": structure_metrics,
                    })
                    continue
                qty = int(position_usd / order_price)
                if qty < 1:
                    counts["position_too_small"] = counts.get("position_too_small", 0) + 1
                    continue
                counts["ordered"] = counts.get("ordered", 0) + 1
                results.append({
                    "ok": True, "dry": True, "symbol": symbol,
                    "qty": qty, "price": order_price, "decision_price": price,
                    "session": session, "change_pct": round(change_pct, 2),
                    "volume": volume, "earnings": earnings,
                    "position_usd": position_usd, "grade": grade,
                    "structure": structure,
                    "pretrade": gate,
                    # 观测面方向标 (2026-09-23 复查批): dry 台账按方向可分列,
                    # 空侧候选不再与多侧混在同一个 "ordered" 里无法归因。
                    "direction": _direction,
                })
            # 研究臂 dry 通道: 严格臂候选列完后补列 (无配额模拟, 与严格臂 dry 对称)。
            _existing_research = _research_existing_symbols(research_pool, results)
            for _ri in _research_source_candidates(session, qctx, _existing_research):
                research_pool.append(_ri)
                _rsrc = str(_ri.get("_source") or "?")
                counts[f"research_src_{_rsrc}_pool"] = (
                    counts.get(f"research_src_{_rsrc}_pool", 0) + 1
                )
                _record_research_funnel(session, today, _ri, "selected")
            if research_pool:
                counts["research_pool"] = len(research_pool)
                _research_pass_dry(
                    research_pool, session, today, qctx, counts, results,
                    pretrade_rejects,
                )
            counts["earnings_phase"] = (
                earnings_reason_counts.get("calendar_phase_mismatch", 0)
                + earnings_reason_counts.get("calendar_phase_unknown", 0)
            )
            counts["earnings_stale"] = earnings_reason_counts.get("surprise_stale", 0)
            counts["earnings_src_err"] = (
                earnings_reason_counts.get("calendar_source_error", 0)
                + earnings_reason_counts.get("finnhub_error", 0)
                + earnings_reason_counts.get("finnhub_client_unavailable", 0)
            )
            _print_gate_counts(session, len(movers), counts)
            _record_price_gate_rejects(session, price_gate_rejects)
            _record_execution_price_shadow(session, movers)
            _record_pretrade_gate_rejects(session, pretrade_rejects)
            _record_earnings_gate_rejects(
                session, today, earnings_reason_counts, earnings_reject_details,
                candidates=[str(m.get("symbol") or "") for m in movers],
            )
            _record_earnings_near_misses(session, today, earnings_near_misses)
        finally:
            qctx.close()
        return results or [{"skip": "no_signal_passed_all_gates"}]

    # ETH 真钱下单需双条件: TradingMode=live (AUTO_TRADE_ACCOUNT+ALLOW_REAL_TRADING)
    #   **且** EXT_REAL_AUTHORIZED=true (ETH 链从未实盘跑过, 独立闸)。
    if _mode().is_live and not _ext_real_authorized():
        return [{"ok": False, "skip": "real_not_authorized"}]

    today = _today_et()
    with _state_lock() as locked:
        if not locked:
            return [{"ok": False, "skip": "another_executor_running"}]
        state = _load_state()
        state.setdefault("version", 2)
        state.setdefault("attempts", {})
        state.setdefault("opened_today", {})
        reconciliation_tctx = _open_trade_context()
        try:
            active_orders, reconcile_conflicts = _reconcile_broker_entry_orders(
                reconciliation_tctx, _mode().futu_trd_env(),
                _mode().account_id(), session,
                state=state, today=today,
            )
        except Exception as exc:
            return [{
                "ok": False,
                "skip": "broker_order_reconcile_failed",
                "error": str(exc),
            }]
        finally:
            reconciliation_tctx.close()
        if reconcile_conflicts:
            _save_state(state)
            return [{
                "ok": False,
                "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            }]
        if active_orders:
            _save_state(state)
            return [{
                "ok": False,
                "skip": "active_ext_entry_order",
                "orders": active_orders,
            }]
        _save_state(state)
        attempts = state["attempts"]
        opened_today = state["opened_today"]
        day_key = f"{today}:{session}"
        total_today = _opened_today_total(opened_today, today)
        session_today = int(opened_today.get(day_key, 0) or 0)
        if total_today >= MAX_PER_DAY:
            return [{"skip": "daily_limit", "total_today": total_today}]
        # P1-4: 保留额度条件化 — 当日日历无盘后相位财报时, 盘前可用满全天额度。
        premarket_limit = PREMARKET_MAX_PER_DAY
        reserved_for_afterhours = True
        if session == "premarket" and PREMARKET_RESERVE_CONDITIONAL:
            reserved_for_afterhours = _afterhours_earnings_expected()
            if not reserved_for_afterhours:
                premarket_limit = MAX_PER_DAY
        if session == "premarket" and session_today >= premarket_limit:
            return [{
                "skip": "premarket_session_limit",
                "session_today": session_today,
                "limit": premarket_limit,
                "reserved_for_afterhours": reserved_for_afterhours,
            }]
        exposure_gate = _open_exposure_gate()
        if exposure_gate:
            _save_state(state)
            return [exposure_gate]

        held = _held_symbols()
        results = []
        price_gate_rejects = []
        pretrade_rejects = []
        from futu import OpenQuoteContext
        qctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                                port=int(os.getenv("FUTU_OPD_PORT", "11111")))
        try:
            movers = _scan_candidates(session, qctx)
            counts = {}
            earnings_reason_counts = {}
            earnings_reject_details = []
            earnings_near_misses = []
            research_pool = []
            research_active = _research_arm_active()
            for item in movers:
                symbol, price, change_pct, volume = item["symbol"], item["px"], item["chg"], item["vol"]
                attempt_key = f"{today}:{symbol}"
                if attempt_key in attempts:
                    counts["attempt_reserved"] = counts.get("attempt_reserved", 0) + 1
                    continue
                if symbol in held:
                    counts["held"] = counts.get("held", 0) + 1
                    continue
                if not _price_in_range(price):
                    counts["price_gate"] = counts.get("price_gate", 0) + 1
                    price_gate_rejects.append({"symbol": symbol, "price": price,
                                               "change_pct": round(change_pct, 2), "volume": volume})
                    continue
                # 观测与决策解耦 (2026-09-23): 判据②评估**先于**涨幅带/量闸。
                # 带只该挡下单, 不该挡观测 —— 否则对照台账 (旧判据在生产误拦多少) 的样本
                # 被带截断, 且最典型的"当日财报 pop 票"(跳空 >2%) 全看不到。
                # 计数语义不变: 带/量不达标仍在 momentum_volume 分支 continue。
                _min_vol = _min_volume_now(session)   # RFC-100: 分档门槛 (未配置=MIN_VOLUME 原值)
                _chg_ok, _vol_ok = _entry_chg_ok(change_pct), volume >= _min_vol
                # RFC-100 反事实观测 (纯观测, 不参与判定): 分档放行了多少票次、影子分档还会放行多少。
                #   位置在"判完门槛"处、与后续走哪条支路无关 —— 放在分支里会漏掉"过闸后死在
                #   下一道门"的票 (实测 05:2x 一轮分档放行 0 条读数 = 恰恰是这种漏计)。
                if _vol_ok and volume < MIN_VOLUME:
                    _VOL_BUCKET_CF["live"] += 1
                    _VOL_BUCKET_CF["thr"] = _min_vol
                    _VOL_BUCKET_CF["symbols"].append(symbol)
                elif not _vol_ok and volume >= _min_volume_for(
                        MIN_VOLUME_BUCKETS_SHADOW_RAW, _et_hour_now()):
                    _VOL_BUCKET_CF["shadow"] += 1
                    _VOL_BUCKET_CF["shadow_symbols"].append(symbol)
                    if _chg_ok:   # 影子放行后**还过不过涨幅带** (RFC-103 Q2: 放宽不能只把人送到下一堵墙)
                        _VOL_BUCKET_CF["shadow_in_band"] += 1
                _ENTRY_CTX_OBS[(symbol, session)] = {
                    "change_pct": round(change_pct, 2), "volume": volume,
                    "in_band": bool(_chg_ok), "vol_ok": bool(_vol_ok),
                }
                # 方向 (RFC-099): LONG=现役温和带; SHORT=跳空 ≥ SHORT_GAP_MIN。
                #   默认 OFF ⇒ _direction 恒为 LONG ⇒ 下行闸/计数语义 byte 级不变。
                #   注: 上方观测 pass (2033~2262) 不动 —— 它只记观测台账, 不下单。
                # RFC-167: 判定与近失带观测合并到 SSOT 助手 (两条扫描臂共用, 防内联式漂移)。
                _direction = _classify_ext_direction(symbol, session, change_pct, price, volume)
                earnings, earnings_reason = _earnings_surprise(symbol, session)
                if (_direction == "LONG" and not _chg_ok) or not _vol_ok:
                    counts["momentum_volume"] = counts.get("momentum_volume", 0) + 1
                    continue
                # 空侧 (RFC-099 §4/§8): 财报**事件**必须存在 —— "财报定有没有事件可交易,
                # 幅度定做哪一边" ⇒ 证据人群 = 财报票 + 跳空≥3%, 不是任意异动票;
                # 但不看 beat/miss (禁项 1: 财报好坏不得定方向)。
                if _direction == "SHORT":
                    event, event_reason = _earnings_calendar_event(symbol, session)
                    if event is None:
                        earnings_reason = event_reason or earnings_reason or "calendar_missing"
                        counts["earnings"] = counts.get("earnings", 0) + 1
                        earnings_reason_counts[earnings_reason] = (
                            earnings_reason_counts.get(earnings_reason, 0) + 1
                        )
                        if earnings_reason != "calendar_missing":
                            earnings_reject_details.append({
                                "symbol": symbol,
                                "sub_reason": earnings_reason,
                                "price": price,
                                "change_pct": round(change_pct, 2),
                                "volume": volume,
                            })
                        continue
                if _direction == "LONG" and (
                        not earnings or not earnings.get("surprise_pct")
                        or earnings["surprise_pct"] <= 0):
                    earnings_reason = earnings_reason or "surprise_nonpositive"
                    counts["earnings"] = counts.get("earnings", 0) + 1
                    earnings_reason_counts[earnings_reason] = (
                        earnings_reason_counts.get(earnings_reason, 0) + 1
                    )
                    if earnings_reason != "calendar_missing":
                        earnings_reject_details.append({
                            "symbol": symbol,
                            "sub_reason": earnings_reason,
                            "price": price,
                            "change_pct": round(change_pct, 2),
                            "volume": volume,
                        })
                    else:
                        earnings_near_misses.append({
                            "symbol": symbol,
                            "price": price,
                            "change_pct": change_pct,
                            "volume": volume,
                        })
                        # 研究臂池 (纪律③人群): 仅 calendar_missing, 与 near-miss 台账同人群。
                        #   attempt/held/价/动量/量闸已在本循环上方排除, 池内皆为四闸全过者。
                        if research_active:
                            research_pool.append(item)
                            _rsrc = str(item.get("_source") or "?")
                            counts[f"research_src_{_rsrc}_pool"] = (
                                counts.get(f"research_src_{_rsrc}_pool", 0) + 1
                            )
                            _record_research_funnel(session, today, item, "selected")
                    continue

                quote = _liquidity_quote(qctx, symbol)
                grade, grade_reason = _entry_grade(price, volume, quote.get("market_cap") if quote else 0)
                if grade is None:
                    counts[grade_reason] = counts.get(grade_reason, 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "reason": grade_reason,
                        "quote": quote,
                    })
                    continue
                window_reason = _entry_window_reason(session, grade)
                if window_reason:
                    counts["entry_window"] = counts.get("entry_window", 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "reason": window_reason,
                        "grade": grade,
                    })
                    continue
                position_usd = POSITION_USD if grade == "A" else POSITION_B_USD
                qty = int(position_usd / price)
                spread_limit = MAX_SPREAD_PCT_A if grade == "A" else MAX_SPREAD_PCT
                depth_ratio = MIN_ASK_DEPTH_RATIO_A if grade == "A" else MIN_ASK_DEPTH_RATIO
                gate, reject_reason = _pretrade_gate(
                    price, qty, quote, today,
                    max_spread_pct=spread_limit,
                    min_ask_depth_ratio=depth_ratio,
                    side=_direction,
                )
                if gate is None:
                    counts["liquidity"] = counts.get("liquidity", 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "qty": qty,
                        "reason": reject_reason,
                        "quote": quote,
                    })
                    continue
                order_price = gate["order_price"]
                structure, structure_reason, structure_metrics = _entry_structure_gate(
                    qctx, symbol, session, order_price
                )
                if structure is None:
                    counts["structure"] = counts.get("structure", 0) + 1
                    pretrade_rejects.append({
                        "symbol": symbol,
                        "decision_price": price,
                        "qty": qty,
                        "reason": structure_reason,
                        "grade": grade,
                        "structure_metrics": structure_metrics,
                    })
                    continue
                qty = int(position_usd / order_price)
                if qty < 1:
                    counts["position_too_small"] = counts.get("position_too_small", 0) + 1
                    continue
                counts["ordered"] = counts.get("ordered", 0) + 1
                attempts[attempt_key] = {
                    "status": "reserved", "session": session, "symbol": symbol,
                    "trade_date": today, "reserved_at": datetime.utcnow().isoformat() + "Z",
                }
                _save_state(state)
                try:
                    result = _execute(
                        symbol, qty, order_price, session, change_pct, volume,
                        earnings or {}, dry,
                        position_usd=position_usd, grade=grade, structure=structure,
                        side=_direction,
                    )
                    result["decision_price"] = price
                    result["pretrade"] = gate
                except Exception as exc:
                    result = {
                        "ok": False, "symbol": symbol, "session": session,
                        "error": f"executor_exception:{exc}",
                    }
                    _push(f"❌ 延长时段开仓执行器异常\n{symbol}\n{exc}")
                    _audit(result)
                result["symbol"] = symbol
                result["session"] = session
                attempts[attempt_key] = {
                    "status": "opened" if result.get("ok") else "failed",
                    "session": session,
                    "trade_date": today,
                    "order_id": result.get("buy_order_id"),
                    "stop_order_id": result.get("stop_order_id"),
                    "filled_qty": result.get("filled_qty"),
                    "avg_fill_price": result.get("avg_fill_price"),
                    "error": result.get("error") or result.get("skip"),
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                }
                results.append(result)
                if result.get("ok"):
                    total_today += 1
                    session_today += 1
                    opened_today[day_key] = session_today
                _save_state(state)
                if total_today >= MAX_PER_DAY or (
                    session == "premarket" and session_today >= premarket_limit
                ):
                    break
            # 研究臂真单通道 (纪律①严格臂优先): 只有严格臂循环收口后仍有剩余配额才跑。
            #   每单前复查配额+在仓帽 (_open_exposure_gate), 全链 gate_mode 打标。
            _existing_research = _research_existing_symbols(research_pool, results)
            for _ri in _research_source_candidates(session, qctx, _existing_research):
                research_pool.append(_ri)
                _rsrc = str(_ri.get("_source") or "?")
                counts[f"research_src_{_rsrc}_pool"] = (
                    counts.get(f"research_src_{_rsrc}_pool", 0) + 1
                )
                _record_research_funnel(session, today, _ri, "selected")
            if research_pool:
                counts["research_pool"] = len(research_pool)
                total_today, session_today = _research_pass_real(
                    research_pool, session, today, qctx, state, attempts,
                    opened_today, day_key, total_today, session_today,
                    premarket_limit, results, pretrade_rejects, counts,
                )
            counts["earnings_phase"] = (
                earnings_reason_counts.get("calendar_phase_mismatch", 0)
                + earnings_reason_counts.get("calendar_phase_unknown", 0)
            )
            counts["earnings_stale"] = earnings_reason_counts.get("surprise_stale", 0)
            counts["earnings_src_err"] = (
                earnings_reason_counts.get("calendar_source_error", 0)
                + earnings_reason_counts.get("finnhub_error", 0)
                + earnings_reason_counts.get("finnhub_client_unavailable", 0)
            )
            _print_gate_counts(session, len(movers), counts)
            _record_price_gate_rejects(session, price_gate_rejects)
            _record_execution_price_shadow(session, movers)
            _record_pretrade_gate_rejects(session, pretrade_rejects)
            _record_earnings_gate_rejects(
                session, today, earnings_reason_counts, earnings_reject_details,
                candidates=[str(m.get("symbol") or "") for m in movers],
            )
            _record_earnings_near_misses(session, today, earnings_near_misses)
        finally:
            qctx.close()
    return results or [{"skip": "no_signal_passed_all_gates"}]





def _write_open_position(
    symbol, qty, entry_px, decision_px, session, earn, chg, vol,
    buy_order_id="", position_usd=None, grade="B", structure=None,
    gate_mode="strict", side="LONG", decision_id="", admission_meta=None,
):
    """成交后写 open_positions + trade_records (盘中监控读 trade_records.metadata_json.stop_loss).
    返回 position_id. 幂等且不覆盖既有仓位.
    gate_mode 落两表 metadata_json (2026-09-22 研究臂样本隔离): 退出链只增键零影响,
    分析面按 metadata_json.gate_mode 过滤, research_momentum 与严格臂永不混算."""
    import uuid
    ensure_test_symbol_not_in_production_db(symbol, DB)
    conn = sqlite3.connect(DB, timeout=30)
    try:
        now = _now_et().isoformat()
        pos_id = f"EXT-{uuid.uuid4().hex[:12]}"
        # 空头 SL 在**上方** (STOP_PCT<0 ⇒ 取反), 否则盘中兜底会把空头当多头扫掉。
        init_sl = round(entry_px * (1 + STOP_PCT / 100 * (1 if side == "LONG" else -1)), 2)
        meta = json.dumps({"session": session, "decision_px": decision_px,
                           "position_id": pos_id, "entry_order_id": buy_order_id,
                           "direction": side,
                           "earnings_surprise_pct": (earn or {}).get("surprise_pct"),
                           "earnings_period": earn.get("period"),
                           "earnings_period_age_days": earn.get("period_age_days"),
                           "earnings_report_date": earn.get("report_date"),
                           "earnings_report_phase": earn.get("report_phase"),
                           "ext_change_pct": chg, "ext_volume": vol,
                           "ext_turnover_usd": float(entry_px or 0) * float(vol or 0),
                           "entry_grade": grade,
                           "gate_mode": gate_mode,
                           "research_source": (earn or {}).get("research_source"),
                           "late_premarket": (earn or {}).get("late_premarket"),
                           "entry_position_usd": float(position_usd or POSITION_USD),
                           "entry_structure": structure,
                           "cert_lock_pct": (
                               GRADE_A_CERT_LOCK_PCT if grade == "A"
                               else GRADE_B_CERT_LOCK_PCT
                           ),
                           "cohort": "B",  # 2026-09-17 用户裁: EXT 仓可隔夜 (cohort B + PLAYBOOK_SIM 默认允许, 盈利跳过 PRE_CLOSE_EXIT)
                           "stop_loss": init_sl,           # 盘中 monitor_playbook_sim_exits 读此键做 SL
                           "stop_source": "rule_engine_initial_sl",
                           # RFC-126: admission 统一口径 verdict 留痕 (record-only, 不拦单)。
                           # EXT 不写 playbook_instructions ⇒ 绕开 save_instruction 收口点;
                           # 此处成交时补算一遍, 使 EXT 与主链同口径可审计。
                           "decision_id": decision_id or None,
                           **(admission_meta or {}),
                           }, ensure_ascii=False)
        conn.execute("BEGIN IMMEDIATE")
        existing_order = None
        if buy_order_id:
            existing_order = conn.execute(
                "SELECT id FROM trade_records WHERE futu_order_id=? "
                "AND owner_strategy='extended_hours_entry' LIMIT 1",
                (buy_order_id,),
            ).fetchone()
        if existing_order:
            conn.commit()
            existing_position = conn.execute(
                "SELECT position_id FROM open_positions WHERE symbol=? AND size>0",
                (symbol,),
            ).fetchone()
            return existing_position[0] if existing_position else pos_id
        existing_position = conn.execute(
            "SELECT 1 FROM open_positions WHERE symbol=? AND size>0", (symbol,)
        ).fetchone()
        if existing_position:
            conn.rollback()
            raise RuntimeError(f"open position already exists for {symbol}")
        conn.execute(
            "INSERT INTO open_positions (position_id, symbol, direction, size, avg_entry_price, entry_time, "
            " strategy_id, source, owner_strategy, owner, mode, account_type, trader_type, "
            " is_filled, version, updated_at, metadata_json, sync_status, "
            " initial_sl, current_sl, stop_loss_price) "
            "VALUES (?, ?, ?, ?, ?, ?, 'extended_hours_entry', 'extended_hours_entry', "
            " 'extended_hours_entry', 'extended_hours_entry', 'normal', 'simulate', 'AUTO', 1, 1, ?, ?, 'pending', "
            " ?, ?, ?)",
            (pos_id, symbol, side, qty, entry_px, now, now, meta, init_sl, init_sl, init_sl))
        # P0-3 完整闭环: 写 trade_records — 盘中退出监控只读此表 (source_type PLAYBOOK_SIM 未平仓)
        conn.execute(
            "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, entry_price, "
            " quantity, source_type, trade_mode, mode, owner_strategy, strategy_id, "
            " futu_order_id, metadata_json, created_at, exit_price, exit_time, config_version, "
            " entry_session, decision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'PLAYBOOK_SIM', 'simulate', 'futu_simulate', "
            # 2026-10-20 记录卫生: 未平仓 exit_time 必须 NULL, 不能是空串 —
            #   `WHERE exit_time IS NULL` 的重启/运维查询会漏掉 EXT 未平仓行
            #   (10/19 实测 ASML/TSM/BA 三行空串, 只有 ADSK 是 NULL)。
            " 'extended_hours_entry', 'extended_hours_entry', ?, ?, ?, 0, NULL, 'v2', ?, ?)",
            # config_version='v2': 与规范写入方 app/storage/trade_store.py 一致。该表 schema 是
            # DEFAULT 'v1'，本列不写会落默认值 ⇒ W1 冻结窗出现版本分裂假象 (2026-09-18 F7 修复)。
            # decision_id (RFC-126): EXT-<date>-<symbol>-<session> 归因, 消灭空 did 成交。
            (_today_et(), symbol, side, now, entry_px, qty, buy_order_id, meta, now, session,
             decision_id or None))
        conn.commit()
        return pos_id
    finally:
        conn.close()


def _execute(
    symbol, qty, price, session, chg, vol, earn, dry,
    position_usd=None, grade="B", structure=None, gate_mode="strict",
    side="LONG",
):
    """SIM 真下单: 买入→等成交→写 open_positions(含 initial_sl 规则引擎兜底). 用户裁方案1.
    SIM 无券商STOP, 止损靠 ①开盘 monitor_playbook_sim_exits 读 initial_sl ②盘前 tighten exit executor.
    gate_mode (2026-09-22 研究臂): "strict"=财报门严格臂(默认, 缺省不打标即严格臂);
    "research_momentum"=SIM 研究臂 — 随 rec 落审计、随 metadata_json 落仓/交易记录 (样本永不混算)."""
    acc_id = _mode().account_id()
    # 2026-09-18: 测试占位标的 (TEST/US.TEST/*.TEST) 在**源头**挡掉 —— 它们只可能来自单测 artifact 或
    #   券商账户里的报废测试单 (如 US.TEST order 8185070), 不是真候选股; 且不推告警(见下)。
    if is_test_symbol(symbol):
        return {"ok": False, "skip": "test_symbol"}
    if _mode().is_live and not _ext_real_authorized():
        # 2026-09-18 用户令「不要 sim 和 real 有混淆」: 本脚本的 REAL 通道默认不授权;
        #   ld 属**配置态**问题 ⇒ **_环境级**一次提示, 一日一次 (不逐标的刷屏)。
        _push_once(
            "real_env_misconfigured",
            "⚠️ 延长时段开仓链路: TradingMode=实盘但 EXT_REAL_AUTHORIZED≠true"
            "（ETH 链路实盘闸未放行，已全部拦截；如需放行请显式设 EXT_REAL_AUTHORIZED=true）",
        )
        return {"ok": False, "skip": "real_not_authorized"}

    requested_position_usd = float(position_usd or POSITION_USD)
    rec = {"symbol": symbol, "qty": qty, "price": price, "session": session, "side": side,
           "change_pct": round(chg, 2), "volume": vol, "earnings": earn,
           "position_usd": requested_position_usd, "entry_grade": grade,
           "entry_structure": structure, "gate_mode": gate_mode}
    from futu import TrdSide, OrderType, TrdEnv, TimeInForce, Session, RET_OK
    tctx = _open_trade_context()
    try:
        env = _mode().futu_trd_env()
        # 显式传当日: 下单前安全闸只对**当日**已成交无主的单做建仓回补/冲突检测,
        #   跨日旧单不回补(entry_time 会是错的) — 与 collect() 同一口径。
        active_orders, reconcile_conflicts = _reconcile_broker_entry_orders(
            tctx, env, acc_id, session, today=_today_et()
        )
        if reconcile_conflicts:
            rec.update({
                "ok": False, "skip": "broker_order_reconcile_conflict",
                "orders": reconcile_conflicts,
            })
            _audit(rec)
            return rec
        if active_orders:
            rec.update({
                "ok": False, "skip": "active_ext_entry_order", "orders": active_orders,
            })
            _audit(rec)
            return rec
        if _probe_env_only():
            # 2026-09-23 (P1 类级治本): **显式探针**下单口短路 (仅认 FUTSEEK_PROBE, 不认 pytest);
            # 生产 unset ⇒ 行为完全不变。刻意不复用 `_probe_mode()`: 它把"进程内在跑 pytest"
            # 也算静默 ⇒ 会连带封死**测试对下单路径的正常断言**(实测 8 个 research_arm 用例
            # 期望 executed 非空而拿空) —— "测试不该真下单"该由 conftest/桩负责, 不该由生产代码替它决定。
            rec.update({"ok": False, "skip": "probe_mode", "symbol": symbol})
            _audit(rec)
            return rec
        trd_side = TrdSide.BUY
        if side == "SHORT":
            # 券源逐票预检 (与 scripts/probe_eth_short_open.py 同一方法):
            #   acctradinginfo_query.max_sell_short>=1 + get_margin_ratio.is_short_permit。
            #   SHORT_ALLOW=false ⇒ 本段不可达, place_order 恒为 BUY (行为不变)。
            if not SHORT_ALLOW:
                rec.update({"ok": False, "skip": "short_not_enabled"})
                _audit(rec)
                return rec
            try:
                ret_ti, ti = tctx.acctradinginfo_query(
                    order_type=OrderType.NORMAL, code=symbol, price=price,
                    trd_env=env, acc_id=acc_id)
                short_able = (int(ti.iloc[0].get("max_sell_short", 0))
                              if ret_ti == RET_OK and not ti.empty else 0)
                ret_mr, mr = tctx.get_margin_ratio([symbol])
                permit = (bool(mr.iloc[0].get("is_short_permit"))
                          if ret_mr == RET_OK and not mr.empty else False)
            except Exception as exc:
                short_able, permit = 0, False
                rec["short_check_error"] = str(exc)[:100]
            rec.update({"max_sell_short": short_able, "is_short_permit": permit})
            if short_able < 1 or not permit:
                rec.update({"ok": False, "skip": "short_borrow_unavailable"})
                _audit(rec)
                return rec
            # 富途官方语义: 下单入参只有 BUY/SELL; SELL_SHORT/BUY_BACK 只出现在
            # 查询/回报侧 (DEB-20260923 §4.3)。开空 = SELL, 平空 = BUY (exit_lifecycle 已如此)。
            trd_side = TrdSide.SELL
        ret, data = tctx.place_order(price=price, qty=qty, code=symbol, trd_side=trd_side,
                                     order_type=OrderType.NORMAL, trd_env=env, acc_id=acc_id,
                                     time_in_force=TimeInForce.DAY, session=Session.ETH, remark="ext_entry")
        if ret != RET_OK:
            rec["ok"] = False
            rec["error"] = str(data)[:120]
            _push(f"❌ {_stage(session)}开仓下单失败\n{symbol} {qty}股 @ ${price:.2f}\n{rec['error']}")
            _audit(rec)
            return rec
        buy_oid = str(data["order_id"].values[0]) if data is not None else ""
        rec["buy_order_id"] = buy_oid

        # 等成交；部分成交则撤掉剩余并等待券商确认终态。
        fill = wait_order_fill(tctx, env, acc_id, buy_oid, qty, price)
        rec["fill"] = fill
        if not fill.get("cancel_ok"):
            rec["ok"] = False
            rec["error"] = f"buy_cancel_failed_{fill.get('status')}:{fill.get('error')}"
            _push(f"⚠️ {_stage(session)}开仓撤单未确认\n{symbol} 订单 {buy_oid}\n状态 {fill.get('status')}")
            _audit(rec)
            return rec
        if not fill.get("filled"):
            rec["ok"] = False
            rec["error"] = f"buy_not_filled_{fill.get('status')}"
            _push(f"⏳ {_stage(session)}开仓未成交已撤单\n{symbol} {qty}股 @ ${price:.2f}\n状态 {fill.get('status')}")
            _audit(rec)
            return rec

        fqty = float(fill["filled_qty"]); fpx = float(fill["avg_fill_price"])
        rec.update({"filled_qty": fqty, "avg_fill_price": fpx})
        # 空头 SL 在**上方** (STOP_PCT 为负 ⇒ 取反); 退出链 _dir_sign 另行判向。
        # (RFC-126 提前到 admission 留痕之前, 使 max_sl_pct 判据拿到真值)
        rec["initial_sl"] = round(fpx * (1 + STOP_PCT / 100 * (1 if side == "LONG" else -1)), 2)
        # ── RFC-126: 归因收口 (record-only, 零行为变更) ──
        # 1) did: EXT-<date>-<symbol>-<session> — 消灭空 did 成交 (10/1 ACN 教训),
        #    幂等: 同单重跑生成同 did。
        ext_did = f"EXT-{_today_et()}-{symbol}-{session}"
        # 2) admission verdict 留痕: 与主链同一纯函数同一生产配置 (unified_config
        #    playbook.admission), 只记录不拦单。EXT 已有同源 $5 价格地板 (9/17
        #    ce54f64eb) ⇒ 预期恒 PASS; 出现 REJECT = 新信号, 体检脚本会 P2 提示。
        admission_meta = {}
        try:
            from app.playbook.admission import evaluate as _adm_evaluate
            _adm = _adm_evaluate({
                "date": _today_et(), "symbol": symbol,
                "direction": "SELL_SHORT" if side == "SHORT" else "BUY",
                "entry_price": fpx, "position_size": fqty,
                "stop_loss": rec.get("initial_sl") or 0,
            })
            rec["admission"] = {"verdict": _adm["verdict"], "reasons": _adm["reasons"],
                                "fee_bps": _adm["metrics"].get("fee_bps")}
            admission_meta = {"admission_verdict": _adm["verdict"],
                              "admission_reasons": _adm["reasons"],
                              "admission_fee_bps": _adm["metrics"].get("fee_bps")}
        except Exception as _adm_exc:  # 留痕失败绝不阻成交 (宁缺归因不误交易)
            rec["admission_error"] = str(_adm_exc)[:120]
        # 成交 → 写 open_positions + trade_records (盘中监控闭环; metadata.stop_loss 供 SL 判定)
        try:
            pos_id = _write_open_position(
                symbol, fqty, fpx, price, session, earn, chg, vol, buy_oid,
                position_usd=requested_position_usd, grade=grade, structure=structure,
                gate_mode=gate_mode, side=side,
                decision_id=ext_did, admission_meta=admission_meta,
            )
        except Exception as exc:
            rec["ok"] = False
            rec["error"] = f"position_db_write_failed:{exc}"
            _push(f"🚨 {_stage(session)}开仓已成交但本地建仓失败\n{symbol} 订单 {buy_oid}\n{exc}", level=_crit_level())
            _audit(rec)
            return rec
        rec["position_id"] = pos_id
        rec["ok"] = True
        _audit(rec)
        _push_filled(symbol, fqty, fpx, price, session, earn, chg, vol, rec, pos_id)
        return rec
    finally:
        tctx.close()



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours"])
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    rs = collect(args.session, args.dry)
    print(json.dumps(rs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
