"""
美股突发拦截器 (US Emergency Interceptor) - V3

P2 重构 (2026-04-19): 以富途 LV3 行情指标为主，新闻为辅。

职责:
1. [主] 实时监控持仓股的富途 LV3 行情指标 (价格闪崩、放量暴跌、连续下跌)。
2. [辅] 监控突发新闻 (Black Swan, Earnings Miss, SEC Investigation)。
3. 行情触发 → 切换 RISK_OFF → LLM 研判 → 清仓决策。
4. 支持自动恢复 (Auto Recovery)。

设计原则:
- 行情指标 (Futu LV3) 是第一优先 — 不依赖外部 API，延迟 <10ms
- 新闻 (Finnhub) 是第二优先 — 作为补充信息源
- 行情触发不依赖 LLM，直接切换 RISK_OFF 并告警
- LLM 仅用于辅助判断是否执行清仓 (HOLD vs EXIT)
"""

import os
import sys
import json
import time
import logging
import asyncio
import requests
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.us_pipeline.battle_plan_manager import USBattlePlanManager
from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
from app.notifier.dingtalk import DingTalkNotifier
from app.llm.unified_service import UnifiedLLMService

logger = logging.getLogger("ftseek.us_pipeline.emergency_interceptor")

# ============================================================================
#  行情指标拦截阈值 (Futu LV3 主拦截)
# ============================================================================
PRICE_CRASH_THRESHOLD = -0.05       # 单次检查跌幅 >5% → 紧急清仓候选
RAPID_DECLINE_THRESHOLD = -0.02     # 单次检查跌幅 >2% → 警告
CONSECUTIVE_DECLINE_COUNT = 3       # 连续下跌次数阈值
VOLUME_SPIKE_RATIO = 3.0            # 放量倍数 (vs 前5分钟均值)
MAX_ACCEPTABLE_GAP = -0.03          # 开盘跳空 >3% → 紧急

# ============================================================================
#  突发新闻关键词 (辅助拦截)
# ============================================================================
BLACK_SWAN_KEYWORDS = [
    # 监管/法律 — 高危，直接 RISK_OFF
    "SEC investigation", "SEC charges", "subpoena", "delist", "delisting",
    # P3 修复 (2026-05-19 22:35): "bankruptcy"/"chapter 11" 移至 LLM 确认区
    # 误触发: "3 Cash-Burning Stocks Walking a Fine Line" 匹配 bankruptcy → 非实际破产事件
    # 与 lawsuit/fraud 同类问题：泛用金融/法律词汇需走 LLM 二阶确认
    "receivership",
    # 财务/业绩 — P2 修复 (2026-05-07): 财报类移入 LLM 确认区
    "going concern", "audit qualification", "restatement",
    # 医药/生物科技 (FDA)
    "FDA rejection", "clinical trial failed", "not approvable", "CRL",
    # 其他 — 高危
    "CEO resign", "CFO resign",
    # 模糊关键词 (需 LLM 二阶确认)
    "missed earnings", "revenue miss", "guidance cut", "downgraded to sell",
    "entity list", "insider selling"
]

# ============================================================================
#  Kill Switch (P0-fix 2026-05-02: 手动紧急全仓平仓机制)
#  P1 修复 (2026-05-11): 添加自动恢复机制 + 最大 HALT 时长限制
# ============================================================================
_KILL_SWITCH_FLAG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'KILL_SWITCH.flag'
)
# 最大 HALT 时长 (分钟)，超过后自动解除
_MAX_HALT_DURATION_MINUTES = int(os.environ.get("FTSEEKS_MAX_HALT_MINUTES", "60"))

def _parse_kill_switch_timestamp(content: str) -> Optional[datetime]:
    """从 Kill Switch 文件内容解析激活时间戳"""
    if not content:
        return None
    try:
        # 格式: "2026-05-11T10:00:00 | reason"
        ts_str = content.split(" | ")[0]
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None

def check_kill_switch() -> bool:
    """检查 Kill Switch 是否被激活 (文件标记)
    P1 修复 (2026-05-11): 超过最大 HALT 时长自动解除，防止系统永久死亡
    P0-6 (2026-05-14): 用 atomic_read_with_mtime 防 phantom-empty (写到一半被读)
    """
    from app.utils.atomic_write import atomic_read_with_mtime
    content, _mtime = atomic_read_with_mtime(_KILL_SWITCH_FLAG)
    if content is None:
        # 文件不存在 OR 正在被改 (mtime 不一致) → 都视为未激活, 安全侧
        return False
    content = content.strip()
    if not content:
        # 空内容 — 视为未激活
        return False

    activated_at = _parse_kill_switch_timestamp(content)
    if activated_at:
        elapsed_minutes = (datetime.now() - activated_at).total_seconds() / 60.0
        if elapsed_minutes > _MAX_HALT_DURATION_MINUTES:
            logger.critical(
                f"🚨 KILL SWITCH 已超时 ({elapsed_minutes:.0f}min > {_MAX_HALT_DURATION_MINUTES}min), "
                f"自动解除"
            )
            deactivate_kill_switch()
            return False

    logger.critical(f"🚨 KILL SWITCH 已激活: {content}")
    return True

