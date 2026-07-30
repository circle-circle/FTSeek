"""
Unified Regime Filter — 市场环境门禁 (ADR-007: 2026-06-16)

核心原则: 不预测方向，识别不对称性。
只在"对的环境"里使用"对的策略"。

三要素:
  1. SPY 趋势 (价格 vs MA200) — 大盘方向
  2. VIX 水平 — 恐慌程度
  3. 横截面宽度 (oversold count) — 市场健康度

输出 regime 状态 + 策略准入 + 仓位缩放。

Regime 状态机:
  STRONG_BULL  → 全力做多动量, IC 最高
  BULL         → 做多为主, 适度谨慎
  CAUTION      → 减少交易, 只做极端信号
  BEAR         → 空仓或只做极端反转
  CRISIS       → 全面停止

依据:
  - SPY > SMA200 时月度收益中位数 +0.8%, 之下 -0.3% (年化差距 ~13%)
  - VIX < 18: alpha 存在, 个股分化; VIX > 25: 相关性飙升, alpha 消失
  - 宽度恶化领先 SPY 见顶 2-6 周
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List

import sqlite3
import threading as _threading
from datetime import datetime as _datetime
from pathlib import Path as _Path

logger = logging.getLogger("ftseek.regime_filter")

# ═══════════════════════════════════════════════════════════════════
#  P2 (2026-06-17): Regime 切换历史记录
#  记录每次 Regime 变化到 SQLite, 供回测和准确性分析
# ═══════════════════════════════════════════════════════════════════

_REGIME_HISTORY_DB: Optional[_Path] = None
_REGIME_HISTORY_INITIALIZED = False
_REGIME_HISTORY_LOCK = _threading.Lock()


def _get_regime_db_path() -> _Path:
    global _REGIME_HISTORY_DB
    if _REGIME_HISTORY_DB is None:
        try:
            from app.core.paths import get_data_dir
            _REGIME_HISTORY_DB = get_data_dir() / "signal_snapshots.db"
        except Exception:
            _REGIME_HISTORY_DB = _Path(__file__).resolve().parent.parent.parent / "data" / "signal_snapshots.db"
    return _REGIME_HISTORY_DB


def _ensure_regime_table():
    """确保 regime_history 表存在"""
    global _REGIME_HISTORY_INITIALIZED
    if _REGIME_HISTORY_INITIALIZED:
        return
    with _REGIME_HISTORY_LOCK:
        if _REGIME_HISTORY_INITIALIZED:
            return
        try:
            db_path = _get_regime_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS regime_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,           -- ISO format
                    old_regime  TEXT,                     -- 旧 regime (NULL=首次)
                    new_regime  TEXT NOT NULL,            -- 新 regime
                    trigger     TEXT NOT NULL,            -- 'evaluate' / 'hysteresis' / 'rapid' / 'safety'
                    vix         REAL,
                    spy_price   REAL,
                    spy_ma200   REAL,
                    rationale   TEXT,
                    created_at  TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_ts ON regime_history(timestamp)")
            conn.commit()
            conn.close()
            _REGIME_HISTORY_INITIALIZED = True
        except Exception as e:
            logger.debug(f"[RegimeHistory] 表初始化失败 (不影响主流程): {e}")


def record_regime_change(
    old_regime: Optional[str],
    new_regime: str,
    trigger: str,
    vix: float = 0,
    spy_price: float = 0,
    spy_ma200: float = 0,
    rationale: str = "",
):
    """记录一次 Regime 切换 (非阻塞, 失败静默)"""
    try:
        _ensure_regime_table()
        db_path = _get_regime_db_path()
        conn = sqlite3.connect(str(db_path))
        now = _datetime.now().isoformat()
        conn.execute("""
            INSERT INTO regime_history
            (timestamp, old_regime, new_regime, trigger, vix, spy_price, spy_ma200, rationale, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, old_regime, new_regime, trigger, vix, spy_price, spy_ma200, rationale[:200], now))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[RegimeHistory] 记录失败 (不影响主流程): {e}")


