"""
午间 LLM 分析师 (Midday LLM Analyst)

在午间休市时段 (12:00-13:00 HKT) 执行：
1. 分析上午交易表现
2. 读取最新市场快照
3. 生成午间市场判断和作战计划调整
4. 写入 feedback relay 供盘后复盘和次日使用

架构:
  上午交易数据 + 市场快照 → MiddayAnalyst → LLM 分析 → midday_update.json
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek.pipeline.midday_analyst")

# 反馈文件路径
FEEDBACK_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "hk_feedback"
MIDDAY_UPDATE_FILE = FEEDBACK_DIR / "midday_update.json"

# ============================================================
#  午间评估结果
# ============================================================
@dataclass
class MiddayAssessment:
    """午间 LLM 分析结果"""
    
    session: str = "midday"
    market_regime: str = "NEUTRAL"       # RISK_ON / NEUTRAL / RISK_OFF
    morning_summary: str = ""            # 上午市场总结
    key_observations: List[str] = field(default_factory=list)
    
    # 交易表现评估
    trade_performance: Dict[str, Any] = field(default_factory=dict)
    pnl_assessment: str = ""             # 盈亏评估
    execution_quality: str = ""          # 执行质量
    
    # 下午作战调整
    afternoon_strategy: str = ""         # 下午策略
    focus_symbols: List[str] = field(default_factory=list)
    risk_adjustments: List[str] = field(default_factory=list)
    position_recommendations: List[Dict] = field(default_factory=list)
    
    # 置信度
    confidence: float = 0.0
    raw_response: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    trace_id: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "session": self.session,
            "market_regime": self.market_regime,
            "morning_summary": self.morning_summary,
            "key_observations": self.key_observations,
            "trade_performance": self.trade_performance,
            "pnl_assessment": self.pnl_assessment,
            "execution_quality": self.execution_quality,
            "afternoon_strategy": self.afternoon_strategy,
            "focus_symbols": self.focus_symbols,
            "risk_adjustments": self.risk_adjustments,
            "position_recommendations": self.position_recommendations,
            "confidence": round(self.confidence, 3),
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ============================================================
#  System Prompt
# ============================================================
MIDDAY_SYSTEM_PROMPT = """你是 FTSeek 港股系统的午间策略分析师。

现在是午间休市时段 (12:00-13:00 HKT)，你需要：
1. 评估上午交易表现
2. 分析当前市场状态
3. 给出下午交易策略调整建议

【港股午间特异性】:
- A股午间休市对港股联动影响
- 南向资金上午流向
- 上午量价特征
- 早盘信号验证情况

【分析框架】:
1. 上午盈亏归因: 哪些交易赚钱/亏钱？原因是什么？
2. 市场状态判断: 风险偏好是否变化？
3. 计划执行度: 盘前计划是否被正确执行？
4. 下午策略: 需要加仓/减仓/观望？重点关注哪些标的？

【强制输出 JSON 格式】:
{
  "market_regime": "RISK_ON|NEUTRAL|RISK_OFF",
  "morning_summary": "上午市场总结 (50字内)",
  "key_observations": ["观察1", "观察2", "观察3"],
  "trade_performance": {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0},
  "pnl_assessment": "盈亏评估 (30字内)",
  "execution_quality": "执行质量评估 (30字内)",
  "afternoon_strategy": "下午策略建议 (50字内)",
  "focus_symbols": ["HK.XXXX", ...],
  "risk_adjustments": ["风险调整1", ...],
  "position_recommendations": [{"symbol": "HK.XXXX", "action": "HOLD|ADD|REDUCE|CLOSE", "reason": "..."}],
  "confidence": 0.0-1.0
}

