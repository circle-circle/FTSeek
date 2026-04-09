"""
StrategyGraduator - 策略晋级器

功能:
1. 绩效追踪：记录每日收益率
2. 晋级条件：连续 3 天收益>5% → 发送通知
3. 降级条件：连续 2 天亏损 → 发送通知
4. 人工切换：提供 API 供用户手动切换模式
5. 钉钉通知：达到条件时推送建议

约束:
- 不自动切换模式，仅发送通知
- 钉钉通知包含建议和操作指引
- 用户通过 API 或钉钉指令手动确认
"""
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from enum import Enum
import logging

from app.v4.models.trading_models import TradingDatabase, TradingMode
from app.v4.notifier.dingtalk_v4 import DingTalkNotifierV4

logger = logging.getLogger("ftseek.v4.graduator")
logger.propagate = False  # 防止日志重复


class SuggestionType(str, Enum):
    """建议类型"""
    GRADUATE = "GRADUATE"  # 晋级
    DEMOTE = "DEMOTE"      # 降级


@dataclass
class GraduationSuggestion:
    """晋级建议"""
    code: str
    suggestion_type: SuggestionType
    current_mode: TradingMode
    suggested_mode: TradingMode
    reason: str
    consecutive_days: int
    avg_return: float
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'code': self.code,
            'suggestion_type': self.suggestion_type.value,
            'current_mode': self.current_mode.value,
            'suggested_mode': self.suggested_mode.value,
            'reason': self.reason,
            'consecutive_days': self.consecutive_days,
            'avg_return': self.avg_return,
            'created_at': self.created_at.isoformat()
        }


@dataclass
class PerformanceRecord:
    """绩效记录"""
    code: str
    date: str
    daily_return: float
    created_at: datetime = field(default_factory=datetime.now)
    
    def is_profitable(self) -> bool:
        """是否盈利"""
        return self.daily_return > 0
    
    def meets_graduation_threshold(self) -> bool:
        """是否达到晋级阈值 (>5%)"""
        return self.daily_return > 0.05
    
    def is_loss(self) -> bool:
        """是否亏损"""
        return self.daily_return < 0
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'daily_return': self.daily_return,
            'created_at': self.created_at.isoformat()
        }