class Regime(str, Enum):
    """市场环境枚举"""
    STRONG_BULL = "STRONG_BULL"     # 趋势强劲, 低波动, 宽度健康
    BULL = "BULL"                   # 上升趋势, 正常波动
    CAUTION = "CAUTION"             # 信号混乱, 减少交易
    BEAR = "BEAR"                   # 下跌趋势或高波动
    CRISIS = "CRISIS"               # 极端恐慌, 全面停止


@dataclass(frozen=True)
class RegimeVerdict:
    """Regime Filter 的判定结果"""
    regime: Regime
    allow_momentum: bool        # TECHNICAL_MULTI_FACTOR 是否放行
    allow_trend_first: bool     # trend_first 是否放行 (IC=-0.24, 通常禁止)
    allow_orb: bool             # ORB 是否放行
    allow_fast_lane: bool       # 作战计划快车道是否放行 (永远放行)
    allow_insider: bool         # 内部人集群买入是否放行 (基本面信号, 大多数环境放行)
    allow_earnings_revision: bool  # 分析师修正是否放行 (基本面信号)
    position_scale: float       # 仓位缩放系数 (0.0 ~ 1.0)
    min_strength: int           # 信号最低强度要求
    min_confidence: float       # 信号最低置信度要求
    allowed_directions: list    # 允许的方向 ["LONG"] / ["SHORT"] / ["LONG","SHORT"]
    rationale: str              # 人类可读的判定理由
    # 2026-07-29: 补市场数值三字段 (regime_history 告警 "vix/spy_price=0.0 数据未注入"
    #   根因 = 本类从未携带数值, record_regime_change 只能吃默认 0)。默认 0.0 兼容旧构造。
    vix: float = 0.0
    spy_price: float = 0.0
    spy_ma200: float = 0.0

    def blocks(self, strategy_id: str) -> bool:
        """判断是否阻止特定策略"""
        _map = {
            "TECHNICAL_MULTI_FACTOR": self.allow_momentum,
            "trend_first": self.allow_trend_first,
            "orb": self.allow_orb,
            "BATTLE_PLAN_FAST_PATH": self.allow_fast_lane,
            "INSIDER_CLUSTER_BUY": self.allow_insider,
            "EARNINGS_REVISION_MOMENTUM": self.allow_earnings_revision,
        }
        return not _map.get(strategy_id, True)

    def blocks_direction(self, direction: str) -> bool:
        """判断是否阻止特定方向"""
        dir_str = direction if isinstance(direction, str) else str(direction)
        return dir_str not in self.allowed_directions


