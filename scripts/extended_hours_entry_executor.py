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

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

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
MIN_VOLUME = 200000        # 盘前量门槛
MIN_PRICE = float(os.getenv("EXT_ENTRY_MIN_PRICE", "5.0"))
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
        return bool(hub.push(_m(
            symbol=symbol, direction="BUY", qty=int(fqty), price=float(fpx),
            strategy="extended_hours_entry", trace_id=pos_id,
            sl=rec.get("initial_sl"), source_type="playbook",
            session=session,
            extra={
                "earnings_surprise_pct": earn.get("surprise_pct"),
                "ext_change_pct": chg,
                "ext_volume": vol,
                "stop_source": "规则引擎 initial_sl (-5%)",
                "sim_no_broker_stop": True,
                # 本链路按构造仅 SIM (trd_env 守卫在 _execute 内), 口径标 sim 与落库一致
                "trade_mode": "sim",
                "planned_entry": plan_px,
                "entry_dev_pct": ((float(fpx) - float(plan_px)) / float(plan_px) * 100)
                                 if plan_px else None,
                "extra_lines": [f"🆔 延长时段持仓: {pos_id}"],
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
                    pass
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
        "passes_current_gate": bool(chg >= ENTRY_CHANGE_PCT and vol >= MIN_VOLUME and px >= MIN_PRICE),
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
        pass
    head = ", ".join(f"{r['symbol']} ${r['price']:g}" for r in rejects[:8])
    more = f" (+{len(rejects) - 8})" if len(rejects) > 8 else ""
    print(f"[ext_entry] 价格闸(<${MIN_PRICE:g}) 拦下 {len(rejects)} 只: {head}{more}")


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
    ask_volume = _snapshot_number(row, "ask_vol")
    market_cap = _snapshot_number(row, "total_market_val")
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    spread_pct = (ask - bid) / ((bid + ask) / 2.0) * 100.0
    return {
        "bid": bid,
        "ask": ask,
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
):
    if not quote:
        return None, "quote_missing"
    ask = float(quote["ask"])
    spread_pct = float(quote["spread_pct"])
    spread_limit = max_spread_pct if max_spread_pct is not None else MAX_SPREAD_PCT
    depth_ratio = (
        min_ask_depth_ratio if min_ask_depth_ratio is not None else MIN_ASK_DEPTH_RATIO
    )
    if spread_pct > spread_limit:
        return None, "spread_too_wide"
    if decision_price > 0 and ask > decision_price * (1.0 + MAX_PRICE_DRIFT_PCT / 100.0):
        return None, "price_drift_too_high"
    if qty > 0 and float(quote["ask_volume"]) < qty * depth_ratio:
        return None, "insufficient_ask_depth"

    from app.execution.fee_model import estimate_round_trip
    fee = estimate_round_trip(qty, ask, ask, "LONG", trade_date)
    notional = qty * ask
    fee_pct = fee["total"] / notional * 100.0 if notional > 0 else float("inf")
    if fee_pct > MAX_ROUND_TRIP_FEE_PCT:
        return None, "round_trip_fee_too_high"

    return {
        "order_price": _rounded_order_price(ask),
        "bid": quote["bid"],
        "ask": ask,
        "ask_volume": float(quote["ask_volume"]),
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
        pass


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
        pass


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
                    or change_pct < ENTRY_CHANGE_PCT
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
        pass


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
                continue
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
            merged.append(it)

    if SOURCE_RANK_BOARD:
        _add(_scan_movers(session, qctx), "rank")
    extra = []
    if SOURCE_BEAT_RANK:
        extra += _beat_rank_symbols(qctx)
    if SOURCE_WATCHLIST:
        extra += _watchlist_symbols(qctx)
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
              f"财报超预期榜+自选股 {n_extra}) [开关 rank={SOURCE_RANK_BOARD} "
              f"beat={SOURCE_BEAT_RANK} watchlist={SOURCE_WATCHLIST}]")
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
        pass
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
    与旧版差异: 扫完所有 symbol+date 匹配行再定论 (旧版首行相位不符即拒, 不看后续行)。
    """
    rows, error = _earnings_calendar_rows()
    if error:
        return None, "calendar_source_error"
    today = _today_et()
    ticker = symbol.replace("US.", "").upper()
    saw_mismatch = False
    saw_unknown = False
    for row in rows:
        if str(row.get("symbol", "")).upper() != ticker:
            continue
        if str(row.get("reportDate", "")) != today:
            continue
        before, after = _parse_earnings_phase(row)
        if not before and not after:
            saw_unknown = True
            continue
        if before == (session == "premarket") and after == (session == "afterhours"):
            return row, None
        saw_mismatch = True
    if saw_mismatch:
        return None, "calendar_phase_mismatch"
    if saw_unknown:
        return None, "calendar_phase_unknown"
    return None, "calendar_missing"


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


def _earnings_surprise(symbol, session):
    """同日同相位 earnings beat + Finnhub surprise 新鲜度守卫 (P0-1)。

    返回 (earnings|None, sub_reason|None)。新增子原因:
      finnhub_client_unavailable / finnhub_no_data / finnhub_error — 源侧
      surprise_nonpositive — 最新 actual ≤ 0
      surprise_stale       — e[0].period 超龄 (今日 actual 尚未入库, 读到的是上一季)
    新鲜度守卫理由: 财报**刚发布**时 Finnhub 常未更新, e[0] 是上季记录;
    "上季 beat + 今日盘前涨 2% (好 guidance/误读)" 会透过旧闸, 今日实际 miss ⇒ 回落踩雷,
    与 9/17 首跑 4 笔"财报高开→回落"病灶同型。period 距今 [-7, 150] 天外一律拒。
    """
    event, calendar_reason = _earnings_calendar_event(symbol, session)
    if event is None:
        return None, calendar_reason
    try:
        from app.data.premarket_enrichment import _get_finnhub_client
        c = _get_finnhub_client()
        if c is None:
            return None, "finnhub_client_unavailable"
        tick = symbol.replace("US.", "")
        e = c.company_earnings(tick, limit=1)
        if not (e and isinstance(e, list)):
            return None, "finnhub_no_data"
        sp = e[0].get("surprisePercent")
        period = e[0].get("period")
        if sp is None or sp <= 0:
            return None, "surprise_nonpositive"
        age_days = _surprise_period_age_days(period)
        if age_days is None or age_days > EARNINGS_MAX_PERIOD_AGE_DAYS or age_days < -7:
            print(
                f"[ext_entry] {symbol} surprise STALE rejected: period={period!r} "
                f"age_days={age_days} limit={EARNINGS_MAX_PERIOD_AGE_DAYS} "
                "(今日 actual 疑似尚未入 Finnhub, fail-closed)"
            )
            return None, "surprise_stale"
        return {
            "surprise_pct": sp,
            "period": period,
            "period_age_days": age_days,
            "report_date": event.get("reportDate"),
            "report_phase": event.get("timeOfTheDay") or event.get("pub_type"),
        }, None
    except Exception as exc:
        print(f"[ext_entry] {symbol} finnhub earnings failed: {type(exc).__name__}: {exc}")
        return None, "finnhub_error"


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
        )
        body = " ".join(f"{k}={int(counts.get(k, 0))}" for k in keys)
        print(f"[ext_entry] 闸计数[{session}] 候选={total} {body}")
    except Exception:
        pass


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
                if change_pct < ENTRY_CHANGE_PCT or volume < MIN_VOLUME:
                    counts["momentum_volume"] = counts.get("momentum_volume", 0) + 1
                    continue
                earnings, earnings_reason = _earnings_surprise(symbol, session)
                if not earnings or not earnings.get("surprise_pct") or earnings["surprise_pct"] <= 0:
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
                })
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
                if change_pct < ENTRY_CHANGE_PCT or volume < MIN_VOLUME:
                    counts["momentum_volume"] = counts.get("momentum_volume", 0) + 1
                    continue
                earnings, earnings_reason = _earnings_surprise(symbol, session)
                if not earnings or not earnings.get("surprise_pct") or earnings["surprise_pct"] <= 0:
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
                        symbol, qty, order_price, session, change_pct, volume, earnings, dry,
                        position_usd=position_usd, grade=grade, structure=structure,
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
):
    """成交后写 open_positions + trade_records (盘中监控读 trade_records.metadata_json.stop_loss).
    返回 position_id. 幂等且不覆盖既有仓位."""
    import uuid
    ensure_test_symbol_not_in_production_db(symbol, DB)
    conn = sqlite3.connect(DB, timeout=30)
    try:
        now = _now_et().isoformat()
        pos_id = f"EXT-{uuid.uuid4().hex[:12]}"
        init_sl = round(entry_px * (1 + STOP_PCT / 100), 2)  # 规则引擎盘中兜底读这个
        meta = json.dumps({"session": session, "decision_px": decision_px,
                           "position_id": pos_id, "entry_order_id": buy_order_id,
                           "earnings_surprise_pct": earn.get("surprise_pct"),
                           "earnings_period": earn.get("period"),
                           "earnings_period_age_days": earn.get("period_age_days"),
                           "earnings_report_date": earn.get("report_date"),
                           "earnings_report_phase": earn.get("report_phase"),
                           "ext_change_pct": chg, "ext_volume": vol,
                           "ext_turnover_usd": float(entry_px or 0) * float(vol or 0),
                           "entry_grade": grade,
                           "entry_position_usd": float(position_usd or POSITION_USD),
                           "entry_structure": structure,
                           "cert_lock_pct": (
                               GRADE_A_CERT_LOCK_PCT if grade == "A"
                               else GRADE_B_CERT_LOCK_PCT
                           ),
                           "cohort": "B",  # 2026-09-17 用户裁: EXT 仓可隔夜 (cohort B + PLAYBOOK_SIM 默认允许, 盈利跳过 PRE_CLOSE_EXIT)
                           "stop_loss": init_sl,           # 盘中 monitor_playbook_sim_exits 读此键做 SL
                           "stop_source": "rule_engine_initial_sl"}, ensure_ascii=False)
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
            "VALUES (?, ?, 'LONG', ?, ?, ?, 'extended_hours_entry', 'extended_hours_entry', "
            " 'extended_hours_entry', 'extended_hours_entry', 'normal', 'simulate', 'AUTO', 1, 1, ?, ?, 'pending', "
            " ?, ?, ?)",
            (pos_id, symbol, qty, entry_px, now, now, meta, init_sl, init_sl, init_sl))
        # P0-3 完整闭环: 写 trade_records — 盘中退出监控只读此表 (source_type PLAYBOOK_SIM 未平仓)
        conn.execute(
            "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, entry_price, "
            " quantity, source_type, trade_mode, mode, owner_strategy, strategy_id, "
            " futu_order_id, metadata_json, created_at, exit_price, exit_time, config_version, "
            " entry_session) "
            "VALUES (?, ?, 'LONG', ?, ?, ?, 'PLAYBOOK_SIM', 'simulate', 'futu_simulate', "
            " 'extended_hours_entry', 'extended_hours_entry', ?, ?, ?, 0, '', 'v2', ?)",
            # config_version='v2': 与规范写入方 app/storage/trade_store.py 一致。该表 schema 是
            # DEFAULT 'v1'，本列不写会落默认值 ⇒ W1 冻结窗出现版本分裂假象 (2026-09-18 F7 修复)。
            (_today_et(), symbol, now, entry_px, qty, buy_order_id, meta, now, session))
        conn.commit()
        return pos_id
    finally:
        conn.close()


def _execute(
    symbol, qty, price, session, chg, vol, earn, dry,
    position_usd=None, grade="B", structure=None,
):
    """SIM 真下单: 买入→等成交→写 open_positions(含 initial_sl 规则引擎兜底). 用户裁方案1.
    SIM 无券商STOP, 止损靠 ①开盘 monitor_playbook_sim_exits 读 initial_sl ②盘前 tighten exit executor."""
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
    rec = {"symbol": symbol, "qty": qty, "price": price, "session": session,
           "change_pct": round(chg, 2), "volume": vol, "earnings": earn,
           "position_usd": requested_position_usd, "entry_grade": grade,
           "entry_structure": structure}
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
        ret, data = tctx.place_order(price=price, qty=qty, code=symbol, trd_side=TrdSide.BUY,
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
        # 成交 → 写 open_positions + trade_records (盘中监控闭环; metadata.stop_loss 供 SL 判定)
        try:
            pos_id = _write_open_position(
                symbol, fqty, fpx, price, session, earn, chg, vol, buy_oid,
                position_usd=requested_position_usd, grade=grade, structure=structure,
            )
        except Exception as exc:
            rec["ok"] = False
            rec["error"] = f"position_db_write_failed:{exc}"
            _push(f"🚨 {_stage(session)}开仓已成交但本地建仓失败\n{symbol} 订单 {buy_oid}\n{exc}", level=_crit_level())
            _audit(rec)
            return rec
        rec["position_id"] = pos_id
        rec["initial_sl"] = round(fpx * (1 + STOP_PCT / 100), 2)
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