强制规范:
1. 仅输出 JSON，不要额外文字
2. 股票代码必须带 HK. 前缀
3. 如果上午无交易，trade_performance 填默认值
4. confidence 根据数据充分性判断
"""


# ============================================================
#  MiddayAnalyst
# ============================================================
@dataclass
class MiddayAnalyst:
    """午间 LLM 分析师
    
    使用 UnifiedLLMService 进行统一调用和统计
    """
    
    config: Dict = field(default_factory=dict)
    model: str = "qwen3.6-plus"        # 午间分析使用中等模型
    timeout: int = 90
    max_retries: int = 1
    
    def analyze(
        self, 
        morning_trades: List[Dict] = None,
        morning_positions: List[Dict] = None,
        market_snapshot: Dict = None,
        battle_plan: Dict = None,
    ) -> MiddayAssessment:
        """执行午间分析
        
        Args:
            morning_trades: 上午交易列表
            morning_positions: 当前持仓列表
            market_snapshot: 市场快照数据
            battle_plan: 盘前作战计划
            
        Returns:
            MiddayAssessment: 午间评估结果
        """
        morning_trades = morning_trades or []
        morning_positions = morning_positions or []
        market_snapshot = market_snapshot or {}
        battle_plan = battle_plan or {}
        
        logger.info(
            f"[MiddayAnalyst] 开始午间分析: "
            f"trades={len(morning_trades)}, positions={len(morning_positions)}"
        )
        
        # 构建 prompt
        prompt = self._build_prompt(
            morning_trades, morning_positions, market_snapshot, battle_plan
        )
        messages = [
            {"role": "system", "content": MIDDAY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        
        # 调用 LLM
        try:
            from app.llm.unified_service import get_unified_llm, UnifiedLLMService
            
            svc = get_unified_llm(
                default_model=self.model, 
                default_timeout=self.timeout, 
                max_retries=self.max_retries
            )
            
            result_text = UnifiedLLMService.run_sync(
                svc.call(
                    messages, 
                    model=self.model, 
                    timeout=self.timeout,
                    task_type="midday_analysis"
                ),
                timeout=self.timeout + 30,
            )
            
            if result_text:
                return self._parse_response(result_text)
            else:
                logger.warning("[MiddayAnalyst] LLM 返回空结果")
                return self._make_fallback("LLM 返回空结果")
                
        except Exception as e:
            logger.error(f"[MiddayAnalyst] 午间分析失败: {e}")
            return self._make_fallback(f"LLM 调用失败: {e}")
    
    def _build_prompt(
        self, 
        trades: List[Dict], 
        positions: List[Dict], 
        snapshot: Dict,
        plan: Dict
    ) -> str:
        """构建午间分析 prompt"""
        
        parts = []
        
        # 上午交易总结
        if trades:
            parts.append("## 上午交易记录")
            for t in trades[:10]:  # 最多10条
                parts.append(
                    f"- {t.get('code', '?')}: {t.get('direction', '?')} "
                    f"PnL={t.get('pnl', t.get('realized_pnl', 0)):.0f} "
                    f"持仓={t.get('hold_minutes', '?')}分钟"
                )
        else:
            parts.append("## 上午交易记录\n(无交易)")
        
        # 当前持仓
        if positions:
            parts.append("\n## 当前持仓")
            for p in positions[:10]:
                parts.append(
                    f"- {p.get('code', '?')}: {p.get('quantity', 0)}股 "
                    f"未实现PnL={p.get('unrealized_pnl', 0):.0f}"
                )
        else:
            parts.append("\n## 当前持仓\n(无持仓)")
        
        # 市场快照
        if snapshot:
            parts.append("\n## 市场快照")
            for k, v in list(snapshot.items())[:15]:
                if isinstance(v, dict):
                    chg = v.get("change_pct", 0)
                    parts.append(f"- {k}: {v.get('last_price', '?')} ({chg:+.2%})")
                else:
                    parts.append(f"- {k}: {v}")
        else:
            parts.append("\n## 市场快照\n(无实时数据)")
        
        # 盘前计划对比
        plan_targets = plan.get("targets", [])
        if plan_targets:
            parts.append("\n## 盘前作战计划")
            for t in plan_targets[:5]:
                parts.append(
                    f"- {t.get('symbol', '?')}: {t.get('direction', '?')} "
                    f"紧迫度={t.get('urgency', '?')}"
                )
        else:
            parts.append("\n## 盘前作战计划\n(无计划)")
        
        return "\n".join(parts)
    
    def _parse_response(self, raw: str) -> MiddayAssessment:
        """解析 LLM 响应"""
        from app.llm.unified_service import UnifiedLLMService
        
        data = UnifiedLLMService.extract_json(raw)
        
        if not data:
            return self._make_fallback("JSON 解析失败", raw)
        
        assessment = MiddayAssessment(
            session=data.get("session", "midday"),
            market_regime=data.get("market_regime", "NEUTRAL"),
            morning_summary=data.get("morning_summary", ""),
            key_observations=data.get("key_observations", []),
            trade_performance=data.get("trade_performance", {}),
            pnl_assessment=data.get("pnl_assessment", ""),
            execution_quality=data.get("execution_quality", ""),
            afternoon_strategy=data.get("afternoon_strategy", ""),
            focus_symbols=data.get("focus_symbols", []),
            risk_adjustments=data.get("risk_adjustments", []),
            position_recommendations=data.get("position_recommendations", []),
            confidence=float(data.get("confidence", 0.0)),
            raw_response=raw,
            trace_id=f"midday-{datetime.now().strftime('%Y%m%d-%H%M')}",
        )
        
        logger.info(
            f"[MiddayAnalyst] 分析完成: regime={assessment.market_regime} "
            f"confidence={assessment.confidence:.2f} "
            f"focus={assessment.focus_symbols}"
        )
        
        return assessment
    
    def _make_fallback(self, reason: str, raw: str = None) -> MiddayAssessment:
        """创建降级结果"""
        return MiddayAssessment(
            session="midday",
            market_regime="NEUTRAL",
            morning_summary=f"午间分析降级: {reason}",
            confidence=0.0,
            raw_response=raw,
        )
    
    def save_result(self, assessment: MiddayAssessment) -> bool:
        """保存午间分析结果到 feedback relay，并注入 BattlePlanManager + 作战计划"""
        try:
            FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

            # 写入 midday_update.json
            update_data = {
                "timestamp": datetime.now().isoformat(),
                "assessment": assessment.to_dict(),
            }

            # Atomic write
            temp_file = MIDDAY_UPDATE_FILE.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(update_data, f, ensure_ascii=False, indent=2)
            temp_file.rename(MIDDAY_UPDATE_FILE)

            logger.info(f"[MiddayAnalyst] 结果已保存: {MIDDAY_UPDATE_FILE}")

            # ── 注入 BattlePlanManager 午间推荐缓存 ──
            try:
                from app.pipeline.battle_plan import BattlePlanManager
                bpm = BattlePlanManager()
                recs = {}
                for r in assessment.position_recommendations:
                    sym = r.get("symbol", "")
                    action = r.get("action", "HOLD").upper()
                    if sym and sym.startswith("HK.") and action in ("HOLD", "ADD", "REDUCE", "CLOSE"):
                        recs[sym] = action
                if recs:
                    bpm.update_midday_recommendations(recs)
                    logger.info(f"[MiddayAnalyst] 已注入 {len(recs)} 只标的到 BattlePlanManager")
            except Exception as _inject_err:
                logger.warning(f"[MiddayAnalyst] 注入 BattlePlanManager 失败 (非阻塞): {_inject_err}")

            # ── 注入 daily_battle_plan.json (Phase 2: 午间 LLM 标的进入交易管线) ──
            try:
                from pathlib import Path as _Path
                _plan_path = _Path(__file__).resolve().parent.parent.parent / "data" / "daily_battle_plan.json"
                if _plan_path.exists():
                    with open(_plan_path, 'r', encoding='utf-8') as _f:
                        _plan = json.load(_f)

                    _existing = {t.get('symbol') for t in _plan.get('targets', [])}
                    _new = []

                    # 1. focus_symbols 注入
                    for sym in assessment.focus_symbols:
                        if sym and sym.startswith("HK.") and sym not in _existing:
                            _new.append({
                                "symbol": sym,
                                "direction": "ANY",
                                "status": "VALID",
                                "urgency": "High" if assessment.confidence > 0.6 else "Medium",
                                "key_trigger": f"[午间LLM] {assessment.afternoon_strategy[:40]}",
                                "max_position_pct": 0.03,
                                "source": "midday_llm_focus",
                            })

                    # 2. position_recommendations 注入 (ADD/REDUCE/CLOSE 操作)
                    for r in assessment.position_recommendations:
                        sym = r.get("symbol", "")
                        action = r.get("action", "HOLD").upper()
                        if sym and sym.startswith("HK.") and action in ("ADD", "REDUCE", "CLOSE"):
                            if sym not in _existing and sym not in {t["symbol"] for t in _new}:
                                _new.append({
                                    "symbol": sym,
                                    "direction": "ANY",
                                    "status": "VALID",
                                    "urgency": "High" if action == "ADD" else "Medium",
                                    "key_trigger": f"[午间LLM操作建议] {action} ({r.get('reason', '')[:30]})",
                                    "max_position_pct": 0.03,
                                    "source": "midday_llm_position",
                                })

                    if _new:
                        _plan["targets"].extend(_new)
                        _plan["plan_version"] = _plan.get("plan_version", 1) + 1
                        _plan["midday_llm_injected_at"] = datetime.now().isoformat()
                        _plan["midday_llm_added"] = [t["symbol"] for t in _new]

                        with open(_plan_path, 'w', encoding='utf-8') as _f:
                            json.dump(_plan, _f, ensure_ascii=False, indent=2)

                        # 热重载 BPM
                        from app.pipeline.battle_plan import BattlePlanManager
                        BattlePlanManager().initialize(str(_plan_path), enabled=True)

                        logger.info(
                            f"[MiddayAnalyst] 午间 LLM 标的已注入 plan: {len(_new)} 只 "
                            f"({[t['symbol'] for t in _new]})"
                        )
            except Exception as _plan_err:
                logger.warning(f"[MiddayAnalyst] 注入 plan 失败 (非阻塞): {_plan_err}")

            return True

        except Exception as e:
            logger.error(f"[MiddayAnalyst] 保存失败: {e}")
            return False


# ============================================================
#  便捷函数
# ============================================================
def run_midday_analysis(
    trades: List[Dict] = None,
    positions: List[Dict] = None,
    market_snapshot: Dict = None,
    battle_plan: Dict = None,
) -> MiddayAssessment:
    """运行午间分析并保存结果
    
    Args:
        trades: 上午交易列表
        positions: 当前持仓列表
        market_snapshot: 市场快照
        battle_plan: 盘前作战计划
        
    Returns:
        MiddayAssessment: 午间评估结果
    """
    analyst = MiddayAnalyst()
    
    assessment = analyst.analyze(
        morning_trades=trades,
        morning_positions=positions,
        market_snapshot=market_snapshot,
        battle_plan=battle_plan,
    )
    
    # 保存结果
    analyst.save_result(assessment)
    
    return assessment