def evaluate_regime(
    spy_price: float,
    spy_ma200: float,
    vix: float,
    oversold_count: int = 0,
    scanned_count: int = 0,
) -> RegimeVerdict:
    """
    评估当前市场 Regime。

    Args:
        spy_price: SPY 当前价格
        spy_ma200: SPY 200 日均线
        vix: VIX 恐慌指数
        oversold_count: 当前轮超卖标的数 (MarketBreadth)
        scanned_count: 当前轮扫描总数

    Returns:
        RegimeVerdict — 包含策略准入、仓位缩放、方向限制
    """
    # ── 数据有效性检查 ──
    has_spy = spy_price > 0 and spy_ma200 > 0
    spy_above_ma200 = has_spy and spy_price > spy_ma200
    spy_below_ma200 = has_spy and spy_price < spy_ma200

    # 宽度健康度: 超卖比例
    breadth_healthy = oversold_count <= 1 and scanned_count > 0
    breadth_warning = oversold_count >= 3
    breadth_panic = oversold_count >= 6

    # ── CRISIS: VIX >= 35, 极端恐慌, 全面停止 ──
    if vix >= 35:
        return RegimeVerdict(
            regime=Regime.CRISIS,
            allow_momentum=False,
            allow_trend_first=False,
            allow_orb=False,
            allow_fast_lane=True,
            allow_insider=False,
            allow_earnings_revision=False,
            position_scale=0.0,
            min_strength=99,
            min_confidence=0.99,
            allowed_directions=[],
            rationale=f"CRISIS: VIX={vix:.1f}>=35, 极端恐慌, 全面停止",
            vix=vix, spy_price=spy_price, spy_ma200=spy_ma200,
        )

    # ── BEAR: SPY < MA200 + VIX > 25, 或宽度恐慌 ──
    if (spy_below_ma200 and vix > 25) or breadth_panic:
        reasons = []
        if spy_below_ma200:
            reasons.append(f"SPY<MA200(${spy_price:.0f}<${spy_ma200:.0f})")
        if vix > 25:
            reasons.append(f"VIX={vix:.1f}>25")
        if breadth_panic:
            reasons.append(f"oversold={oversold_count}>=6")
        return RegimeVerdict(
            regime=Regime.BEAR,
            allow_momentum=False,
            allow_trend_first=False,
            allow_orb=False,
            allow_fast_lane=True,
            allow_insider=True,
            allow_earnings_revision=True,
            position_scale=0.25,
            min_strength=75,
            min_confidence=0.70,
            allowed_directions=["SHORT"],
            rationale=f"BEAR: {', '.join(reasons)}",
            vix=vix, spy_price=spy_price, spy_ma200=spy_ma200,
        )

    # ── CAUTION: SPY < MA200, 或 VIX 25-35, 或宽度警告 ──
    if spy_below_ma200 or vix >= 25 or breadth_warning:
        reasons = []
        if spy_below_ma200:
            reasons.append(f"SPY<MA200")
        if vix >= 25:
            reasons.append(f"VIX={vix:.1f}")
        if breadth_warning:
            reasons.append(f"oversold={oversold_count}")
        return RegimeVerdict(
            regime=Regime.CAUTION,
            allow_momentum=True,
            allow_trend_first=False,
            allow_orb=True,
            allow_fast_lane=True,
            allow_insider=True,
            allow_earnings_revision=True,
            position_scale=0.50,
            min_strength=55,
            min_confidence=0.55,
            allowed_directions=["LONG", "SHORT"],
            rationale=f"CAUTION: {', '.join(reasons)}",
            vix=vix, spy_price=spy_price, spy_ma200=spy_ma200,
        )

    # ── STRONG_BULL: SPY > MA200 + VIX < 18 + 宽度健康 ──
    if spy_above_ma200 and vix < 18 and breadth_healthy:
        return RegimeVerdict(
            regime=Regime.STRONG_BULL,
            allow_momentum=True,
            allow_trend_first=False,
            allow_orb=True,
            allow_fast_lane=True,
            allow_insider=True,
            allow_earnings_revision=True,
            position_scale=1.0,
            min_strength=30,
            min_confidence=0.35,
            allowed_directions=["LONG", "SHORT"],
            rationale=f"STRONG_BULL: SPY>MA200, VIX={vix:.1f}<18, oversold={oversold_count}",
            vix=vix, spy_price=spy_price, spy_ma200=spy_ma200,
        )

    # ── BULL: SPY > MA200 + VIX 18-25 (default positive state) ──
    if spy_above_ma200:
        return RegimeVerdict(
            regime=Regime.BULL,
            allow_momentum=True,
            allow_trend_first=False,
            allow_orb=True,
            allow_fast_lane=True,
            allow_insider=True,
            allow_earnings_revision=True,
            position_scale=0.80,
            min_strength=35,
            min_confidence=0.40,
            allowed_directions=["LONG", "SHORT"],
            rationale=f"BULL: SPY>MA200, VIX={vix:.1f}, oversold={oversold_count}",
            vix=vix, spy_price=spy_price, spy_ma200=spy_ma200,
        )

    # ── Fallback: 数据不足, 默认 CAUTION ──
    return RegimeVerdict(
        regime=Regime.CAUTION,
        allow_momentum=True,
        allow_trend_first=False,
        allow_orb=True,
        allow_fast_lane=True,
        allow_insider=True,
        allow_earnings_revision=True,
        position_scale=0.50,
        min_strength=55,
        min_confidence=0.55,
        allowed_directions=["LONG", "SHORT"],
        rationale=f"FALLBACK→CAUTION: spy={spy_price:.0f} ma200={spy_ma200:.0f} vix={vix:.1f}",
        vix=vix, spy_price=spy_price, spy_ma200=spy_ma200,
    )


