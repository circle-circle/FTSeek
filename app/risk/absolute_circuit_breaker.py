"""
绝对风控底线 — Daily/Hourly/Consecutive Loss 硬熔断

Task #1 (2026-06-17): 系统级安全基础设施
- 不受任何 feature flag 或开关控制
- 触发后: 禁止新开仓, 已有仓位止损/止盈不受影响
- 恢复: 人工确认 OR 下一交易日自动重置
- 审计日志持久化到 data/audit/circuit_breaker.jsonl (不可删除)
"""
# ── 日亏损防线登记 (2026-08-28 冲突显性化): "当日亏损"事实六套权威并存 ──
#   1. AccountCircuitBreaker  app/risk/account_circuit_breaker.py
#      净值 -1%/-2%/-3% 三档 (减半/停新仓/停新仓 — 全平从未接线)
#   2. AbsoluteCircuitBreaker app/risk/absolute_circuit_breaker.py
#      -$2000/日 + -$1000/时 + 连亏5笔 (类常量, 不受开关控制)
#   3. symbol_day_loss        app/risk/symbol_day_loss.py
#      -$500/标的/日 (出场日+净额, 主线/playbook 共用)
#   4. SymbolDailyLossGuard   app/risk/symbol_daily_loss_guard.py
#      净值0.5%/标的/日 + 连亏N笔 + 低胜率 (yaml risk.symbol_daily_loss_guard.*)
#   5. RiskCircuitBreaker     app/playbook/risk_circuit_breaker.py
#      总资金1% + 连亏3减半/连亏5停1日 (playbook 域; 周报告警组件 — "禁执行按钮+飞书"
#      从未接线, 全仓唯一调用方=周五周报 logger.warning, 仅记录不阻断; 2026-08-30 DEB-timezone 第4轮)
#   6. DrawdownController     app/risk/drawdown_control.py
#      日3% (risk.account.max_daily_drawdown, 持久化防重启归零; 仅主线平仓在喂)
# 六套独立查询/独立状态互不感知; 口径 % 与 $ 混杂, 触发先后交错, 合并/裁剪属
# 人工风险裁决 (每套有各自历史语义与恢复机制)。本登记保证读任一防线时看得见
# 全景; 已统一项: 1/3 出场日+净额 (2026-08-27/28), 其余各口径见各自头注。
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

from app.utils.timezone import now_et, _et_trade_date

logger = logging.getLogger(__name__)

# ── 审计日志路径 ──
_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "audit"
_AUDIT_FILE = _AUDIT_DIR / "circuit_breaker.jsonl"


def _write_audit(event: Dict[str, Any]) -> None:
    """追加写入审计日志 (不可删除, append-only)"""
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        event["timestamp"] = now_et().isoformat()
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"[AbsoluteCircuitBreaker] 审计日志写入失败: {e}")