def activate_kill_switch(reason: str = "Manual trigger") -> bool:
    """激活 Kill Switch — 写入标记文件并触发紧急全仓平仓
    P0-6 (2026-05-14): 用 atomic_write_text 原子写, 防 phantom-empty
    """
    try:
        from app.utils.atomic_write import atomic_write_text
        from app.utils.critical_alerts import send_critical
        atomic_write_text(_KILL_SWITCH_FLAG, f"{datetime.now().isoformat()} | {reason}")
        logger.critical(f"🚨 KILL SWITCH 已激活: {reason}")
        # P0-3: 关键告警走 critical_notifier (5x 重试 + 多通道)
        send_critical(f"🚨 KILL SWITCH 已激活\n\nReason: {reason}\n\nTime: {datetime.now().isoformat()}", async_mode=True)
        return True
    except Exception as e:
        logger.error(f"Kill Switch 激活失败: {e}")
        return False

def deactivate_kill_switch() -> bool:
    """解除 Kill Switch — 删除标记文件"""
    try:
        if os.path.exists(_KILL_SWITCH_FLAG):
            os.remove(_KILL_SWITCH_FLAG)
        logger.info("🟢 KILL SWITCH 已解除")
        return True
    except Exception as e:
        logger.error(f"Kill Switch 解除失败: {e}")
        return False



async def _safe_get_positions(scheduler):
    """Async-safe position retrieval with retry for SQLite locks
    P0-4 修复 2026-04-26: 改为 async def，避免在事件循环中使用 time.sleep 阻塞全部协程
    """
    import asyncio
    for _ in range(3):
        try:
            return scheduler.position_store.all()
        except Exception as e:
            if "locked" in str(e).lower() or "database" in str(e).lower():
                await asyncio.sleep(0.5)
            else:
                raise
    return []