def check_rapid_regime_change(
    vix_history: Optional[List[float]] = None,
    spy_history: Optional[List[float]] = None,
) -> Optional[str]:
    """
    快速 Regime 切换检测 (Task #2, 2026-06-17)
    嵌入 scan_loop 调用，不创建新线程。

    - VIX 5分钟涨幅 > 15% → 返回 "CAUTION"
    - SPY 5分钟跌幅 > 1%  → 返回 "BEAR"
    - 否则返回 None（无切换）

    Args:
        vix_history: 最近5分钟VIX采样序列, [oldest, ..., newest]
        spy_history: 最近5分钟SPY采样序列, [oldest, ..., newest]

    Returns:
        "CAUTION" / "BEAR" / None
    """
    # 检查VIX急涨
    if vix_history and len(vix_history) >= 2:
        vix_now = vix_history[-1]
        vix_5min_ago = vix_history[0]  # list按时间排序，[0]是5分钟前
        if vix_5min_ago > 0:
            vix_change_pct = (vix_now - vix_5min_ago) / vix_5min_ago * 100
            if vix_change_pct > 15:
                logger.warning(
                    f"[RegimeQuickSwitch] VIX spike: {vix_5min_ago:.1f}→{vix_now:.1f} "
                    f"(+{vix_change_pct:.1f}%) → CAUTION"
                )
                return "CAUTION"

    # 检查SPY急跌
    if spy_history and len(spy_history) >= 2:
        spy_now = spy_history[-1]
        spy_5min_ago = spy_history[0]
        if spy_5min_ago > 0:
            spy_change_pct = (spy_now - spy_5min_ago) / spy_5min_ago * 100
            if spy_change_pct < -1.0:
                logger.warning(
                    f"[RegimeQuickSwitch] SPY drop: {spy_5min_ago:.2f}→{spy_now:.2f} "
                    f"({spy_change_pct:.2f}%) → BEAR"
                )
                return "BEAR"

    return None


# ═══════════════════════════════════════════════════════════════════
#  P1 (2026-06-17): Regime 滞回机制 (Hysteresis)
#  防止 VIX/SPY 在边界值附近震荡导致策略准入频繁切换
# ═══════════════════════════════════════════════════════════════════


