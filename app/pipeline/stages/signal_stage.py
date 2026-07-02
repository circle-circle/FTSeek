"""
Signal Stage — 信号生成 (策略编排层)

职责:
  1. Regime Filter 门禁 (ADR-007: 2026-06-16) — 先判环境, 再选策略
  2. 收集所有策略候选 (collect-all) + 质量评分 + 信号融合
  3. 快车道 → ORB → 技术面 (trend_first 已禁用, IC=-0.24)

策略本身是纯函数 (app/strategy/pure/), 本 Stage 只负责编排调用顺序。

ADR-007 核心变更:
  - 新增 Regime Filter 作为所有策略的前置门禁
  - trend_first 全局禁用 (IC=-0.24, 历史验证有害)
  - TECHNICAL_MULTI_FACTOR 受 regime 准入门控
  - 信号门槛随 regime 动态调整 (STRONG_BULL 宽松, CAUTION 严格)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import evaluate as eval_strategy

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  P1 (2026-06-17): 日均信号数量监控 — 防止过度过滤
#  跟踪每日信号放行/过滤数量, 信号过少时发出警告
# ═══════════════════════════════════════════════════════════════════

class _SignalCounter:
    """信号计数器 (进程内, 按交易日重置)"""

    _MIN_DAILY_SIGNALS = 3      # 日均信号低于此数发出警告
    _MIN_DAILY_CANDIDATES = 5   # 日均候选低于此数说明选股池过窄

    def __init__(self):
        self._date: str = ""
        self._generated: int = 0
        self._filtered_by_quality: int = 0
        self._filtered_by_regime: int = 0
        self._filtered_by_direction: int = 0
        self._total_candidates: int = 0
        self._warned: bool = False

    def _check_date_reset(self):
        """检查并重置日期"""
        try:
            from app.utils.timezone import now_et
            today = now_et().strftime("%Y-%m-%d")
        except Exception:
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date:
            if self._date and self._total_candidates > 0:
                self._emit_summary()
            self._date = today
            self._generated = 0
            self._filtered_by_quality = 0
            self._filtered_by_regime = 0
            self._filtered_by_direction = 0
            self._total_candidates = 0
            self._warned = False

    def record_generated(self):
        self._check_date_reset()
        self._generated += 1

    def record_filtered(self, reason: str = "quality"):
        self._check_date_reset()
        if reason == "regime":
            self._filtered_by_regime += 1
        elif reason == "direction":
            self._filtered_by_direction += 1
        else:
            self._filtered_by_quality += 1

    def record_candidates(self, count: int):
        self._check_date_reset()
        self._total_candidates += count

    def _emit_summary(self):
        """输出前一日信号统计"""
        total_filtered = self._filtered_by_quality + self._filtered_by_regime + self._filtered_by_direction
        logger.info(
            f"[SignalCounter] {self._date} 日终统计: "
            f"生成={self._generated} 候选={self._total_candidates} "
            f"过滤={total_filtered} (质量={self._filtered_by_quality}, "
            f"regime={self._filtered_by_regime}, 方向={self._filtered_by_direction})"
        )

    def check_warning(self):
        """检查是否需要发出过度过滤警告"""
        self._check_date_reset()
        if self._warned:
            return
        if self._total_candidates >= 10 and self._generated < self._MIN_DAILY_SIGNALS:
            self._warned = True
            total_filtered = self._filtered_by_quality + self._filtered_by_regime + self._filtered_by_direction
            logger.warning(
                f"⚠️ [SignalCounter·过度过滤] {self._date}: "
                f"候选={self._total_candidates} 但生成仅={self._generated} "
                f"(过滤={total_filtered}). "
                f"建议检查 Regime 门槛/VIX折扣/LONG折扣是否过于严格"
            )
        if self._total_candidates >= 20 and self._generated == 0:
            self._warned = True
            logger.warning(
                f"⚠️ [SignalCounter·零信号] {self._date}: "
                f"已扫描 {self._total_candidates} 个候选但 0 信号生成. "
                f"可能原因: Regime=CRISIS/BEAR, VIX>=28, 或所有策略IC为负"
            )


_signal_counter = _SignalCounter()


# ═══════════════════════════════════════════════════════════════════
#  ADR-008 (2026-06-28): 技术定方向仲裁 — LLM 退化为确认/冲突标记
#  当前模式开关读取自 unified_config.yaml::signal_stage.mode
#  "technical_direction_arbitration": technical 拥有方向否决权
#  "legacy_collect_all"(默认): 保持原 collect-all 候选融合逻辑
# ═══════════════════════════════════════════════════════════════════

# 策略 ID 常量，用于区分技术信号与 LLM 投票来源
_TECHNICAL_STRATEGY_ID = "TECHNICAL_MULTI_FACTOR"
_LLM_STRATEGY_ID = "voting_llm"


def _get_signal_stage_mode() -> str:
    """读取 signal_stage 运行模式"""
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        return str(_cfg.get_dotted("signal_stage.mode", "legacy_collect_all")).lower()
    except Exception:
        return "legacy_collect_all"


def _get_agreement_boost() -> float:
    """读取 LLM 同向确认时的置信度加成"""
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        return float(_cfg.get_dotted("signal_stage.technical_arbitration.agreement_boost", 0.05))
    except Exception:
        return 0.05


def _get_llm_conflict_threshold() -> float:
    """读取 LLM 反向冲突拦截阈值"""
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        return float(_cfg.get_dotted("signal_stage.technical_arbitration.llm_conflict_confidence_threshold", 0.70))
    except Exception:
        return 0.70


def _get_shadow_log_path() -> str:
    """技术方向影子日志路径"""
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        return str(_cfg.get_dotted("signal_stage.technical_arbitration.shadow_log_path", "data/shadow_technical_direction.jsonl"))
    except Exception:
        return "data/shadow_technical_direction.jsonl"


def _normalize_direction(c) -> str:
    """将 SignalCandidate 的方向统一转成字符串"""
    d = c.direction if isinstance(c.direction, str) else (
        c.direction.value if hasattr(c.direction, 'value') else str(c.direction)
    )
    return str(d).upper()


def _log_shadow_decision(symbol: str, technical_dir: str, llm_dir: Optional[str],
                          llm_conf: float, technical_strength: int, outcome: str):
    """记录技术定方向影子日志 (JSONL)"""
    import json
    import os
    from datetime import datetime

    log_path = _get_shadow_log_path()
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except Exception:
        pass
    try:
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "technical_direction": technical_dir,
            "llm_direction": llm_dir,
            "llm_confidence": llm_conf,
            "technical_strength": technical_strength,
            "outcome": outcome,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[TechnicalArbitration] shadow log failed: {e}")


def _apply_technical_direction_arbitration(candidates: list[SignalCandidate], symbol: str) -> Optional[SignalCandidate]:
    """
    技术定方向仲裁:
      - technical 候选存在时, 以 technical 方向为准
      - LLM 同向 → 置信度加成
      - LLM 强反向 (confidence >= 阈值) → 拦截, 不交易
      - 无 technical 候选 → 无论 LLM 说什么都不交易
    """
    technical_candidates = [c for c in candidates if getattr(c, "strategy_id", "") == _TECHNICAL_STRATEGY_ID]
    llm_candidates = [c for c in candidates if getattr(c, "strategy_id", "") == _LLM_STRATEGY_ID]

    if not technical_candidates:
        if llm_candidates:
            logger.info(
                f"[TechnicalArbitration] {symbol} LLM-only signal rejected "
                f"(no technical direction)"
            )
            _log_shadow_decision(symbol, "", llm_candidates[0].direction, llm_candidates[0].confidence, 0, "llm_only_rejected")
        return None

    best = max(technical_candidates, key=lambda c: c.confidence)
    tech_direction = _normalize_direction(best)

    # 检查 LLM 强反向冲突
    conflict_threshold = _get_llm_conflict_threshold()
    for llm in llm_candidates:
        llm_dir = _normalize_direction(llm)
        if llm_dir != tech_direction and llm.confidence >= conflict_threshold:
            logger.info(
                f"[TechnicalArbitration] {symbol} blocked: "
                f"technical={tech_direction} vs LLM={llm_dir}(conf={llm.confidence:.2f})"
            )
            _log_shadow_decision(symbol, tech_direction, llm_dir, llm.confidence, best.strength, "blocked_conflict")
            return None

    # LLM 同向确认加成
    agreement_boost = _get_agreement_boost()
    for llm in llm_candidates:
        if _normalize_direction(llm) == tech_direction:
            best.confidence = min(0.95, best.confidence + agreement_boost)
            best.rationale += " | LLM confirms"
            if isinstance(getattr(best, "metadata", None), dict):
                best.metadata["llm_confirmed"] = True
            break

    # 确保 owner_strategy 写入 metadata
    if isinstance(getattr(best, "metadata", None), dict):
        if not best.metadata.get("owner_strategy"):
            best.metadata["owner_strategy"] = getattr(best, "strategy_id", "")

    logger.info(
        f"[TechnicalArbitration] {symbol} {tech_direction} "
        f"technical_wins (strength={best.strength}, conf={best.confidence:.3f})"
    )
    _log_shadow_decision(symbol, tech_direction,
                         llm_candidates[0].direction if llm_candidates else "",
                         llm_candidates[0].confidence if llm_candidates else 0.0,
                         best.strength, "traded")
    return best


def _get_regime_verdict(ctx: SymbolContext):
    """从 SymbolContext 中提取数据, 调用 Regime Filter 评估当前环境

    P0 (2026-06-17): 支持 rapid_regime_override — scan_loop 检测到急变时注入
    """
    try:
        from app.strategy.regime_filter import evaluate_regime_with_hysteresis, RegimeVerdict, Regime
        from app.analysis.market_breadth import get_market_breadth

        breadth = get_market_breadth()
        # P1 (2026-06-17): 使用滞回版本, 防止边界震荡
        verdict = evaluate_regime_with_hysteresis(
            spy_price=getattr(ctx, 'spy_price', 0) or 0,
            spy_ma200=getattr(ctx, 'spy_ma200', 0) or 0,
            vix=getattr(ctx, 'vix', 20) or 20,
            oversold_count=breadth.get_oversold_count(),
            scanned_count=getattr(breadth, '_scan_count', 0),
        )

        # P0: 如果 scan_loop 检测到快速 regime 切换, 强制收紧到更保守的 regime
        _rapid = getattr(ctx, '_rapid_regime_override', None)
        if _rapid and verdict:
            # 只允许收紧 (升级到更保守), 不允许放松
            _severity = {
                Regime.STRONG_BULL: 0, Regime.BULL: 1,
                Regime.CAUTION: 2, Regime.BEAR: 3, Regime.CRISIS: 4,
            }
            _target_map = {
                "CAUTION": Regime.CAUTION,
                "BEAR": Regime.BEAR,
            }
            _target_regime = _target_map.get(_rapid)
            if _target_regime and _severity.get(_target_regime, 0) > _severity.get(verdict.regime, 0):
                logger.warning(
                    f"[RegimeQuickSwitch] 强制收紧: {verdict.regime.value} → {_target_regime.value}"
                )
                # 重新评估, 用更保守的 regime 参数覆盖
                # 简单方式: 收紧 min_strength/min_confidence 和 position_scale
                if _target_regime == Regime.CAUTION:
                    verdict = RegimeVerdict(
                        regime=Regime.CAUTION,
                        allow_momentum=True,
                        allow_trend_first=False,
                        allow_orb=True,
                        allow_fast_lane=True,
                        allow_insider=True,
                        allow_earnings_revision=True,
                        position_scale=min(verdict.position_scale, 0.50),
                        min_strength=max(verdict.min_strength, 55),
                        min_confidence=max(verdict.min_confidence, 0.55),
                        allowed_directions=["LONG", "SHORT"],
                        rationale=f"RAPID_SWITCH→CAUTION (原{verdict.regime.value})",
                    )
                elif _target_regime == Regime.BEAR:
                    verdict = RegimeVerdict(
                        regime=Regime.BEAR,
                        allow_momentum=False,
                        allow_trend_first=False,
                        allow_orb=False,
                        allow_fast_lane=True,
                        allow_insider=True,
                        allow_earnings_revision=True,
                        position_scale=min(verdict.position_scale, 0.25),
                        min_strength=max(verdict.min_strength, 75),
                        min_confidence=max(verdict.min_confidence, 0.70),
                        allowed_directions=["SHORT"],
                        rationale=f"RAPID_SWITCH→BEAR (原{verdict.regime.value})",
                    )

        return verdict
    except Exception as e:
        logger.warning(f"[SignalStage] Regime Filter 评估异常, 放行: {e}")
        return None


def _apply_quality_scoring(ctx: SymbolContext, candidate: SignalCandidate) -> None:
    """对单个候选施加 SignalQualityScorer 评分, 就地修改 confidence/strength"""
    try:
        from app.strategy.signal_quality_scorer import SignalQualityScorer
        direction_str = candidate.direction if isinstance(candidate.direction, str) else (
            candidate.direction.value if hasattr(candidate.direction, 'value') else str(candidate.direction)
        )
        quality = SignalQualityScorer.score(
            ctx, direction_str,
            strategy_confidence=getattr(candidate, 'confidence', None),
        )
        candidate.confidence = quality.confidence
        candidate.strength = quality.strength
        if isinstance(getattr(candidate, 'metadata', None), dict):
            candidate.metadata["quality_factors"] = quality.to_dict()
    except Exception as qe:
        logger.debug(f"[SignalStage] quality scoring failed: {qe}")


def process(ctx: SymbolContext,
            position_store=None,
            battle_plan_manager=None,
            voting_coordinator=None,
            _skip_execution_in_trading: bool = False) -> Optional[SignalCandidate]:
    """
    策略编排: Regime Filter → collect-all → 质量评分 → 信号融合.

    ADR-007 (2026-06-16): Regime Filter 作为第一道门禁
      - STRONG_BULL: 全策略放行, 门槛宽松
      - BULL: 动量策略放行, 门槛适中
      - CAUTION: 仅快车道+ORB, 门槛严格
      - BEAR/CRISIS: 仅快车道(人审), 基本停止

    策略顺序:
      1. 快车道 (作战计划匹配, 永远放行)
      2. ORB (开盘区间突破, regime 准许时)
      3. Technical (多因子动量, regime 准许时)
      — trend_first 已全局禁用 (IC=-0.24) —
      4. LLM 投票 (盘前/盘后, regime 准许时)

    所有命中策略收集后:
      - 每个候选施加 SignalQualityScorer 多维评分
      - 同方向多策略 → 汇合加成 (confluence bonus)
      - 方向冲突 → 取最强候选 + 惩罚
    """
    symbol = ctx.symbol

    # ── P0 (2026-05-02): 时段门禁 — 盘前/盘后禁止新开仓 ──
    # 迁移自 Legacy L1675-1685
    # 只有 TRADING 时段允许新建持仓。盘前/盘后仅允许已有持仓的退出监控。
    # 快车道(fast_lane)是例外 — 人已审核, 任何时段都放行
    try:
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        _current_phase = get_market_phase()
    except Exception:
        _current_phase = None

    # ── 持仓检查 (快车道和LLM投票共用) ──
    try:
        has_position = bool(
            position_store
            and hasattr(position_store, 'has')
            and position_store.has(symbol)
        )
    except Exception:
        has_position = False

    # 时段门禁: 非TRADING + 无持仓 → 只允许快车道, 其他策略全部跳过
    _non_trading_block_auto = False
    if _current_phase is not None and _current_phase != MarketPhase.TRADING and not has_position:
        logger.info(
            f"[时段门禁] {symbol} 当前阶段={_current_phase.value} 且无现有持仓，"
            f"仅允许快车道 (等待 TRADING 时段)"
        )
        _non_trading_block_auto = True

    # ── ADR-007: Regime Filter — 环境门禁 ──
    regime_verdict = _get_regime_verdict(ctx)
    if regime_verdict:
        logger.info(
            f"[Regime] {symbol} regime={regime_verdict.regime.value} "
            f"pos_scale={regime_verdict.position_scale} | {regime_verdict.rationale}"
        )
        # 将 regime 注入 ctx 供下游使用
        ctx.market_regime = regime_verdict.regime.value
    else:
        logger.debug(f"[Regime] {symbol} 无法评估, 默认放行")

    candidates: list[SignalCandidate] = []

    # ── 策略 1: 快车道 (作战计划, 永远放行 — 人已审核) ──
    if not has_position:
        try:
            if battle_plan_manager:
                target = battle_plan_manager.get_target_info(symbol)
                if target:
                    if target.get("status") == "CANCELLED":
                        logger.info(f"[Signal] {symbol} 作战计划已取消")
                        return None
                    if target.get("status") == "ACTIVE":
                        ctx.metadata["battle_plan_match"] = target
                        candidate = eval_strategy("fast_lane", ctx)
                        if candidate is not None:
                            _apply_quality_scoring(ctx, candidate)
                            logger.info(
                                f"[Signal] {symbol} 快车道命中: {candidate.rationale}"
                            )
                            candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] 快车道检查异常 (放行): {e}")

    # ── 策略 2: ORB (开盘区间突破, regime 准许时) ──
    _orb_allowed = True
    if regime_verdict and regime_verdict.blocks("orb"):
        _orb_allowed = False
        logger.debug(f"[Regime] {symbol} ORB 被 {regime_verdict.regime.value} 阻止")
    if _orb_allowed and not has_position and not _non_trading_block_auto:
        try:
            candidate = eval_strategy("orb", ctx)
            if candidate is not None:
                _apply_quality_scoring(ctx, candidate)
                logger.info(
                    f"[Signal] {symbol} ORB命中: {candidate.rationale}"
                )
                candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] ORB异常 (放行): {e}")

    # ── 策略 3: trend_first — ADR-007 全局禁用 (IC=-0.24, 历史验证有害) ──
    # 保留代码路径但默认不执行。如需恢复: 设 config strategies.trend_first.enabled=true
    # 且 regime 准许 (当前 regime_filter 永远阻止 trend_first)
    _trend_first_enabled = False
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _trend_first_enabled = bool(_cfg.get_dotted("strategies.trend_first.enabled", False))
    except Exception:
        pass
    if _trend_first_enabled and regime_verdict and regime_verdict.allow_trend_first:
        try:
            candidate = eval_strategy("trend_first", ctx)
            if candidate is not None:
                _apply_quality_scoring(ctx, candidate)
                logger.info(
                    f"[Signal] {symbol} TrendFirst命中: {candidate.rationale}"
                )
                candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] TrendFirst异常 (放行): {e}")

    # ── 策略 4: Technical (多因子动量) — regime 准入门控 ──
    _technical_enabled = True
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _technical_enabled = bool(_cfg.get_dotted("strategies.technical.enabled", True))
    except Exception:
        pass
    _technical_regime_ok = True
    if regime_verdict and regime_verdict.blocks("TECHNICAL_MULTI_FACTOR"):
        _technical_regime_ok = False
        logger.debug(f"[Regime] {symbol} TECHNICAL 被 {regime_verdict.regime.value} 阻止")
    if _technical_enabled and _technical_regime_ok and not _non_trading_block_auto:
        try:
            candidate = eval_strategy("technical", ctx)
            if candidate is not None:
                _apply_quality_scoring(ctx, candidate)
                logger.info(
                    f"[Signal] {symbol} 技术面命中: {candidate.rationale}"
                )
                candidates.append(candidate)
            else:
                logger.debug(f"[Signal] {symbol} 技术面无命中")
        except Exception as e:
            logger.debug(f"[Signal] 技术面异常 (放行): {e}")

    # ── 策略 5: LLM 投票 (无持仓时, regime 准许时) ──
    _voting_llm_enabled = True
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _voting_llm_enabled = bool(_cfg.get_dotted("strategies.voting_llm.enabled", True))
    except Exception:
        pass
    _llm_regime_ok = True
    if regime_verdict and regime_verdict.regime.value in ("BEAR", "CRISIS"):
        _llm_regime_ok = False
    if _voting_llm_enabled and _llm_regime_ok and not _non_trading_block_auto:
        try:
            # P0 (2026-06-29): TRADING阶段Playbook独占时跳过LLM投票, 消灭空转省$2.7/天
            if _skip_execution_in_trading:
                logger.debug(f"[SignalStage] {symbol} _skip_execution_in_trading=True, 跳过LLM投票")
            elif voting_coordinator and not has_position:
                # Skip LLM vote if we already have high-confidence candidates
                if candidates and any(c.confidence >= 0.70 for c in candidates):
                    logger.info(f"[SignalStage] Skipping LLM vote for {symbol} - already have high-confidence candidates")
                else:
                    # LLM vote with timeout
                    try:
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(voting_coordinator.vote, symbol, ctx)
                            vote_result = future.result(timeout=15.0)  # 2026-06-26: 5s→15s, LLM需要更多时间
                            if vote_result:
                                ctx.metadata["vote_result"] = vote_result
                                candidate = eval_strategy("voting_llm", ctx)
                                if candidate is not None:
                                    _apply_quality_scoring(ctx, candidate)
                                    logger.info(
                                        f"[Signal] {symbol} LLM投票命中: {candidate.rationale}"
                                    )
                                    candidates.append(candidate)
                    except concurrent.futures.TimeoutError:
                        logger.warning(f"[SignalStage] LLM vote timeout for {symbol}")
                    except Exception as e:
                        logger.debug(f"[SignalStage] LLM vote failed for {symbol}: {e}")
        except Exception as e:
            logger.debug(f"[Signal] LLM投票异常 (放行): {e}")

    # ── 策略 6: 内部人集群买入 (ADR-007, SEC Form 4) ──
    # 独立于 regime — 内部人买入是基本面信号, 任何环境都有效
    _insider_enabled = True
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _insider_enabled = bool(_cfg.get_dotted("strategies.insider.enabled", True))
    except Exception:
        pass
    if _insider_enabled and not has_position and not _non_trading_block_auto:
        try:
            candidate = eval_strategy("insider", ctx)
            if candidate is not None:
                _apply_quality_scoring(ctx, candidate)
                logger.info(
                    f"[Signal] {symbol} 内部人集群买入命中: {candidate.rationale}"
                )
                candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] 内部人信号异常 (放行): {e}")

    # ── 策略 7: 分析师修正动量 (ADR-007, Yahoo Finance) ──
    _erev_enabled = True
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _erev_enabled = bool(_cfg.get_dotted("strategies.earnings_revision.enabled", True))
    except Exception:
        pass
    if _erev_enabled and not has_position and not _non_trading_block_auto:
        try:
            candidate = eval_strategy("earnings_revision", ctx)
            if candidate is not None:
                _apply_quality_scoring(ctx, candidate)
                logger.info(
                    f"[Signal] {symbol} 分析师修正动量命中: {candidate.rationale}"
                )
                candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] 分析师修正信号异常 (放行): {e}")

    # ── 无候选 → 返回 None ──
    if not candidates:
        logger.debug(f"[Signal] {symbol} 无策略命中")
        _signal_counter.check_warning()
        return None

    # P1: 记录本轮候选数量
    _signal_counter.record_candidates(len(candidates))

    # ── ADR-007: Regime-aware 质量底线 (动态门槛) ──
    # 原固定门槛: strength<30 OR confidence<0.30
    # 新动态门槛: 由 regime 决定
    _min_strength = 30
    _min_confidence = 0.30
    if regime_verdict:
        _min_strength = regime_verdict.min_strength
        _min_confidence = regime_verdict.min_confidence
    filtered = []
    for c in candidates:
        _s = int(getattr(c, 'strength', 0) or 0)
        _cf = float(getattr(c, 'confidence', 0.0) or 0.0)
        if _s < _min_strength or _cf < _min_confidence:
            logger.info(
                f"[Signal] {symbol} 弱信号过滤: strategy={c.strategy_id} "
                f"strength={_s}<{_min_strength} OR conf={_cf:.2f}<{_min_confidence} "
                f"(regime={regime_verdict.regime.value if regime_verdict else 'unknown'})"
            )
            _signal_counter.record_filtered("quality")
        else:
            # ADR-007: regime 方向门禁
            if regime_verdict:
                _dir = c.direction if isinstance(c.direction, str) else (
                    c.direction.value if hasattr(c.direction, 'value') else str(c.direction)
                )
                if regime_verdict.blocks_direction(_dir):
                    logger.info(
                        f"[Regime] {symbol} 方向 {_dir} 被 {regime_verdict.regime.value} 阻止 "
                        f"(allowed={regime_verdict.allowed_directions})"
                    )
                    _signal_counter.record_filtered("direction")
                    continue
            filtered.append(c)
    if not filtered:
        logger.info(f"[Signal] {symbol} 所有候选未通过质量底线 "
                    f"(strength≥{_min_strength} AND conf≥{_min_confidence}, "
                    f"regime={regime_verdict.regime.value if regime_verdict else 'unknown'})")
        _signal_counter.check_warning()
        return None
    candidates = filtered

    # ── ADR-007: Regime 仓位缩放注入 metadata ──
    if regime_verdict and regime_verdict.position_scale < 1.0:
        for c in candidates:
            if isinstance(getattr(c, 'metadata', None), dict):
                c.metadata["regime_position_scale"] = regime_verdict.position_scale

    # ── SPY 方向门禁 (保留: 牛市抑制弱 SHORT 噪音) ──
    _spy_below = getattr(ctx, 'spy_below_ma200', False)
    if not _spy_below and len(candidates) > 0:
        _short_filtered = []
        for c in candidates:
            _dir = c.direction if isinstance(c.direction, str) else (
                c.direction.value if hasattr(c.direction, 'value') else str(c.direction)
            )
            if _dir == "SHORT":
                _str = int(getattr(c, 'strength', 0) or 0)
                if _str < 70:
                    logger.info(
                        f"[Signal] {symbol} SHORT 噪音过滤 (SPY>MA200): "
                        f"strategy={c.strategy_id} strength={_str}<70"
                    )
                    continue
            _short_filtered.append(c)
        if _short_filtered:
            candidates = _short_filtered

    # ── ADR-008: 技术定方向仲裁 (2026-06-28) ──
    # 当配置 mode=technical_direction_arbitration 时, technical 拥有方向否决权:
    #   - LLM 同向 → 加成
    #   - LLM 强反向 → 拦截
    #   - 无 technical 候选 → 不交易
    if _get_signal_stage_mode() == "technical_direction_arbitration":
        _arb_result = _apply_technical_direction_arbitration(candidates, symbol)
        if _arb_result is not None:
            _signal_counter.record_generated()
            _signal_counter.check_warning()
            return _arb_result
        return None

    # ── 单候选 → 直接返回 (已施加质量评分) ──
    if len(candidates) == 1:
        _single = candidates[0]
        # P0 (2026-06-05): 确保 owner_strategy 写入 metadata (下游 order_executor 依赖)
        if isinstance(getattr(_single, 'metadata', None), dict):
            if not _single.metadata.get("owner_strategy"):
                _single.metadata["owner_strategy"] = getattr(_single, 'strategy_id', '')
        _signal_counter.record_generated()
        _signal_counter.check_warning()
        return _single

    # ── 信号融合: 多策略命中 ──
    directions = []
    for c in candidates:
        d = c.direction if isinstance(c.direction, str) else (
            c.direction.value if hasattr(c.direction, 'value') else str(c.direction)
        )
        directions.append(d)

    if len(set(directions)) == 1:
        # 所有候选同方向 → 汇合加成 (confluence bonus)
        best = max(candidates, key=lambda c: c.confidence)
        bonus = 0.08 * (len(candidates) - 1)
        best.confidence = min(0.95, best.confidence + bonus)
        best.strength = min(99, best.strength + 5 * (len(candidates) - 1))
        if isinstance(getattr(best, 'metadata', None), dict):
            best.metadata["fused"] = True
            best.metadata["fused_count"] = len(candidates)
            best.metadata["fused_strategies"] = [c.strategy_id for c in candidates]
            # P0 (2026-06-05): 保留获胜者的策略身份，防止被下游覆盖为 "voting_llm"
            best.metadata["owner_strategy"] = getattr(best, 'strategy_id', '') or best.metadata.get("owner_strategy", "")
            # 汇总所有候选的 owner_strategy (用于归因分析)
            _all_owners = list({getattr(c, 'strategy_id', '') for c in candidates if getattr(c, 'strategy_id', '')})
            if _all_owners:
                best.metadata["contributing_strategies"] = _all_owners
        logger.info(
            f"[SignalFusion] {symbol} {directions[0]} "
            f"fused {len(candidates)} strategies → conf={best.confidence:.3f}"
        )
        _signal_counter.record_generated()
        _signal_counter.check_warning()
        return best

    # 方向冲突 → 取最强候选 + 惩罚
    best = max(candidates, key=lambda c: c.confidence)
    best.confidence = max(0.30, best.confidence - 0.05)
    if isinstance(getattr(best, 'metadata', None), dict):
        best.metadata["conflict"] = True
        # P0 (2026-06-05): 冲突路径也保留策略身份
        best.metadata["owner_strategy"] = getattr(best, 'strategy_id', '') or best.metadata.get("owner_strategy", "")
    logger.info(
        f"[SignalFusion] {symbol} conflict "
        f"({', '.join(directions)}), taking strongest with penalty"
    )
    _signal_counter.record_generated()
    _signal_counter.check_warning()
    return best