class AbsoluteCircuitBreaker:
    """
    绝对风控底线熔断器 (单例)

    三重保护:
    1. DAILY_MAX_LOSS: 日内累计亏损 > $2000 → 暂停新仓
    2. HOURLY_MAX_LOSS: 小时累计亏损 > $1000 → 暂停 1 小时
    3. MAX_CONSECUTIVE_LOSSES: 连续亏损 5 笔 → 暂停该策略

    此类不受任何 feature flag 控制, 是最后防线。
    """

    _instance: Optional["AbsoluteCircuitBreaker"] = None
    _lock = threading.Lock()

    # ── 硬限参数 (不可通过配置覆盖) ──
    DAILY_MAX_LOSS = -2000.0       # 日亏 > $2K → 暂停新仓
    HOURLY_MAX_LOSS = -1000.0      # 时亏 > $1K → 暂停 1 小时
    MAX_CONSECUTIVE_LOSSES = 5     # 连亏 5 笔 → 暂停该策略

    # ── 解除与观察机制 (2026-09-05, 用户裁"按照建议执行") ──────────────────────
    # 原设计三处**单向锁存**, 当天亏损好转也不放行:
    #   ① 连亏暂停 → 只由"出现一笔盈利平仓"或次日重置解除。实测现行制度 48 个交易日中
    #      11 天触发 (单日最长 14 连亏), 其中 **4 天当日再没有盈利平仓 → 一路锁到收盘**,
    #      半天成交样本丢失 (数据采集模式的真实成本)。
    #   ② 时亏暂停 → 固定 1 小时定时解除, 暂停后连续盈利也不提前放行。
    #   ③ 无"观察期"概念 → 解除即全仓, 没有分级复权, 也没有滞回带。
    # 本批补的语义 (方向=既提高可用性又不放松风控):
    #   连亏触发 → 冷静期 30min → 到期进入**观察期** (接下来 3 笔已了结交易按 0.5× 名义)
    #     → 观察期内出现亏损即回到冷静期; 3 笔不再亏损 → 恢复全仓。
    #   时亏触发 → 最短冷静 15min 后, 若"触发小时口径"回升过 -$500 → 提前解除。
    #   日亏 -$2000 **刻意保持单向锁存不动**: 48 天 0 次触发 (最差日 -$793.60 = 线的 40%),
    #     它是真正的最后防线; 能被盘中反弹解除的最后防线不是最后防线。
    # 口径注意: 本类全部计数器只吃**已了结**净额 (record_trade_result 唯一喂数点
    #   app/playbook/pipeline.py:6437), 账面浮亏不进这些闸 — 浮亏侧由
    #   app/playbook/pipeline.py:3279 (净值 vs 当日首评基线) 与组合兜底 A9 覆盖。
    # 回滚: 把 CONSEC_PAUSE_SEC 设为 None 即恢复"锁到盈利平仓/次日"旧语义;
    #   或整体 git revert 本块 (测试 tests/risk/test_acb_recovery_probation.py 会先红)。
    CONSEC_PAUSE_SEC: Optional[float] = 1800.0   # 连亏冷静期 (秒); None=旧单向锁存
    CONSEC_PROBATION_TRADES = 3                  # 观察期笔数 (按已了结交易计)
    PROBATION_SIZE_MULT = 0.5                    # 观察期名义缩放 (与 REDUCE_50 同源)
    HOURLY_MIN_PAUSE_SEC = 900.0                 # 时亏最短冷静期 (提前解除的地板)
    HOURLY_EARLY_RELEASE_PNL = -500.0            # 时亏提前解除线 (触发小时口径回升到此值以上)

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
        self._state_lock = threading.Lock()

        # 日计数
        self._trade_date: str = _et_trade_date(now_et())
        self._daily_pnl: float = 0.0

        # 小时计数
        self._hourly_pnl: float = 0.0
        self._hour_start: datetime = now_et().replace(minute=0, second=0, microsecond=0)
        self._hourly_paused_until: Optional[datetime] = None

        # 连续亏损 (按 strategy_id)
        self._consecutive_losses: Dict[str, int] = {}
        self._strategy_paused: Dict[str, bool] = {}
        # 连亏冷静期到期时刻 + 观察期剩余笔数 (按 strategy_id)
        self._strategy_paused_until: Dict[str, datetime] = {}
        self._strategy_probation: Dict[str, int] = {}

        # 全局暂停 (日级别)
        self._daily_paused: bool = False

        # 时亏暂停的提前解除三件套: 起点 / 触发时的小时累计 / 暂停期间新增净额
        #   独立于 _hourly_pnl 的自然小时归零 — 10:15 触发的暂停不能因为 11:00 跨小时
        #   把计数器清零就被判成"已回升" (那等于把暂停砍短, 放松方向)。
        self._hourly_pause_started: Optional[datetime] = None
        self._hourly_pnl_at_pause: float = 0.0
        self._hourly_recovery_pnl: float = 0.0

        # 手动重置标记
        self._manual_override: bool = False

        logger.info(
            "[AbsoluteCircuitBreaker] 初始化完成 | "
            f"DAILY_MAX_LOSS=${abs(self.DAILY_MAX_LOSS)} | "
            f"HOURLY_MAX_LOSS=${abs(self.HOURLY_MAX_LOSS)} | "
            f"MAX_CONSECUTIVE_LOSSES={self.MAX_CONSECUTIVE_LOSSES}"
        )

        # P0 (2026-08-28 全链路复查): 启动即从台账重建当日计数.
        #   本类此前纯内存态, 而 ftseek-us.service 配 Restart=on-failure/RestartSec=30 →
        #   盘中一次崩溃自愈就把"当日已触发停新仓"清零且零告警: 自称"最后防线、不受任何
        #   feature flag 控制", 实际一次重启即可解除。
        #   2026-09-05 (DEB-20260905 §8.3): 生产单元已改 Restart=always +
        #   StartLimitBurst/IntervalSec 20/600 → 自愈重启更勤, 本重建更是唯一防线;
        #   且连亏暂停的重建带**有界**冷静期 (见 _restore_from_db: 重启不得把
        #   "锁到收盘"复活, 否则 §九 的解除语义在每次重启后被撤销)。
        self._restore_from_db()

    def _restore_from_db(self) -> None:
        """从 trade_records 重建当日 日亏/时亏/连亏 (出场日记账 + 净额口径).

        总体对齐: 实盘只有 playbook 一个喂数方
        (app/playbook/pipeline.py: record_trade_result("playbook", ...)), 故重建范围取
        source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') — 新增喂数策略时需同步扩该映射,
        否则恢复值会低于实时累计值。

        已知代价 (fail-closed, 有意): manual_reset 的 _manual_override 只存内存,
        故人工放行不跨重启 — 崩溃自愈后若当日亏损仍超线, 熔断会重新触发。此前重启
        连"触发"本身都一起清零 (即本修复要堵的洞), 所以净效果是变严不变松。
        人工放行需持久化时应另建 override 存储, 不是跳过重建。
        """
        try:
            import sqlite3
            from app.core import paths as _paths
            db_path = str(_paths.get_trade_records_db())
            today = _et_trade_date(now_et())
            hour_prefix = now_et().strftime("%Y-%m-%dT%H")
            _src_filter = "source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')"

            with sqlite3.connect(db_path) as conn:
                day_row = conn.execute(
                    f"SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0), COUNT(*) "
                    f"FROM trade_records WHERE substr(exit_time, 1, 10) = ? "
                    f"AND exit_price > 0 AND {_src_filter}",
                    (today,),
                ).fetchone()
                hour_row = conn.execute(
                    f"SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) "
                    f"FROM trade_records WHERE substr(exit_time, 1, 13) = ? "
                    f"AND exit_price > 0 AND {_src_filter}",
                    (hour_prefix,),
                ).fetchone()
                # 净额缺失行不参与连亏链 (避免一行 NULL 被当成"非亏"把连亏清零)
                pnl_rows = conn.execute(
                    f"SELECT COALESCE(pnl_net, realized_pnl) AS net FROM trade_records "
                    f"WHERE substr(exit_time, 1, 10) = ? AND exit_price > 0 "
                    f"AND COALESCE(pnl_net, realized_pnl) IS NOT NULL AND {_src_filter} "
                    f"ORDER BY exit_time DESC",
                    (today,),
                ).fetchall()

            self._trade_date = today
            self._daily_pnl = float(day_row[0] or 0.0)
            self._hourly_pnl = float(hour_row[0] or 0.0)

            consecutive = 0
            for _r in pnl_rows:
                if float(_r[0]) < 0:
                    consecutive += 1
                else:
                    break
            if consecutive:
                self._consecutive_losses["playbook"] = consecutive

            if self._daily_pnl <= self.DAILY_MAX_LOSS:
                self._daily_paused = True
            if self._hourly_pnl <= self.HOURLY_MAX_LOSS:
                self._hourly_paused_until = now_et() + timedelta(hours=1)
                # 重启后重建的暂停同样具备"提前解除"能力 (起点=重建时刻, 保守方向)
                self._hourly_pause_started = now_et()
                self._hourly_pnl_at_pause = self._hourly_pnl
                self._hourly_recovery_pnl = 0.0
            if consecutive >= self.MAX_CONSECUTIVE_LOSSES:
                # 重建的连亏暂停带**有界**冷静期 (旧行为=无到期时刻, 锁到盈利平仓或次日)。
                #   有界是保守与可用的折中: 重启不该把当日剩余时段整段锁死; 但到期后仍须
                #   走半仓观察期, 不是直接全放行。
                self._arm_consec_pause("playbook")

            logger.info(
                f"[AbsoluteCircuitBreaker·restore] ET 今日({today}) 已了结 "
                f"{int(day_row[1] or 0)} 笔 净额=${self._daily_pnl:.2f} "
                f"(本小时=${self._hourly_pnl:.2f}, 连亏={consecutive}) | "
                f"daily_paused={self._daily_paused} "
                f"strategy_paused={self._strategy_paused.get('playbook', False)}"
            )
        except Exception as e:
            # 重建失败不阻断启动 (保留零值), 但留 WARNING — 失明的防线不该静默
            logger.warning(
                f"[AbsoluteCircuitBreaker·restore] 当日计数重建失败, 保留零值: {e}"
            )

    def _check_new_day(self) -> None:
        """新交易日自动重置日计数"""
        today = _et_trade_date(now_et())
        if today != self._trade_date:
            logger.info(
                f"[AbsoluteCircuitBreaker] 新交易日 {today}, 重置日计数 "
                f"(前日PnL=${self._daily_pnl:.2f})"
            )
            _write_audit({
                "event": "daily_reset",
                "prev_date": self._trade_date,
                "new_date": today,
                "prev_daily_pnl": self._daily_pnl,
            })
            self._trade_date = today
            self._daily_pnl = 0.0
            self._daily_paused = False
            self._hourly_pnl = 0.0
            self._hourly_paused_until = None
            self._hourly_pause_started = None
            self._hourly_pnl_at_pause = 0.0
            self._hourly_recovery_pnl = 0.0
            self._consecutive_losses.clear()
            self._strategy_paused.clear()
            self._strategy_paused_until.clear()
            self._strategy_probation.clear()
            self._manual_override = False

    def _check_new_hour(self) -> None:
        """检查是否进入新的小时窗口"""
        current_hour = now_et().replace(minute=0, second=0, microsecond=0)
        if current_hour > self._hour_start:
            self._hour_start = current_hour
            self._hourly_pnl = 0.0
        # G-03 (2026-09-05 审计): 小时暂停到期必须清零, 否则小时熔断退化为"每日一次"。
        #   根因: record_trade_result 的触发条件含 `not self._hourly_paused_until`, 而该字段
        #   此前仅在 _check_new_day/manual_reset 清零; can_open_new_position 只做过期判断
        #   从不回写 → 首次触发后当日永久解除武装, 且暂停期满零告警 (防线静默失效)。
        #   另一入口: _restore_today_counters 启动重建亦会预置该字段 → 重启即消耗当日配额。
        #   语义边界: 按"到期"而非"跨小时"清零 — 10:15 触发的 1h 暂停须完整持续到 11:15;
        #   若在 11:00 跨小时即清零会把暂停砍短 15min (放松方向, 不可接受)。
        #   回滚: 删除本块 (等效恢复"每日一次"旧行为)。
        if self._hourly_paused_until and now_et() >= self._hourly_paused_until:
            _expired_until = self._hourly_paused_until
            self._hourly_paused_until = None
            logger.warning(
                f"[AbsoluteCircuitBreaker] 小时暂停已到期 "
                f"({_expired_until.strftime('%H:%M')} ET), 熔断重新武装 | "
                f"时累计=${self._hourly_pnl:.2f} / 限额=${self.HOURLY_MAX_LOSS}"
            )
            _write_audit({
                "event": "hourly_pause_expired",
                "expired_until": _expired_until.isoformat(),
                "hourly_pnl": self._hourly_pnl,
                "threshold": self.HOURLY_MAX_LOSS,
            })

    def _check_pause_expiries(self) -> None:
        """解除扫描 (必须在 _state_lock 内调): 连亏冷静期到期 → 观察期; 时亏提前恢复。

        与 _check_new_hour 的分工: 那里管"暂停到期即重新武装"(G-03), 这里管
        "到期后以什么姿态回来"——不是直接全放行, 而是进入半仓观察期 (分级复权)。
        """
        _now = now_et()

        # ① 时亏暂停的提前解除: 最短冷静期已过 且 触发小时口径回升过提前解除线。
        #   口径 = 触发瞬间的小时累计 + 暂停期间新增净额 (不受自然小时归零影响)。
        if self._hourly_paused_until and _now < self._hourly_paused_until:
            _elapsed = ((self._hourly_pause_started and
                         (_now - self._hourly_pause_started).total_seconds()) or 0.0)
            _recovered = self._hourly_pnl_at_pause + self._hourly_recovery_pnl
            if (_elapsed >= self.HOURLY_MIN_PAUSE_SEC and
                    _recovered > self.HOURLY_EARLY_RELEASE_PNL):
                _orig_until = self._hourly_paused_until
                self._hourly_paused_until = None
                self._hourly_pause_started = None
                self._hourly_pnl_at_pause = 0.0
                self._hourly_recovery_pnl = 0.0
                logger.warning(
                    f"[AbsoluteCircuitBreaker] 小时暂停提前解除 (亏损已回补): "
                    f"冷静 {_elapsed:.0f}s ≥ {self.HOURLY_MIN_PAUSE_SEC:.0f}s 且 "
                    f"小时口径 ${_recovered:.2f} > ${self.HOURLY_EARLY_RELEASE_PNL} "
                    f"(原定时至 {_orig_until.strftime('%H:%M')} ET) | 熔断重新武装"
                )
                _write_audit({
                    "event": "hourly_pause_early_release",
                    "elapsed_sec": round(_elapsed, 1),
                    "recovered_hourly_pnl": round(_recovered, 2),
                    "early_release_line": self.HOURLY_EARLY_RELEASE_PNL,
                    "original_until": _orig_until.isoformat(),
                })

        # ② 连亏冷静期到期 → 转入观察期 (半仓 N 笔), 不直接全放行。
        if self.CONSEC_PAUSE_SEC is None:
            return
        for _sid in list(self._strategy_paused.keys()):
            if not self._strategy_paused.get(_sid):
                continue
            _until = self._strategy_paused_until.get(_sid)
            if _until is None or _now < _until:
                continue
            self._strategy_paused[_sid] = False
            self._strategy_paused_until.pop(_sid, None)
            self._strategy_probation[_sid] = self.CONSEC_PROBATION_TRADES
            logger.warning(
                f"[AbsoluteCircuitBreaker] 连亏冷静期到期, {_sid} 进入观察期: "
                f"接下来 {self.CONSEC_PROBATION_TRADES} 笔按 "
                f"{self.PROBATION_SIZE_MULT:.0%} 名义 (出现亏损即回到冷静期) | "
                f"连亏计数={self._consecutive_losses.get(_sid, 0)}"
            )
            _write_audit({
                "event": "consecutive_pause_expired_probation",
                "strategy_id": _sid,
                "probation_trades": self.CONSEC_PROBATION_TRADES,
                "size_mult": self.PROBATION_SIZE_MULT,
                "consecutive_losses": self._consecutive_losses.get(_sid, 0),
            })

    def get_position_scale(self, strategy_id: str = "") -> float:
        """观察期名义缩放: 观察期内返回 PROBATION_SIZE_MULT, 否则 1.0。

        消费方: app/playbook/playbook_service.py 下单前 (与 OKR 熔断 REDUCE_50 档取 min)。
        只影响**新开仓名义**, 不影响已持仓的 SL/TP/退出 (退出快环永不受熔断影响)。
        """
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()
            self._check_pause_expiries()
            if strategy_id and self._strategy_probation.get(strategy_id, 0) > 0:
                return float(self.PROBATION_SIZE_MULT)
            return 1.0

    def _arm_consec_pause(self, strategy_id: str) -> None:
        """置连亏暂停 (必须在 _state_lock 内调)。

        CONSEC_PAUSE_SEC=None → 退回旧的单向锁存 (无到期时刻, 只由盈利平仓/次日/人工解除)。
        """
        self._strategy_paused[strategy_id] = True
        if self.CONSEC_PAUSE_SEC is None:
            self._strategy_paused_until.pop(strategy_id, None)
        else:
            self._strategy_paused_until[strategy_id] = now_et() + timedelta(
                seconds=self.CONSEC_PAUSE_SEC)

    def can_open_new_position(self, strategy_id: str = "") -> Tuple[bool, str]:
        """
        检查是否允许开新仓

        Returns:
            (allowed: bool, reason: str)
            - allowed=True: 可以开仓, reason=""
            - allowed=False: 被拦截, reason 说明原因
        """
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()
            self._check_pause_expiries()

            # 手动重置后放行
            if self._manual_override:
                return True, ""

            # 检查日级别暂停
            if self._daily_paused:
                reason = (
                    f"日亏损触发熔断: 累计${self._daily_pnl:.2f} "
                    f"超过限额${self.DAILY_MAX_LOSS}"
                )
                return False, reason

            # 检查小时级别暂停
            if self._hourly_paused_until and now_et() < self._hourly_paused_until:
                remaining = (self._hourly_paused_until - now_et()).total_seconds()
                reason = (
                    f"小时亏损触发熔断: 累计${self._hourly_pnl:.2f} "
                    f"超过限额${self.HOURLY_MAX_LOSS}, "
                    f"剩余{int(remaining)}秒恢复"
                )
                return False, reason

            # 检查策略连续亏损
            if strategy_id and self._strategy_paused.get(strategy_id, False):
                losses = self._consecutive_losses.get(strategy_id, 0)
                _until = self._strategy_paused_until.get(strategy_id)
                if _until is not None:
                    _left = int((_until - now_et()).total_seconds())
                    reason = (
                        f"策略 {strategy_id} 连亏{losses}笔, "
                        f"已达上限{self.MAX_CONSECUTIVE_LOSSES}, 冷静期剩余{_left}秒 "
                        f"(到期转 {self.CONSEC_PROBATION_TRADES} 笔半仓观察期)"
                    )
                else:
                    reason = (
                        f"策略 {strategy_id} 连亏{losses}笔, "
                        f"已达上限{self.MAX_CONSECUTIVE_LOSSES}, 暂停该策略 "
                        f"(恢复: 任一盈利平仓 / 次日重置 / 人工 manual_reset)"
                    )
                return False, reason

            return True, ""

    def record_trade_result(self, strategy_id: str, pnl: float, symbol: str = "") -> None:
        """
        记录一笔交易结果, 更新熔断状态

        Args:
            strategy_id: 策略ID
            pnl: 盈亏金额 (正=盈利, 负=亏损)
            symbol: 标的代码
        """
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()
            self._check_pause_expiries()

            # ── 触发检查 (提前置位: 观察期失败也是一次"触发", 需进审计) ──
            triggered = False

            # 累加日/小时PnL
            self._daily_pnl += pnl
            self._hourly_pnl += pnl
            # 暂停期间的新增净额单独累计 → _check_pause_expiries 据此判"亏损是否已回补"
            if self._hourly_paused_until is not None:
                self._hourly_recovery_pnl += pnl

            # 连续亏损计数
            _in_probation = bool(strategy_id) and self._strategy_probation.get(strategy_id, 0) > 0
            if pnl < 0:
                self._consecutive_losses[strategy_id] = (
                    self._consecutive_losses.get(strategy_id, 0) + 1
                )
                if _in_probation:
                    # 观察期内出现亏损 = 复权失败 → 回到冷静期 (下台阶, 不是直接全放行)
                    self._strategy_probation.pop(strategy_id, None)
                    self._arm_consec_pause(strategy_id)
                    triggered = True
                    logger.critical(
                        f"🚨 [AbsoluteCircuitBreaker] 观察期失败! {strategy_id} {symbol} "
                        f"本笔 ${pnl:.2f} → 回到冷静期 "
                        f"{int(self.CONSEC_PAUSE_SEC or 0)}s (半仓观察未通过)"
                    )
                    _write_audit({
                        "event": "probation_failed",
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "pnl": pnl,
                        "consecutive_losses": self._consecutive_losses.get(strategy_id, 0),
                        "pause_sec": self.CONSEC_PAUSE_SEC,
                    })
            else:
                # 盈利则重置连续亏损 + 解除冷静期 (这是"亏损好转即解除"的原有隐式路径, 保留)
                self._consecutive_losses[strategy_id] = 0
                if self._strategy_paused.get(strategy_id):
                    logger.warning(
                        f"[AbsoluteCircuitBreaker] {strategy_id} 盈利平仓 ${pnl:.2f} "
                        f"→ 连亏暂停提前解除 (观察期一并清零)"
                    )
                    _write_audit({
                        "event": "consecutive_pause_released_by_win",
                        "strategy_id": strategy_id, "symbol": symbol, "pnl": pnl,
                    })
                self._strategy_paused[strategy_id] = False
                self._strategy_paused_until.pop(strategy_id, None)
                if _in_probation:
                    _left = self._strategy_probation.get(strategy_id, 0) - 1
                    if _left > 0:
                        self._strategy_probation[strategy_id] = _left
                        logger.info(
                            f"[AbsoluteCircuitBreaker] {strategy_id} 观察期通过 {_left} 笔剩余 "
                            f"(仍按 {self.PROBATION_SIZE_MULT:.0%} 名义)"
                        )
                    else:
                        self._strategy_probation.pop(strategy_id, None)
                        logger.warning(
                            f"[AbsoluteCircuitBreaker] ✅ {strategy_id} 观察期通过 "
                            f"{self.CONSEC_PROBATION_TRADES} 笔无亏损 → 恢复全仓名义"
                        )
                        _write_audit({
                            "event": "probation_passed",
                            "strategy_id": strategy_id,
                            "probation_trades": self.CONSEC_PROBATION_TRADES,
                        })

            # 1. 日亏损检查
            if self._daily_pnl <= self.DAILY_MAX_LOSS and not self._daily_paused:
                self._daily_paused = True
                triggered = True
                logger.critical(
                    f"🚨 [AbsoluteCircuitBreaker] 日亏损触发! "
                    f"累计${self._daily_pnl:.2f} <= ${self.DAILY_MAX_LOSS} | "
                    f"暂停所有新仓 (恢复: 人工确认 OR 下一交易日)"
                )
                _write_audit({
                    "event": "daily_loss_triggered",
                    "daily_pnl": self._daily_pnl,
                    "threshold": self.DAILY_MAX_LOSS,
                    "last_trade": {"strategy_id": strategy_id, "pnl": pnl, "symbol": symbol},
                })

            # 2. 小时亏损检查
            if self._hourly_pnl <= self.HOURLY_MAX_LOSS and not self._hourly_paused_until:
                self._hourly_paused_until = now_et() + timedelta(hours=1)
                # 提前解除三件套: 起点 / 触发瞬间的小时累计 / 暂停期新增净额归零
                self._hourly_pause_started = now_et()
                self._hourly_pnl_at_pause = self._hourly_pnl
                self._hourly_recovery_pnl = 0.0
                triggered = True
                logger.critical(
                    f"🚨 [AbsoluteCircuitBreaker] 小时亏损触发! "
                    f"累计${self._hourly_pnl:.2f} <= ${self.HOURLY_MAX_LOSS} | "
                    f"暂停新仓1小时 (至 {self._hourly_paused_until.strftime('%H:%M')}) | "
                    f"提前解除: ≥{int(self.HOURLY_MIN_PAUSE_SEC)}s 后小时口径回升过 "
                    f"${self.HOURLY_EARLY_RELEASE_PNL}"
                )
                _write_audit({
                    "event": "hourly_loss_triggered",
                    "hourly_pnl": self._hourly_pnl,
                    "threshold": self.HOURLY_MAX_LOSS,
                    "paused_until": self._hourly_paused_until.isoformat(),
                    "min_pause_sec": self.HOURLY_MIN_PAUSE_SEC,
                    "early_release_line": self.HOURLY_EARLY_RELEASE_PNL,
                    "last_trade": {"strategy_id": strategy_id, "pnl": pnl, "symbol": symbol},
                })

            # 3. 连续亏损检查
            if (strategy_id and
                self._consecutive_losses.get(strategy_id, 0) >= self.MAX_CONSECUTIVE_LOSSES and
                not self._strategy_paused.get(strategy_id, False)):
                self._arm_consec_pause(strategy_id)
                triggered = True
                _until = self._strategy_paused_until.get(strategy_id)
                logger.critical(
                    f"🚨 [AbsoluteCircuitBreaker] 策略连亏触发! "
                    f"{strategy_id} 连亏{self._consecutive_losses[strategy_id]}笔 "
                    f">= {self.MAX_CONSECUTIVE_LOSSES} | 暂停该策略"
                    + (f" 冷静期至 {_until.strftime('%H:%M')} ET, 到期转 "
                       f"{self.CONSEC_PROBATION_TRADES} 笔半仓观察期" if _until else
                       " (恢复: 盈利平仓 / 次日 / 人工)")
                )
                _write_audit({
                    "event": "consecutive_loss_triggered",
                    "strategy_id": strategy_id,
                    "consecutive_losses": self._consecutive_losses[strategy_id],
                    "threshold": self.MAX_CONSECUTIVE_LOSSES,
                    "paused_until": _until.isoformat() if _until else None,
                    "probation_trades": self.CONSEC_PROBATION_TRADES,
                    "probation_size_mult": self.PROBATION_SIZE_MULT,
                    "last_trade": {"pnl": pnl, "symbol": symbol},
                })

            if not triggered:
                logger.debug(
                    f"[AbsoluteCircuitBreaker] 记录: {symbol} strategy={strategy_id} "
                    f"pnl=${pnl:.2f} | 日累计=${self._daily_pnl:.2f} "
                    f"时累计=${self._hourly_pnl:.2f} "
                    f"连亏={self._consecutive_losses.get(strategy_id, 0)}"
                )

    def manual_reset(self, reason: str = "") -> None:
        """
        人工确认重置 — 由运维/Dashboard 调用

        Args:
            reason: 重置原因 (审计留痕)
        """
        with self._state_lock:
            logger.warning(
                f"[AbsoluteCircuitBreaker] 手动重置! reason={reason} | "
                f"daily_pnl=${self._daily_pnl:.2f} daily_paused={self._daily_paused}"
            )
            _write_audit({
                "event": "manual_reset",
                "reason": reason,
                "state_before": {
                    "daily_pnl": self._daily_pnl,
                    "daily_paused": self._daily_paused,
                    "hourly_paused_until": (
                        self._hourly_paused_until.isoformat()
                        if self._hourly_paused_until else None
                    ),
                    "strategy_paused": dict(self._strategy_paused),
                },
            })
            self._manual_override = True
            self._daily_paused = False
            self._hourly_paused_until = None
            self._hourly_pause_started = None
            self._hourly_pnl_at_pause = 0.0
            self._hourly_recovery_pnl = 0.0
            self._strategy_paused.clear()
            self._strategy_paused_until.clear()
            self._strategy_probation.clear()

    def get_status(self) -> Dict[str, Any]:
        """获取熔断器完整状态"""
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()
            return {
                "trade_date": self._trade_date,
                "daily_pnl": round(self._daily_pnl, 2),
                "daily_paused": self._daily_paused,
                "hourly_pnl": round(self._hourly_pnl, 2),
                "hourly_paused_until": (
                    self._hourly_paused_until.isoformat()
                    if self._hourly_paused_until else None
                ),
                "consecutive_losses": dict(self._consecutive_losses),
                "strategy_paused": dict(self._strategy_paused),
                "strategy_paused_until": {
                    k: v.isoformat() for k, v in self._strategy_paused_until.items()
                },
                "strategy_probation": dict(self._strategy_probation),
                "probation_size_mult": self.PROBATION_SIZE_MULT,
                "hourly_pause_started": (
                    self._hourly_pause_started.isoformat()
                    if self._hourly_pause_started else None
                ),
                "hourly_recovery_pnl": round(self._hourly_recovery_pnl, 2),
                "manual_override": self._manual_override,
                "thresholds": {
                    "daily_max_loss": self.DAILY_MAX_LOSS,
                    "hourly_max_loss": self.HOURLY_MAX_LOSS,
                    "max_consecutive_losses": self.MAX_CONSECUTIVE_LOSSES,
                    "consec_pause_sec": self.CONSEC_PAUSE_SEC,
                    "consec_probation_trades": self.CONSEC_PROBATION_TRADES,
                    "hourly_min_pause_sec": self.HOURLY_MIN_PAUSE_SEC,
                    "hourly_early_release_pnl": self.HOURLY_EARLY_RELEASE_PNL,
                },
            }


# ── 全局单例访问 ──
def get_absolute_circuit_breaker() -> AbsoluteCircuitBreaker:
    """获取 AbsoluteCircuitBreaker 全局单例"""
    return AbsoluteCircuitBreaker()