class RegimeHysteresis:
    """Regime 状态滞回器

    设计原则:
      - 进入更保守的 regime (severity 上升): 需连续 ENTRY_COUNT 次判定
      - 退出保守 regime (severity 下降): 需连续 EXIT_COUNT 次判定
      - CRISIS/BEAR: 立即切换 (安全优先, 不走滞回)
      - Rapid regime change 绕过滞回

    典型场景:
      VIX 在 24.5~25.5 之间震荡 → evaluate_regime 在 BULL/CAUTION 之间跳动
      滞回器要求连续 2 个 scan cycle 都判定为 CAUTION 才真正切换
    """

    # 滞回参数: 连续多少次一致才切换
    ENTRY_COUNT = 2   # 进入更保守 regime 需要连续 N 次
    EXIT_COUNT = 2    # 退出保守 regime 需要连续 N 次

    # 安全通道: 这些 regime 立即切换, 不走滞回
    _IMMEDIATE_REGIMES = {Regime.CRISIS, Regime.BEAR}

    _instance = None
    _lock = _threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._state_lock = _threading.Lock()
        self._stable_regime: Optional[Regime] = None
        self._candidate_regime: Optional[Regime] = None
        self._candidate_count: int = 0
        self._last_verdict: Optional[RegimeVerdict] = None

    # severity: 数字越大越保守
    _SEVERITY = {
        Regime.STRONG_BULL: 0,
        Regime.BULL: 1,
        Regime.CAUTION: 2,
        Regime.BEAR: 3,
        Regime.CRISIS: 4,
    }

    def get_stable_verdict(self, raw_verdict: RegimeVerdict) -> RegimeVerdict:
        """对原始 Regime 判定施加滞回滤波

        Args:
            raw_verdict: evaluate_regime() 的原始输出

        Returns:
            经过滞回平滑后的 RegimeVerdict (可能保持上一个稳定 regime)
        """
        if raw_verdict is None:
            return raw_verdict

        raw_regime = raw_verdict.regime

        with self._state_lock:
            # 首次调用: 直接采用
            if self._stable_regime is None:
                self._stable_regime = raw_regime
                self._last_verdict = raw_verdict
                # 2026-07-29: 无值首调不记 (重启期数据未就绪时 vix/spy 全 0 的 churn 行,
                #   今日 13:44 一秒 8 行实证); 有值才落 regime_history。
                if raw_verdict.vix > 0 or raw_verdict.spy_price > 0:
                    record_regime_change(
                        None, raw_regime.value, "evaluate",
                        vix=raw_verdict.vix, spy_price=raw_verdict.spy_price,
                        spy_ma200=raw_verdict.spy_ma200,
                        rationale=raw_verdict.rationale,
                    )
                return raw_verdict

            # 安全通道: CRISIS/BEAR 立即切换
            if raw_regime in self._IMMEDIATE_REGIMES:
                if raw_regime != self._stable_regime:
                    logger.warning(
                        f"[RegimeHysteresis] 安全通道: {self._stable_regime.value} → "
                        f"{raw_regime.value} (立即切换)"
                    )
                    record_regime_change(
                        self._stable_regime.value, raw_regime.value, "safety",
                        vix=raw_verdict.vix, spy_price=raw_verdict.spy_price,
                        spy_ma200=raw_verdict.spy_ma200,
                        rationale=raw_verdict.rationale,
                    )
                self._stable_regime = raw_regime
                self._candidate_regime = None
                self._candidate_count = 0
                self._last_verdict = raw_verdict
                return raw_verdict

            # 与当前稳定 regime 一致 → 重置候选计数
            if raw_regime == self._stable_regime:
                self._candidate_regime = None
                self._candidate_count = 0
                self._last_verdict = raw_verdict
                return raw_verdict

            # 判定结果与当前稳定 regime 不同
            raw_severity = self._SEVERITY.get(raw_regime, 0)
            stable_severity = self._SEVERITY.get(self._stable_regime, 0)
            is_escalation = raw_severity > stable_severity  # 升级 (更保守)
            required = self.ENTRY_COUNT if is_escalation else self.EXIT_COUNT

            # 更新候选计数
            if self._candidate_regime == raw_regime:
                self._candidate_count += 1
            else:
                self._candidate_regime = raw_regime
                self._candidate_count = 1

            # 达到阈值 → 切换
            if self._candidate_count >= required:
                old = self._stable_regime
                self._stable_regime = raw_regime
                self._candidate_regime = None
                self._candidate_count = 0
                self._last_verdict = raw_verdict
                logger.info(
                    f"[RegimeHysteresis] 切换确认: {old.value} → {raw_regime.value} "
                    f"(连续 {required} 次判定)"
                )
                record_regime_change(
                    old.value, raw_regime.value, "hysteresis",
                    vix=raw_verdict.vix, spy_price=raw_verdict.spy_price,
                    spy_ma200=raw_verdict.spy_ma200,
                    rationale=raw_verdict.rationale,
                )
                return raw_verdict

            # 未达到阈值 → 保持当前稳定 regime, 但更新 rationale
            logger.info(
                f"[RegimeHysteresis] 待确认: raw={raw_regime.value} "
                f"(stable={self._stable_regime.value}, "
                f"count={self._candidate_count}/{required})"
            )
            # 返回稳定 regime 的 verdict (用 last_verdict 的 regime 覆盖)
            return self._last_verdict or raw_verdict

    def reset(self):
        """重置状态 (测试用)"""
        with self._state_lock:
            self._stable_regime = None
            self._candidate_regime = None
            self._candidate_count = 0
            self._last_verdict = None


def get_regime_hysteresis() -> RegimeHysteresis:
    """获取 RegimeHysteresis 全局单例"""
    return RegimeHysteresis()


def evaluate_regime_with_hysteresis(
    spy_price: float,
    spy_ma200: float,
    vix: float,
    oversold_count: int = 0,
    scanned_count: int = 0,
) -> RegimeVerdict:
    """evaluate_regime 的滞回包装 — 供 signal_stage 使用

    对 evaluate_regime 的原始结果施加滞回滤波,
    防止 VIX/SPY 边界震荡导致 regime 频繁切换。

    安全通道: CRISIS/BEAR 立即切换 (不走滞回)。
    """
    raw = evaluate_regime(spy_price, spy_ma200, vix, oversold_count, scanned_count)
    return get_regime_hysteresis().get_stable_verdict(raw)
