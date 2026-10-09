"""Bridge decision adapters (阶段 2a 迁自 app/playbook/pipeline.py)。

迁移纪律: 类本体逐字迁移; 文件加载、健康检查与运行期重载仍留在 pipeline,
保持调度入口与既有 monkeypatch 缝不变。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class NullVotingAdapter:
    """空决策适配器 — bridge文件不存在时使用, 全部候选SKIP.

    2026-07-09: 替代VotingCoordinator作为bridge不存在时的fallback。
    根因: 3模型投票方向准确率47%≈随机, 用噪声投票替代信号不如不交易。
    deep_analysis skill负责盘前生成bridge文件, bridge不存在=今天没有高质量信号。
    """

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """始终返回None → pipeline将SKIP该候选。"""
        logger.info(f"[NullVoting] {symbol} SKIP — 无bridge文件, 不用噪声投票")
        return None

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — NullVotingAdapter 不写 voting_predictions 表."""
        pass


class TechnicalFallbackAdapter:
    """DA bridge 缺席时的确定性 fallback — 用 technical 纯函数策略出方向.

    2026-07-11 (双脑清理): bridge 不存在时替代 NullVotingAdapter 的“全 SKIP”。
    设计担当: DA = 入场决策脑; DA 缺席时用确定性、非 LLM、可单测的 technical
    策略兜底，而非噪声投票。

    ⚠ 陷阱护栏: 纪律/风控无法把“随机方向入场”变成正期望。所以 fallback 必须有
    确定性依据(technical)、独立归因(strategy_id=technical_fallback)、可关停
    (playbook.fallback_when_no_bridge=none 即回退全 SKIP)。与 DA 互斥 → 任一时刻单链。

    接口兼容: vote(symbol, ctx) → vote_result dict (与 BridgeVotingAdapter 对齐)。
    entry/SL/TP 不在此产出——与 DA 同路径, 由 compute_instruction_values(data) 确定性计算。
    """

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """跑 technical 纯策略出方向; 无方向 → None (SKIP, 不硬凑成交)。"""
        try:
            from app.strategy.pure import evaluate as eval_strategy
            # 2026-07-30 (topup 零产出真凶, 第四道暗门): yaml `strategies.technical.
            #   enabled: false` 是主线时代遗留开关 — evaluate() 入口即 return None,
            #   F1 修的 band 从未被执行 (07-29 带 F1 跑 4 轮仍 +0, 手算三只 strength=65
            #   本应过)。本 Adapter 两个消费方 (topup / 无 bridge 兜底) 设计上就该出
            #   方向 — 局部覆写 enabled, 其余参数 (min_strength 等) 仍从 yaml 读,
            #   调参保持单一真相源。
            # 2026-08-22 (topup孤儿开关修复): 键名从 playbook.topup.technical_override_enabled
            #   改为 playbook.technical_fallback_override_enabled — topup 已于 8/6 停供,
            #   该开关实际控制的是 no-bridge 兜底链(TFA)是否覆写 strategies.technical.enabled=false,
            #   与 topup 无关. 语义修正, 调参者不再误以为控制已停供的 topup.
            _t_cfg = None
            try:
                from app.core.config_container import get_config as _tf_gc
                if bool(_tf_gc().get_dotted("playbook.technical_fallback_override_enabled", False)):
                    from app.strategy.pure import _load_yaml_config as _tf_lyc
                    _t_cfg = dict(_tf_lyc("technical") or {})
                    _t_cfg["enabled"] = True
            except Exception:
                _t_cfg = None  # 覆写失败退回 yaml 行为 (现状 = 全 None)
            cand = eval_strategy("technical", ctx, config=_t_cfg)
        except Exception as e:
            # 2026-07-27 (topup 零产出诊断): debug→warning — 全史零方向产出而日志无声,
            #   静默失败路径必须可见 (静默退化军规)
            logger.warning(f"[TechnicalFallback] {symbol} technical 评估异常: {type(e).__name__}: {e}")
            return None
        if cand is None:
            logger.debug(f"[TechnicalFallback] {symbol} 无候选 (momentum NEUTRAL 或回调不达标)")
            return None
        direction = str(getattr(cand, "direction", "") or "").upper()
        if direction not in ("LONG", "SHORT"):
            return None
        # DEB-20260904 #177: `or 50`/`or 0.5` 会把合法的 0 值静默改写成默认值
        # (strength=0 应被下游 SignalQuality 地板拦截, 改写成 50 反而放行)
        _cand_str = getattr(cand, "strength", None)
        _cand_conf = getattr(cand, "confidence", None)
        strength = int(_cand_str) if _cand_str is not None else 50
        confidence = float(_cand_conf) if _cand_conf is not None else 0.5
        logger.info(f"[TechnicalFallback] {symbol} technical 方向={direction} str={strength} — DA 缺席兜底")
        return {
            "symbol": symbol,
            "direction": direction,
            "final_strength": strength,
            "final_confidence": confidence,
            "consensus_level": confidence,
            "std_dev": 0.0,
            "model_scores": [],
            "is_neutral": False,
            "decision_id": f"TFB-{symbol}-{int(time.time())}",
            "strategy_id": "technical_fallback",
            "conditions": list(getattr(cand, "signal_tags", []) or []),
            "degradation_level": 0,
        }

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — TechnicalFallbackAdapter 不写 voting_predictions 表."""
        pass


class BridgeVotingAdapter:
    """桥接适配器 — 从 opportunity_scan_YYYYMMDD.json 读取 deep_analysis 规划结果.

    替代旧 VotingCoordinator.vote()，消除 LLM 在 FTSeek 热路径。
    接口兼容: vote(symbol, ctx) → vote_result dict (同 VotingCoordinator 格式)。

    Ponytail: 一个类一个方法，不是新模块。
    """

    def __init__(self, candidates_by_symbol: Dict[str, Dict], trade_date: str):
        self._candidates = candidates_by_symbol
        self._trade_date = trade_date

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """返回 bridge 中的候选; vote() 是历史接口兼容名。"""
        cand = self._candidates.get(symbol)
        if cand is None:
            return None

        direction = str(cand.get("direction", "NEUTRAL")).upper()
        if direction not in ("LONG", "SHORT"):
            return None

        # DEB-20260903 #85: score 类型守卫 — cand.get("score", 5.0) 当 score=None 时
        # 返回 None, float(None) 抛 TypeError; schema 校验不查 score 字段, 需消费端守卫
        _raw_score = cand.get("score")
        score = float(_raw_score) if (isinstance(_raw_score, (int, float)) and not isinstance(_raw_score, bool)) else 5.0
        # 2026-08-30 (DEB-20260830 E11 修复): 删除消费端重复合成, 直读生成端权威值.
        # 根因: 原处用 `chall_dir == direction` 字面比较, 但挑战者方向是 BUY/SELL_SHORT
        # 标签体系而 direction 是 LONG/SHORT → 恒不等 → 全候选被误判"方向分歧"恒扣 0.35,
        # 与生成端权威合成 (DA skill adapter._compute_confidence: 同标签体系比较+弃权
        # 中性处理+score差值衰减+fallback独立性检测) 及落库真值 (8/28 全候选 70) 矛盾。
        # 双合成漂移实测泄漏: 8/18-8/25 六条 "BUY confidence 35 < 55" SKIP 行 +
        # :1347 方向冲突门 (阈值0.70) 恒死。生成端是权威: 39 个历史 bridge 文件
        # 全候选带 confidence 字段 (含 WARN 降档后的值), 消费端重合成多余且错 — 删除。
        # 回滚: revert 本提交 (回到双合成漂移态, 不比现在更坏)。
        # 2026-08-30 第6轮F修复: `or 0.5` 会把合法 0.0 静默改写成中性放行, 与教训③(SSOT)矛盾.
        # 生成端契约值域 [0.10, 0.95], 但消费端不再二次解释语义 — None/非数 → 中性兜底,
        # 数值 → 原样透传 (含 0.0, 虽然当前生成端不可能产出).
        # 第8轮F边界: isinstance(True, (int, float)) 为真, JSON true 会被当真数 1.0 →
        # 钳到 0.95 满信心. bool 显式排除, 走中性兜底.
        _raw_conf = cand.get("confidence")
        if isinstance(_raw_conf, bool) or not isinstance(_raw_conf, (int, float)):
            confidence = 0.5
        else:
            confidence = float(_raw_conf)
        confidence = max(0.10, min(0.95, confidence))

        return {
            "symbol": symbol,
            "direction": direction,
            "final_strength": int(score * 10),
            "final_confidence": confidence,
            "consensus_level": confidence,
            "std_dev": 0.0,
            "model_scores": [],
            "is_neutral": False,
            "decision_id": cand.get("decision_id", ""),
            "strategy_id": "deep_analysis",
            "position_suggestion": cand.get("position_suggestion", {}),
            "thesis": cand.get("thesis", ""),
            "conditions": cand.get("conditions", []),
            "entry_price": cand.get("entry_price", 0),
            "stop_loss": cand.get("stop_loss", 0),
            "take_profit": cand.get("take_profit", 0),
            "entry_strategy": cand.get("entry_strategy", ""),
            # [DEB-20260901] 结构化入场语义透传 (bridge 有则带, 无则 None; record-only)
            "entry_archetype": cand.get("entry_archetype"),
            "confirmation": cand.get("confirmation"),
            "chase_cap_price": cand.get("chase_cap_price"),
            # F1 (2026-07-31): 透传 DA 锚偏离度 — 桥接 json 有则带上, 无则 None (record-only)
            "anchor_dev_pct": cand.get("anchor_dev_pct"),
            "invalidation": cand.get("invalidation", ""),
            "risks": cand.get("risks", []),
            "strategy_type": cand.get("strategy_type", ""),
            "position_size_hint": cand.get("position_size_hint", ""),
            "degradation_level": 0,
        }

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — bridge 模式不写 voting_predictions 表。"""
        pass


def _validate_bridge_schema(candidates) -> bool:
    """轻量 schema 校验: list[dict] 且每项含 symbol/direction/decision_id.

    fail-safe 语义: 非法 → 视作缺失 (走技术面降级), 不消费半损坏文件.
    """
    if not isinstance(candidates, list) or not candidates:
        return False
    for c in candidates:
        if not isinstance(c, dict):
            return False
        if not c.get("symbol") or not c.get("direction") or not c.get("decision_id"):
            return False
    return True