class StrategyGraduator:
    """
    策略晋级器
    
    根据策略的连续绩效表现，提供晋级或降级建议。
    不自动切换模式，仅通过钉钉通知用户，由用户手动确认切换。
    """
    
    # 配置常量
    GRADUATION_THRESHOLD = 0.05  # 晋级阈值：5%
    GRADUATION_CONSECUTIVE_DAYS = 3  # 连续天数：3 天
    DEMOTION_CONSECUTIVE_DAYS = 2  # 降级连续天数：2 天
    
    def __init__(self, db_path: str, dingtalk_webhook: str = None):
        """
        初始化晋级器
        
        Args:
            db_path: 数据库路径
            dingtalk_webhook: 钉钉 webhook URL（可选）
        """
        self.db_path = db_path
        self.dingtalk_webhook = dingtalk_webhook
        self.db = TradingDatabase(db_path)
        self.notifier = DingTalkNotifierV4(
            webhook_url=dingtalk_webhook,
            enable_push=dingtalk_webhook is not None
        ) if dingtalk_webhook else None
        
        logger.info(f"StrategyGraduator 已初始化，db_path={db_path}")
    
    def record_daily_performance(
        self,
        code: str,
        date: str,
        daily_return: float
    ) -> bool:
        """
        记录每日绩效
        
        Args:
            code: 策略代码
            date: 日期（YYYY-MM-DD 格式）
            daily_return: 日收益率（小数，如 0.05 表示 5%）
            
        Returns:
            bool: 是否记录成功
        """
        try:
            # 解析日期
            if isinstance(date, str):
                date_obj = datetime.strptime(date, '%Y-%m-%d')
            else:
                date_obj = date
            
            # 检查是否已存在该日期的记录
            existing = self.db.get_stats_by_code_and_date(code, date_obj)
            
            if existing:
                # 更新现有记录
                self.db.update_performance_stats(
                    stats_id=existing.id,
                    daily_return=daily_return,
                    cumulative_return=existing.cumulative_return
                )
                logger.info(f"更新绩效记录：{code} {date} {daily_return:.2%}")
            else:
                # 计算累计收益率（最近 30 天）
                history = self.get_performance_history(code, days=30)
                cumulative_return = self._calculate_cumulative_return(history, daily_return)
                
                # 创建新记录
                self.db.create_performance_stats(
                    code=code,
                    date=date_obj,
                    daily_return=daily_return,
                    cumulative_return=cumulative_return
                )
                logger.info(f"记录绩效：{code} {date} {daily_return:.2%}")
            
            return True
            
        except Exception as e:
            logger.error(f"记录绩效失败：{code} {date} - {e}")
            return False
    
    def check_graduation(self, code: str) -> Optional[GraduationSuggestion]:
        """
        检查是否达到晋级或降级条件
        
        Args:
            code: 策略代码
            
        Returns:
            GraduationSuggestion: 晋级建议，如无建议则返回 None
        """
        # 获取当前模式
        mode_record = self.db.get_trading_mode(code)
        current_mode = mode_record.mode if mode_record else TradingMode.PAPER
        
        # 获取绩效历史（最近 30 天）
        history = self.get_performance_history(code, days=30)
        
        if not history:
            return None
        
        # 按日期排序（从近到远）
        history_sorted = sorted(
            history,
            key=lambda x: x.date,
            reverse=True
        )
        
        # 检查晋级条件：连续 3 天收益>5%
        graduation_suggestion = self._check_graduation_condition(
            code=code,
            history=history_sorted,
            current_mode=current_mode
        )
        
        if graduation_suggestion:
            return graduation_suggestion
        
        # 检查降级条件：连续 2 天亏损
        demotion_suggestion = self._check_demotion_condition(
            code=code,
            history=history_sorted,
            current_mode=current_mode
        )
        
        if demotion_suggestion:
            return demotion_suggestion
        
        return None
    
    def get_suggested_mode(self, code: str) -> str:
        """
        获取建议的交易模式
        
        Args:
            code: 策略代码
            
        Returns:
            str: 建议的模式（PAPER/HYBRID/LIVE）
        """
        suggestion = self.check_graduation(code)
        
        if suggestion:
            return suggestion.suggested_mode.value
        
        # 无建议时返回当前模式
        mode_record = self.db.get_trading_mode(code)
        if mode_record:
            return mode_record.mode.value
        
        return TradingMode.PAPER.value
    
    def switch_mode(
        self,
        code: str,
        new_mode: str,
        confirmed_by: str
    ) -> bool:
        """
        手动切换交易模式
        
        Args:
            code: 策略代码
            new_mode: 新模式（PAPER/HYBRID/LIVE）
            confirmed_by: 确认人标识
            
        Returns:
            bool: 是否切换成功
        """
        try:
            # 验证新模式
            new_mode_enum = TradingMode.from_string(new_mode)
            if not new_mode_enum:
                raise ValueError(f"Invalid trading mode: {new_mode}")
            
            # 获取当前模式
            mode_record = self.db.get_trading_mode(code)
            
            if mode_record:
                # 更新现有模式
                self.db.update_trading_mode(code, new_mode_enum)
                old_mode = mode_record.mode
            else:
                # 创建新模式
                self.db.create_trading_mode(code, new_mode_enum)
                old_mode = TradingMode.PAPER
            
            # 记录晋级历史
            reason = f"Manual switch confirmed by {confirmed_by}"
            self.db.create_graduation_history(
                code=code,
                from_mode=old_mode,
                to_mode=new_mode_enum,
                reason=reason
            )
            
            logger.info(f"模式切换：{code} {old_mode.value} -> {new_mode_enum.value} (by {confirmed_by})")
            return True
            
        except Exception as e:
            logger.error(f"切换模式失败：{code} {new_mode} - {e}")
            raise
    
    def get_performance_history(
        self,
        code: str,
        days: int = 30
    ) -> List[PerformanceRecord]:
        """
        获取绩效历史
        
        Args:
            code: 策略代码
            days: 天数（默认 30 天）
            
        Returns:
            List[PerformanceRecord]: 绩效记录列表
        """
        try:
            stats_list = self.db.get_stats_history(code, days=days)
            
            return [
                PerformanceRecord(
                    code=stats.code,
                    date=stats.date.strftime('%Y-%m-%d') if isinstance(stats.date, datetime) else str(stats.date),
                    daily_return=stats.daily_return,
                    created_at=stats.created_at
                )
                for stats in stats_list
            ]
            
        except Exception as e:
            logger.error(f"获取绩效历史失败：{code} - {e}")
            return []
    
    def _check_graduation_condition(
        self,
        code: str,
        history: List,
        current_mode: TradingMode
    ) -> Optional[GraduationSuggestion]:
        """
        检查晋级条件
        
        Args:
            code: 策略代码
            history: 绩效历史（已按日期降序排序）
            current_mode: 当前模式
            
        Returns:
            GraduationSuggestion: 晋级建议，如无则返回 None
        """
        consecutive_profit_days = 0
        total_return = 0.0
        
        for record in history:
            if record.daily_return > self.GRADUATION_THRESHOLD:
                consecutive_profit_days += 1
                total_return += record.daily_return
            else:
                break
        
        if consecutive_profit_days >= self.GRADUATION_CONSECUTIVE_DAYS:
            avg_return = total_return / consecutive_profit_days
            
            # 确定建议模式（PAPER -> HYBRID -> LIVE）
            if current_mode == TradingMode.PAPER:
                suggested_mode = TradingMode.HYBRID
            elif current_mode == TradingMode.HYBRID:
                suggested_mode = TradingMode.LIVE
            else:
                # 已在 LIVE 模式，无需晋级
                return None
            
            suggestion = GraduationSuggestion(
                code=code,
                suggestion_type=SuggestionType.GRADUATE,
                current_mode=current_mode,
                suggested_mode=suggested_mode,
                reason=f'连续{consecutive_profit_days}天收益率>{self.GRADUATION_THRESHOLD:.0%}',
                consecutive_days=consecutive_profit_days,
                avg_return=avg_return
            )
            
            # 发送钉钉通知
            self._send_dingtalk_notification(suggestion)
            
            return suggestion
        
        return None
    
    def _check_demotion_condition(
        self,
        code: str,
        history: List,
        current_mode: TradingMode
    ) -> Optional[GraduationSuggestion]:
        """
        检查降级条件
        
        Args:
            code: 策略代码
            history: 绩效历史（已按日期降序排序）
            current_mode: 当前模式
            
        Returns:
            GraduationSuggestion: 降级建议，如无则返回 None
        """
        consecutive_loss_days = 0
        total_return = 0.0
        
        for record in history:
            if record.daily_return < 0:
                consecutive_loss_days += 1
                total_return += record.daily_return
            else:
                break
        
        if consecutive_loss_days >= self.DEMOTION_CONSECUTIVE_DAYS:
            avg_return = total_return / consecutive_loss_days
            
            # 确定建议模式（LIVE -> HYBRID -> PAPER）
            if current_mode == TradingMode.LIVE:
                suggested_mode = TradingMode.HYBRID
            elif current_mode == TradingMode.HYBRID:
                suggested_mode = TradingMode.PAPER
            else:
                # 已在 PAPER 模式，返回 PAPER 建议（表示需要关注）
                suggested_mode = TradingMode.PAPER
            
            suggestion = GraduationSuggestion(
                code=code,
                suggestion_type=SuggestionType.DEMOTE,
                current_mode=current_mode,
                suggested_mode=suggested_mode,
                reason=f'连续{consecutive_loss_days}天亏损',
                consecutive_days=consecutive_loss_days,
                avg_return=avg_return
            )
            
            # 发送钉钉通知（仅当可以降级时）
            if current_mode != TradingMode.PAPER:
                self._send_dingtalk_notification(suggestion)
            
            return suggestion
        
        return None
    
    def _calculate_cumulative_return(
        self,
        history: List[PerformanceRecord],
        new_return: float
    ) -> float:
        """
        计算累计收益率
        
        Args:
            history: 历史绩效记录
            new_return: 新增的收益率
            
        Returns:
            float: 累计收益率
        """
        if not history:
            return new_return
        
        # 简单累加（实际可能需要更复杂的计算）
        total = sum(r.daily_return for r in history) + new_return
        return total
    
    def _send_dingtalk_notification(
        self,
        suggestion: GraduationSuggestion
    ) -> bool:
        """
        发送钉钉通知
        
        Args:
            suggestion: 晋级建议
            
        Returns:
            bool: 是否发送成功
        """
        if not self.notifier:
            logger.debug("钉钉通知器未配置，跳过通知")
            return False
        
        try:
            # 构建通知消息
            if suggestion.suggestion_type == SuggestionType.GRADUATE:
                action_text = "🎉 晋级建议"
                mode_action = "升级"
            else:
                action_text = "⚠️ 降级建议"
                mode_action = "降级"
            
            message = (
                f"{action_text}\n"
                f"标的：{suggestion.code}\n"
                f"当前模式：{suggestion.current_mode.value}\n"
                f"建议模式：{suggestion.suggested_mode.value}\n"
                f"原因：{suggestion.reason}\n"
                f"连续天数：{suggestion.consecutive_days}天\n"
                f"平均收益：{suggestion.avg_return:.2%}\n"
                f"\n"
                f"操作指引：\n"
                f"请确认是否{mode_action}至{suggestion.suggested_mode.value}模式\n"
                f"回复：switch {suggestion.code} {suggestion.suggested_mode.value}"
            )
            
            # 发送通知
            result = self.notifier.send(message, msg_type="text")
            
            if result.get('success'):
                logger.info(f"钉钉通知发送成功：{suggestion.code} {suggestion.suggestion_type.value}")
                return True
            else:
                logger.warning(f"钉钉通知发送失败：{result.get('error', 'Unknown error')}")
                return False
                
        except Exception as e:
            logger.error(f"发送钉钉通知失败：{suggestion.code} - {e}")
            return False