class USEmergencyInterceptor:
    """美股突发拦截器 (行情指标为主 + 新闻为辅)"""

    def __init__(self, scheduler, execution_engine):
        self.scheduler = scheduler
        self.execution_engine = execution_engine
        self.state_machine = RiskStateMachine()
        self.notifier = DingTalkNotifier()

        # 配置
        self.check_interval = 30  # 每 30 秒检查一次 (行情监控需要更高频率)
        self.finnhub_api_key = os.environ.get("FINNHUB_API_KEY")

        # 记录已处理的新闻 ID (防抖辅助)
        self.processed_news_ids = set()
        # P1 修复 (2026-05-13): 关键词冷却期 — 防止同一关键词在恢复后重复触发 RISK_OFF
        self._keyword_cooldowns: Dict[str, float] = {}  # keyword -> timestamp
        self._keyword_cooldown_seconds = 3600  # 1 小时冷却
        # P0 (2026-06-05): 持久化去重 — 防止进程重启后同一新闻事件反复触发 RISK_OFF
        self._reason_cooldown_seconds = 7200  # 同一 reason 2 小时内不重复触发

        # P2 重构: 行情指标追踪 (每个持仓股票的价格/量历史)
        self._price_history: Dict[str, List[float]] = {}
        self._volume_history: Dict[str, List[float]] = {}
        self._history_length = 10  # 保留最近 10 个检查点 (30s * 10 = 5 分钟窗口)

        # 已触发的紧急退出标记 (防止重复执行)
        self._exited_symbols: Dict[str, float] = {}  # symbol -> exit_timestamp
        self._exit_cooldown = 3600  # 1 小时内不重复退出同一标的

        logger.info(
            f"🛡️ 美股突发拦截器 (V3) 已初始化 | "
            f"行情主 + 新闻辅 | 间隔={self.check_interval}s | "
            f"闪崩阈值={PRICE_CRASH_THRESHOLD:.0%} | "
            f"放量倍数={VOLUME_SPIKE_RATIO}x"
        )

    async def start_monitoring(self):
        """启动后台监控循环 — 行情指标为主 + 新闻为辅"""
        logger.info("🚀 启动突发拦截监控循环 (富途 LV3 行情主 + 新闻辅)...")
        news_cycle_counter = 0  # 新闻检查计数器 (每 N 次行情检查执行 1 次新闻)

        while True:
            try:
                # 顶层守卫: 非交易时段直接休眠, 避免 30s 空转
                # _check_market_indicators 内部也有守卫, 但这里拦截更早,
                # 连 auto_recovery + news_cycle 都跳过
                try:
                    from app.core.scheduler_triggers import get_market_phase, MarketPhase
                    _phase = get_market_phase()
                    if _phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                        await asyncio.sleep(300)
                        continue
                except Exception:
                    pass  # 降级到原有逻辑

                # 1. 自动恢复检查
                self.state_machine.auto_recovery()

                # ── 主循环: 富途 LV3 行情指标监控 ──
                await self._check_market_indicators()

                # ── 副循环: 新闻监控 (每 4 次行情检查 = 每 2 分钟执行一次) ──
                news_cycle_counter += 1
                if news_cycle_counter >= 4:
                    news_cycle_counter = 0
                    await self._check_news_cycle()

                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"❌ 监控循环异常: {e}", exc_info=True)
                await asyncio.sleep(10)

    # ====================================================================
    #  主拦截: 富途 LV3 行情指标监控
    # ====================================================================

    async def _check_market_indicators(self):
        """
        核心拦截逻辑 — 基于富途 LV3 实时行情数据。

        检测维度:
        1. 价格闪崩: 单次检查跌幅 >5%
        2. 连续下跌: 连续 3+ 次检查下跌
        3. 放量暴跌: 成交量 >3 倍均值 + 价格下跌
        4. 开盘跳空: 开盘价 vs 昨收跌幅 >3%
        """
        # P0 修复 (2026-04-25): 统一交易日历门禁
        # 周末/深夜完全静默，不请求任何行情数据
        try:
            from app.core.scheduler_triggers import get_market_phase, MarketPhase
            phase = get_market_phase()
            if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                return
        except Exception:
            pass  # 降级到原有逻辑

        # 非美股交易时段: 无实时行情，跳过监控 (2026-04-21 修复)
        if hasattr(self.scheduler, 'data_router') and self.scheduler.data_router:
            futu = getattr(self.scheduler.data_router, 'futu', None)
            if futu and not futu._is_us_market_open():
                return

        positions = await _safe_get_positions(self.scheduler)
        if not positions:
            return

        for pos in positions:
            symbol = pos.symbol
            try:
                # 获取实时报价 (优先 Futu LV3)
                quote = await self._get_realtime_quote(symbol)
                if not quote:
                    continue

                price = quote.get("price", 0)
                volume = quote.get("volume", 0)
                prev_close = quote.get("prev_close", 0)
                open_price = quote.get("open", 0)

                if price <= 0 or prev_close <= 0:
                    continue

                # 记录历史
                self._record_history(symbol, price, volume)

                # 幂等检查: 1 小时内已退出的标的跳过
                if self._is_in_exit_cooldown(symbol):
                    continue

                # 检查 1: 开盘跳空 (仅首次检查时有效)
                if open_price > 0 and len(self._price_history.get(symbol, [])) <= 2:
                    gap = (open_price - prev_close) / prev_close
                    if gap < MAX_ACCEPTABLE_GAP:
                        await self._trigger_market_emergency(
                            symbol, "OPEN_GAP",
                            f"开盘跳空 {gap:+.1%} (阈值 {MAX_ACCEPTABLE_GAP:.0%})",
                            price, prev_close
                        )
                        continue

                # 检查 2: 价格闪崩 (单次跌幅)
                prev_price = self._get_prev_price(symbol)
                if prev_price and prev_price > 0:
                    change_pct = (price - prev_price) / prev_price

                    if change_pct <= PRICE_CRASH_THRESHOLD:
                        await self._trigger_market_emergency(
                            symbol, "PRICE_CRASH",
                            f"价格闪崩 {change_pct:+.1%} (阈值 {PRICE_CRASH_THRESHOLD:.0%})",
                            price, prev_price
                        )
                        # RFC-Catalyst (2026-05-27): 反向催化 — 行情异动触发新闻搜索+快速辩论
                        try:
                            from app.catalyst.reverse_catalyst import on_price_anomaly
                            on_price_anomaly(symbol, change_pct, scheduler=getattr(self, '_scheduler', None))
                        except Exception:
                            pass  # 不影响主流程
                        continue

                    # 检查 3: 连续下跌
                    if self._check_consecutive_decline(symbol):
                        oldest = self._get_oldest_price(symbol)
                        if oldest and oldest > 0:
                            total_decline = (price - oldest) / oldest
                            await self._trigger_market_emergency(
                                symbol, "CONSECUTIVE_DECLINE",
                                f"连续 {CONSECUTIVE_DECLINE_COUNT} 次下跌, 累计 {total_decline:+.1%}",
                                price, oldest
                            )
                        continue

                    # 检查 4: 放量暴跌 (价格下跌 + 成交量异常)
                    if change_pct <= RAPID_DECLINE_THRESHOLD:
                        avg_volume = self._get_avg_volume(symbol)
                        if avg_volume > 0 and volume > avg_volume * VOLUME_SPIKE_RATIO:
                            vol_ratio = volume / avg_volume
                            await self._trigger_market_emergency(
                                symbol, "VOLUME_CRASH",
                                f"放量暴跌 {change_pct:+.1%} + 成交量 {vol_ratio:.1f}x 均值",
                                price, prev_price
                            )
                            continue

                    # 普通预警 (不触发清仓)
                    if change_pct <= RAPID_DECLINE_THRESHOLD:
                        logger.warning(
                            f"⚠️ [行情预警] {symbol}: 价格下跌 {change_pct:+.1%} "
                            f"(预警线 {RAPID_DECLINE_THRESHOLD:.0%})"
                        )

            except Exception as e:
                logger.debug(f"[行情拦截] {symbol} 检查异常: {e}")

    # ── 行情指标辅助方法 ──────────────────────────────────────────────

    def _record_history(self, symbol: str, price: float, volume: float):
        """记录价格/量历史"""
        if symbol not in self._price_history:
            self._price_history[symbol] = []
        if symbol not in self._volume_history:
            self._volume_history[symbol] = []

        self._price_history[symbol].append(price)
        self._volume_history[symbol].append(volume)

        # 限制历史记录长度
        if len(self._price_history[symbol]) > self._history_length:
            self._price_history[symbol] = self._price_history[symbol][-self._history_length:]
        if len(self._volume_history[symbol]) > self._history_length:
            self._volume_history[symbol] = self._volume_history[symbol][-self._history_length:]

    def _get_prev_price(self, symbol: str) -> Optional[float]:
        """获取上一次检查的价格"""
        history = self._price_history.get(symbol, [])
        return history[-2] if len(history) >= 2 else None

    def _get_oldest_price(self, symbol: str) -> Optional[float]:
        """获取最早记录的价格"""
        history = self._price_history.get(symbol, [])
        return history[0] if history else None

    def _get_avg_volume(self, symbol: str) -> float:
        """获取平均成交量 (排除最近一次)"""
        history = self._volume_history.get(symbol, [])
        if len(history) < 2:
            return 0
        return sum(history[:-1]) / (len(history) - 1)

    def _check_consecutive_decline(self, symbol: str) -> bool:
        """检查是否连续 N 次下跌"""
        history = self._price_history.get(symbol, [])
        if len(history) < CONSECUTIVE_DECLINE_COUNT + 1:
            return False

        recent = history[-(CONSECUTIVE_DECLINE_COUNT + 1):]
        for i in range(1, len(recent)):
            if recent[i] >= recent[i - 1]:
                return False
        return True

    def _is_in_exit_cooldown(self, symbol: str) -> bool:
        """检查是否在退出冷却期"""
        last_exit = self._exited_symbols.get(symbol, 0)
        return (time.time() - last_exit) < self._exit_cooldown

    def _is_same_risk_off_recently(self, reason: str) -> bool:
        """P0 (2026-06-05): 检查 risk_audit.json 中是否已有相同 reason 的 RISK_OFF
        
        解决进程重启后内存冷却期丢失、同一新闻反复触发 RISK_OFF 的问题。
        扫描最近 2 小时内的审计记录，若 reason 匹配（子串包含）则返回 True。
        """
        import os, json as _json
        from datetime import datetime as _dt
        audit_path = getattr(self.state_machine, '_audit_path', '')
        if not audit_path or not os.path.exists(audit_path):
            return False
        try:
            with open(audit_path, 'r') as f:
                logs = _json.load(f)
            now = _dt.now()
            # 提取 reason 中的核心标的 (e.g. "US.AVGO")
            import re
            symbols_in_reason = set(re.findall(r'US\.\w+', reason))
            for rec in logs:
                if rec.get("new_state") != "RISK_OFF":
                    continue
                rec_time = _dt.fromisoformat(rec.get("time", "2000-01-01"))
                if (now - rec_time).total_seconds() > self._reason_cooldown_seconds:
                    continue
                # 检查是否为同一事件: reason 包含相同标的
                rec_symbols = set(re.findall(r'US\.\w+', rec.get("reason", "")))
                if symbols_in_reason and symbols_in_reason == rec_symbols:
                    logger.info(
                        f"[紧急拦截] 持久化去重命中: {symbols_in_reason} "
                        f"在 {(now - rec_time).total_seconds()/60:.0f}min 前已触发，跳过"
                    )
                    return True
            return False
        except Exception:
            return False

    async def _get_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """
        获取实时报价 (Futu LV3 → Finnhub 降级)

        优先级:
        1. scheduler.data_router (Futu LV3, 延迟 <10ms)
        2. scheduler.yahoo (降级数据源)
        """
        try:
            # 优先: 通过 scheduler 的 data_router (Futu LV3)
            if hasattr(self.scheduler, 'data_router') and self.scheduler.data_router:
                quote = self.scheduler.data_router.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    return {
                        "price": quote.get("price", 0),
                        "volume": quote.get("volume", 0),
                        "prev_close": quote.get("prev_close_price", quote.get("last_close", 0)),
                        "open": quote.get("open", 0),
                    }

            # 降级: 通过 scheduler.yahoo 获取
            if hasattr(self.scheduler, 'yahoo') and self.scheduler.yahoo:
                quote = self.scheduler.market_data.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    return {
                        "price": quote.get("price", 0),
                        "volume": quote.get("volume", 0),
                        "prev_close": quote.get("previous_close", 0),
                        "open": quote.get("open", 0),
                    }
        except Exception as e:
            logger.debug(f"[行情获取] {symbol} Futu 获取失败: {e}")

        return None

    async def _trigger_market_emergency(
        self, symbol: str, trigger_type: str, reason: str,
        current_price: float, ref_price: float
    ):
        """
        行情触发紧急拦截

        流程: RISK_OFF → 告警 → LLM 研判 → 清仓决策
        """
        logger.critical(f"🔴 [行情拦截] {symbol} {trigger_type}: {reason}")

        # 1. 切换 RISK_OFF 状态 (先关门)
        self.state_machine.set_state(
            RiskState.RISK_OFF,
            reason=f"{trigger_type}: {reason} (价格 ${current_price:.2f})",
            source="MarketIndicator"
        )

        # 2. 发送告警
        self._send_market_alert(symbol, trigger_type, reason, current_price, ref_price)

        # 3. LLM 快速研判 (决定是否清仓)
        decision = await self._llm_market_judgment(symbol, trigger_type, reason, current_price)

        if decision == "EXIT":
            # 4. 执行清仓 (带幂等检查)
            position = await self._get_position_by_symbol(symbol)
            if position:
                await self._execute_emergency_exit(
                    position,
                    f"行情指标 {trigger_type}: {reason}"
                )
                self._exited_symbols[symbol] = time.time()
        else:
            logger.info(f"[行情拦截] {symbol} LLM 研判 HOLD, 持续监控")

    # ====================================================================
    #  副拦截: 新闻监控
    # ====================================================================

    async def _check_news_cycle(self):
        """执行一轮新闻检查 (辅助拦截)
        
        P0 修复 (2026-05-01): 对非高危关键词添加 LLM 二阶确认，防止宏观/地缘新闻假阳性
        导致频繁切换 RISK_OFF。
        """
        if not self.finnhub_api_key:
            return

        news_items = await self._fetch_finnhub_news()
        if not news_items:
            return

        positions = await _safe_get_positions(self.scheduler)
        if not positions:
            return

        for item in news_items:
            news_id = item.get('id', item.get('headline', ''))
            if news_id in self.processed_news_ids:
                continue

            headline = item.get('headline', '').lower()
            summary = item.get('summary', '').lower()
            text = f"{headline} {summary}"

            import re
            matched_keywords = [kw for kw in BLACK_SWAN_KEYWORDS
                                if re.search(r'\b' + re.escape(kw.lower()) + r'\b', text)]

            if not matched_keywords:
                continue

            # P1 修复 (2026-05-13): 关键词冷却期 — 同一关键词 1 小时内不重复触发
            now = time.time()
            active_cooldowns = []
            for kw in matched_keywords:
                kw_lower = kw.lower()
                if kw_lower in self._keyword_cooldowns:
                    age = now - self._keyword_cooldowns[kw_lower]
                    if age < self._keyword_cooldown_seconds:
                        active_cooldowns.append(kw)
            if active_cooldowns == matched_keywords:
                # 所有匹配关键词都在冷却期内，跳过
                logger.info(f"[紧急拦截] 关键词 {active_cooldowns} 在冷却期内，跳过")
                self.processed_news_ids.add(news_id)
                continue

            self.processed_news_ids.add(news_id)

            relevant_positions = []
            for pos in positions:
                code = pos.symbol.replace("US.", "")
                if code.lower() in text or code in item.get('related', ''):
                    relevant_positions.append(pos)

            if not relevant_positions:
                continue

            # ── P0 修复: 区分高危 vs 模糊关键词 ──
            # 高危关键词: 直接针对公司生存/合法性的事件，无需 LLM 确认
            # P1 修复 (2026-05-13): 移除 "lawsuit" — 常规法律纠纷频繁误触发 RISK_OFF
            # (NFLX "lawsuit" 导致 3 次 RISK_OFF 重入, 需手动恢复)
            # "lawsuit" 降至 ambiguous 区走 LLM 二阶确认
            # P1 修复 (2026-05-14): 移除 "fraud" — 监管新闻中的泛用词频繁误触发 RISK_OFF
            # (CMS "Fraud Crackdown" 导致 RISK_OFF 误触发，LLM 判定为 HOLD 但已先触发)
            # 与 "lawsuit" 同类问题：泛用法律/监管词汇需走 LLM 二阶确认
            high_risk_keywords = ["sec investigation", "sec charges", "subpoena", "delist", "delisting",
                                  "receivership",
                                  "fda rejection", "clinical trial failed", "not approvable", "crl",
                                  "ceo resign", "cfo resign"]
            # P3 修复 (2026-05-19 22:35): "bankruptcy"/"chapter 11" 移至 LLM 确认区
            # 泛用金融词汇（现金消耗/破产风险讨论）不应直接触发 RISK_OFF
            # 与 lawsuit/fraud 同类问题：需走 LLM 二阶确认
            ambiguous_keywords = ["missed earnings", "revenue miss", "guidance cut", "downgraded to sell",
                                  "insider selling", "entity list", "lawsuit", "fraud",
                                  "going concern", "audit qualification", "restatement",
                                  "bankruptcy", "chapter 11"]
            is_high_risk = any(kw.lower() in [k.lower() for k in matched_keywords]
                              for kw in high_risk_keywords)

            symbols_str = ", ".join([p.symbol for p in relevant_positions])

            if is_high_risk:
                # 高危: 立即 RISK_OFF (保持原有逻辑)
                logger.warning(f"🚨 [新闻拦截] 高危突发: {item['headline']}")
                reason = f"Black Swan: {', '.join(matched_keywords)} on {symbols_str}"
                # P0 (2026-06-05): 持久化去重 — 同一事件不重复触发
                if self._is_same_risk_off_recently(reason):
                    continue
                self.state_machine.set_state(RiskState.RISK_OFF, reason=reason, source="NewsInterceptor")
                # 记录关键词冷却期
                for kw in matched_keywords:
                    self._keyword_cooldowns[kw.lower()] = time.time()
                self._send_alert(item, matched_keywords, relevant_positions)

                # LLM 研判清仓
                for pos in relevant_positions:
                    current_positions = await _safe_get_positions(self.scheduler)
                    if not any(p.symbol == pos.symbol for p in current_positions):
                        logger.info(f"✅ [幂等] {pos.symbol} 已无持仓，跳过清仓")
                        continue
                    decision = await self._llm_risk_judgment(item, pos)
                    if decision == "EXIT":
                        logger.critical(f"🔥 [新闻拦截] LLM 清仓: {pos.symbol}")
                        await self._execute_emergency_exit(pos, item['headline'])
            else:
                # P0 修复: 模糊关键词（宏观/地缘类）→ LLM 二阶确认后再决定是否 RISK_OFF
                logger.info(f"🔍 [新闻拦截] 模糊关键词匹配: {matched_keywords} → LLM 二阶确认中...")
                llm_verdict = await self._llm_news_verdict(item, relevant_positions)
                
                if llm_verdict == "RISK_OFF":
                    logger.warning(f"🚨 [新闻拦截] LLM 确认为黑天鹅: {item['headline']}")
                    reason = f"Black Swan (LLM confirmed): {', '.join(matched_keywords)} on {symbols_str}"
                    # P0 (2026-06-05): 持久化去重 — 同一事件不重复触发
                    if self._is_same_risk_off_recently(reason):
                        continue
                    self.state_machine.set_state(RiskState.RISK_OFF, reason=reason, source="NewsInterceptor")
                    # 记录关键词冷却期
                    for kw in matched_keywords:
                        self._keyword_cooldowns[kw.lower()] = time.time()
                    self._send_alert(item, matched_keywords, relevant_positions)
                else:
                    logger.info(f"✅ [新闻拦截] LLM 判定非黑天鹅，忽略: {item['headline']}")

    # ── 新闻获取 ──────────────────────────────────────────────────────

    async def _fetch_finnhub_news(self) -> List[Dict]:
        """从 Finnhub 获取持仓相关新闻 (辅助数据源)"""
        if not self.finnhub_api_key:
            return []

        positions = await _safe_get_positions(self.scheduler)
        if not positions:
            return []

        all_news = []
        seen_ids = set()

        for pos in positions:
            symbol = pos.symbol.replace("US.", "")
            try:
                today = datetime.utcnow().strftime("%Y-%m-%d")
                tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")

                url = "https://finnhub.io/api/v1/company-news"
                params = {
                    "symbol": symbol,
                    "token": self.finnhub_api_key,
                    "from": today,
                    "to": tomorrow,
                }

                resp = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: requests.get(url, params=params, timeout=10)
                )
                if resp.status_code == 200:
                    items = resp.json()
                    for item in items:
                        news_id = item.get("id") or item.get("headline", "")
                        if news_id and news_id not in seen_ids:
                            seen_ids.add(news_id)
                            item["_source_symbol"] = symbol
                            all_news.append(item)

                await asyncio.sleep(1.5)  # P0-2 修复 (2026-05-03): 非阻塞延迟
            except Exception as e:
                logger.debug(f"[新闻获取] {symbol} 失败: {e}")

        return all_news

    # ====================================================================
    #  LLM 研判
    # ====================================================================

    async def _llm_market_judgment(
        self, symbol: str, trigger_type: str, reason: str, price: float
    ) -> str:
        """
        行情指标触发的 LLM 研判

        区分技术性回调 vs 基本面崩溃
        """
        try:
            system_prompt = EMERGENCY_SYSTEM_PROMPT

            user_prompt = f"""标的: {symbol}
当前价: ${price:.2f}
触发类型: {trigger_type}
触发原因: {reason}

判断逻辑:
- 如果是价格闪崩 >5% 或连续暴跌 → 可能是重大利空泄露 → EXIT
- 如果是放量暴跌 + 大盘也在跌 → 可能是系统性风险 → EXIT
- 如果只是技术性回调 (放量不大, 跌幅 <3%) → HOLD
- 如果不确定 → EXIT (保守原则)

只输出 EXIT 或 HOLD。"""

            text = await UnifiedLLMService.call_text_async(
                "emergency_interceptor",
                user_prompt,
                system=system_prompt,
                temperature=0.0,
                max_tokens=10,
                timeout=15,
            )

            result = (text or "").strip().upper()
            logger.info(f"[行情 LLM] {symbol} {trigger_type} → {result}")
            return result if result in ("EXIT", "HOLD") else "HOLD"

        except Exception as e:
            logger.warning(f"[行情 LLM] 研判失败，保守退出: {e}")
            return "EXIT"

    async def _llm_news_verdict(self, news_item: Dict, positions: list) -> str:
        """
        P0 修复 (2026-05-01): LLM 二阶确认 — 判断模糊关键词匹配的新闻是否构成真正黑天鹅
        
        用于宏观/地缘等非高危关键词的二次确认，防止 IMF 会议、常规出口管制等新闻
        误触发 RISK_OFF。
        
        Returns:
            "RISK_OFF" (确认为黑天鹅) 或 "IGNORE" (非黑天鹅)
        """
        try:
            headline = news_item.get("headline", "")
            summary = news_item.get("summary", "")
            symbols = ", ".join([p.symbol.replace("US.", "") for p in positions])

            system_prompt = (
                "你是 FTSeek 美股风控分析师。判断新闻是否构成针对持仓标的的黑天鹅事件。\n"
                "黑天鹅定义: 突发的、对公司基本面有直接重大负面影响的事件。\n"
                "非黑天鹅: 常规宏观经济新闻、地缘政治讨论、IMF/世界银行会议、关税讨论等。"
            )

            user_prompt = (
                f"持仓标的: {symbols}\n"
                f"新闻标题: {headline}\n"
                f"新闻摘要: {summary}\n\n"
                "判断标准:\n"
                "- 宏观趋势、地缘政治、关税政策、贸易关系 → IGNORE\n"
                "- IMF、世界银行、G7/G20 等机构会议 → IGNORE\n"
                "- 对已有事件的评论/分析，不是突发 → IGNORE\n"
                "- SEC 调查、财务造假、破产、诉讼、FDA 拒绝 → RISK_OFF\n"
                "- 针对持仓标的的具体公司生存威胁 → RISK_OFF\n"
                "- 不确定 → IGNORE（保守原则：宁可放过不拦截）\n\n"
                "只输出 RISK_OFF 或 IGNORE。"
            )

            text = await UnifiedLLMService.call_text_async(
                "emergency_interceptor",
                user_prompt,
                system=system_prompt,
                temperature=0.0,
                max_tokens=10,
                timeout=15,
            )

            result = (text or "").strip().upper()
            logger.info(f"[新闻 LLM 二阶确认] {symbols} → {result}")
            return result if result in ("RISK_OFF", "IGNORE") else "IGNORE"

        except Exception as e:
            # P1-5 修复 (2026-05-03): 统一失败策略 → RISK_OFF (fail-closed)
            # 与其他两个 LLM 研判方法保持一致
            logger.warning(f"[新闻 LLM 二阶确认] 失败，保守视为 RISK_OFF: {e}")
            return "RISK_OFF"

    async def _llm_risk_judgment(self, news_item, position) -> str:
        """新闻触发的 LLM 研判"""
        try:
            headline = news_item.get("headline", "")
            summary = news_item.get("summary", "")
            symbol = position.symbol.replace("US.", "")

            system_prompt = EMERGENCY_DESTRUCTIVE_PROMPT
            user_prompt = (
                f"持仓: {symbol}\n新闻: {headline}\n摘要: {summary}\n"
                f"判断: 立即清仓 (EXIT) 或持有 (HOLD)？"
            )

            text = await UnifiedLLMService.call_text_async(
                "emergency_interceptor",
                user_prompt,
                system=system_prompt,
                temperature=0.0,
                max_tokens=10,
                timeout=15,
            )

            result = (text or "").strip().upper()
            logger.info(f"[新闻 LLM] {symbol} → {result}")
            return result if result in ("EXIT", "HOLD") else "HOLD"

        except Exception as e:
            logger.warning(f"[新闻 LLM] 研判失败，保守退出: {e}")
            return "EXIT"

    # ====================================================================
    #  告警 & 清仓
    # ====================================================================

    def _send_market_alert(self, symbol, trigger_type, reason, price, ref_price):
        """行情指标告警"""
        md = f"### 🔴 美股行情突发预警\n\n"
        md += f"**标的**: {symbol}\n"
        md += f"**触发**: {trigger_type}\n"
        md += f"**当前价**: ${price:.2f} (参考 ${ref_price:.2f})\n"
        md += f"**原因**: {reason}\n\n"
        md += f"**系统动作**: 已切换至 RISK_OFF (禁止开仓)\n"
        md += f"**状态**: LLM 研判中..."

        try:
            self.notifier.send_text(md)
        except Exception as e:
            logger.warning(f"[告警] 钉钉推送失败: {e}")

    def _send_alert(self, item, keywords, positions):
        """新闻告警"""
        symbols = ", ".join([p.symbol for p in positions])
        md = f"### 🚨 美股突发风险预警 (RISK_OFF)\n\n"
        md += f"**涉及标的**: {symbols}\n"
        md += f"**关键词**: {', '.join(keywords)}\n\n"
        md += f"**新闻标题**: {item.get('headline')}\n\n"
        md += f"**系统动作**: 已切换至 RISK_OFF (禁止开仓)\n"
        md += f"**当前状态**: 等待 LLM 研判或自动恢复"

        try:
            self.notifier.send_text(md)
        except Exception as e:
            logger.warning(f"[告警] 钉钉推送失败: {e}")

    async def _get_position_by_symbol(self, symbol: str):
        """根据 symbol 获取持仓对象"""
        for pos in await _safe_get_positions(self.scheduler):
            if pos.symbol == symbol:
                return pos
        return None

    async def _execute_emergency_exit(self, position, reason):
        """执行紧急清仓 (带幂等性检查)"""
        try:
            symbol = position.symbol

            # 幂等检查
            current_positions = await _safe_get_positions(self.scheduler)
            if not any(p.symbol == symbol for p in current_positions):
                logger.info(f"⚠️ [幂等拦截] {symbol} 已无持仓，取消清仓")
                return

            logger.info(f"⚡ 正在执行紧急清仓: {symbol} ...")

            if hasattr(self.scheduler, 'close_position'):
                res = self.scheduler.close_position(symbol, reason="EMERGENCY_INTERCEPT")
                if res.get('success'):
                    logger.info(f"✅ 紧急清仓成功: {symbol}")
                    self.notifier.send_text(f"✅ 紧急清仓成功: {symbol} (原因: {reason})")
                else:
                    logger.error(f"❌ 紧急清仓失败: {symbol} -> {res}")
            else:
                logger.error("❌ Scheduler 没有 close_position 方法，无法自动清仓")
        except Exception as e:
            logger.error(f"❌ 执行清仓异常: {e}")
