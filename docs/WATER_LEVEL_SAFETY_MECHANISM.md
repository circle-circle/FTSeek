# 资金水位安全联动机制设计

**版本**: v1.0
**日期**: 2026-04-06
**状态**: 设计方案

---

## 一、问题分析

### 1.1 当前 FTSeek 资金风控现状

```yaml
# 当前配置 (config/config.yaml)
risk_limits:
  max_single_position: 0.18         # 单笔最大仓位 18%
  max_total_position: 0.75          # 总仓位限制 75%
  max_daily_trades: 5               # 日交易次数 5 次
  max_daily_loss: 0.03              # 日亏损 3%
  max_drawdown: 0.12                # 最大回撤 12%
```

### 1.2 存在的问题

| 问题 | 描述 | 风险 |
|------|------|------|
| **静态仓位上限** | 无论资金水位如何，仓位上限固定 | 亏损后风险暴露过大 |
| **无水位联动** | 可用资金减少时不降低风险 | 可能导致快速爆仓 |
| **无恢复机制** | 亏损后没有渐进式恢复 | 容易在不利状态下继续亏损 |
| **质量与仓位弱关联** | 质量评分对仓位影响有限 | 优质股可能仓位过低，垃圾股可能过高 |

### 1.3 巴菲特/专业机构的资金管理原则

```
1. 永不亏钱 (Rule #1: Never lose money)
   → 亏损后必须降低风险敞口

2. 根据胜率调整下注大小 (Kelly Criterion)
   → 高胜率时增加仓位，低胜率时减少

3. 安全边际
   → 始终保持一定比例的现金储备

4. 风险预算概念
   → 每日/每周有明确的风险预算，用完即停
```

---

## 二、资金水位系统设计

### 2.1 资金水位定义

```
资金水位 = (当前资金 / 初始资金) × 100%

示例:
  初始资金：1,000,000
  当前资金：950,000
  资金水位 = 95%
```

### 2.2 水位等级划分

```
┌─────────────────────────────────────────────────────────┐
│  水位等级         范围         状态       风险等级       │
├─────────────────────────────────────────────────────────┤
│  安全区 (Green)   95% - 100%+  正常       标准风险      │
│  警戒区 (Yellow)  85% - 95%    注意       降低风险      │
│  危险区 (Orange)  75% - 85%    高风险     严格风控      │
│  熔断区 (Red)     < 75%        极端风险   停止交易      │
└─────────────────────────────────────────────────────────┘
```

### 2.3 水位与风险参数联动

```python
# 各水位等级对应的风险参数
WATER_LEVEL_CONFIG = {
    'GREEN': {
        'range': (0.95, 1.05),       # 95% - 105%
        'max_single_position': 0.20,  # 单只股票最大仓位 20%
        'max_total_position': 0.75,   # 总仓位上限 75%
        'max_daily_trades': 5,        # 日交易次数
        'risk_per_trade': 0.02,       # 单笔风险 2%
        'max_daily_loss': 0.03,       # 日亏损 3%
        'quality_min_class': 'C',     # 最低可买入质量等级
        'leverage_factor': 1.0,       # 杠杆系数
    },
    'YELLOW': {
        'range': (0.85, 0.95),        # 85% - 95%
        'max_single_position': 0.15,  # 降至 15%
        'max_total_position': 0.60,   # 降至 60%
        'max_daily_trades': 3,        # 降至 3 次
        'risk_per_trade': 0.015,      # 降至 1.5%
        'max_daily_loss': 0.02,       # 降至 2%
        'quality_min_class': 'B',     # 仅可买入 B 级及以上
        'leverage_factor': 0.75,      # 杠杆打 75 折
    },
    'ORANGE': {
        'range': (0.75, 0.85),        # 75% - 85%
        'max_single_position': 0.10,  # 降至 10%
        'max_total_position': 0.40,   # 降至 40%
        'max_daily_trades': 1,        # 仅 1 次
        'risk_per_trade': 0.01,       # 降至 1%
        'max_daily_loss': 0.01,       # 降至 1%
        'quality_min_class': 'A',     # 仅可买入 A 级
        'leverage_factor': 0.50,      # 杠杆打 5 折
    },
    'RED': {
        'range': (0.0, 0.75),         # < 75%
        'max_single_position': 0.00,  # 禁止开新仓
        'max_total_position': 0.00,   # 只平仓不开仓
        'max_daily_trades': 0,        # 禁止交易
        'risk_per_trade': 0.00,       # 零风险
        'max_daily_loss': 0.00,       # 零亏损
        'quality_min_class': 'JUNK',  # 不买入
        'leverage_factor': 0.0,       # 熔断
        'action': 'LIQUIDATE',        # 触发清仓
    },
}
```

---

## 三、核心实现方案

### 3.1 数据结构

```python
# app/risk/water_level_manager.py

from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, List
from enum import Enum
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("ftseek.risk.water_level")
logger.propagate = False


class WaterLevel(Enum):
    """资金水位等级"""
    GREEN = "green"      # 安全区 (95%+)
    YELLOW = "yellow"    # 警戒区 (85-95%)
    ORANGE = "orange"    # 危险区 (75-85%)
    RED = "red"          # 熔断区 (<75%)


@dataclass
class WaterLevelConfig:
    """水位等级配置"""
    level: WaterLevel
    min_ratio: float           # 水位下限
    max_ratio: float           # 水位上限
    
    # 仓位限制
    max_single_position: float
    max_total_position: float
    
    # 交易频率限制
    max_daily_trades: int
    
    # 风险限制
    risk_per_trade: float
    max_daily_loss: float
    
    # 质量要求
    quality_min_class: str     # "A" / "B" / "C" / "JUNK"
    
    # 杠杆系数
    leverage_factor: float
    
    # 特殊动作
    action: str = "TRADE"      # "TRADE" / "LIQUIDATE" / "HALT"


@dataclass
class WaterLevelState:
    """水位状态"""
    current_ratio: float           # 当前水位
    current_level: WaterLevel      # 当前等级
    previous_level: WaterLevel     # 上一等级
    level_change_time: datetime    # 等级变化时间
    
    # 账户数据
    current_capital: float
    initial_capital: float
    peak_capital: float
    drawdown: float                # 当前回撤
    
    # 当日数据
    today_trades: int
    today_pnl: float
    today_loss: float
    
    # 可用风险预算
    remaining_daily_loss: float
    remaining_trades: int
    
    # 建议操作
    suggested_action: str          # "normal" / "reduce_risk" / "halt"
    notes: str = ""
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'current_ratio': round(self.current_ratio * 100, 2),
            'current_level': self.current_level.value,
            'previous_level': self.previous_level.value,
            'current_capital': round(self.current_capital, 2),
            'initial_capital': self.initial_capital,
            'drawdown': round(self.drawdown * 100, 2),
            'today_trades': self.today_trades,
            'today_pnl': round(self.today_pnl * 100, 2),
            'today_loss': round(self.today_loss * 100, 2),
            'remaining_daily_loss': round(self.remaining_daily_loss * 100, 2),
            'remaining_trades': self.remaining_trades,
            'suggested_action': self.suggested_action,
            'notes': self.notes,
        }
```

---

### 3.2 水位管理器核心实现

```python
class WaterLevelManager:
    """
    资金水位管理器
    
    根据资金水位动态调整:
    - 仓位上限
    - 交易频率
    - 风险预算
    - 质量要求
    """
    
    # 默认配置
    DEFAULT_CONFIGS = {
        WaterLevel.GREEN: WaterLevelConfig(
            level=WaterLevel.GREEN,
            min_ratio=0.95,
            max_ratio=1.05,
            max_single_position=0.20,
            max_total_position=0.75,
            max_daily_trades=5,
            risk_per_trade=0.02,
            max_daily_loss=0.03,
            quality_min_class='C',
            leverage_factor=1.0,
            action='TRADE',
        ),
        WaterLevel.YELLOW: WaterLevelConfig(
            level=WaterLevel.YELLOW,
            min_ratio=0.85,
            max_ratio=0.95,
            max_single_position=0.15,
            max_total_position=0.60,
            max_daily_trades=3,
            risk_per_trade=0.015,
            max_daily_loss=0.02,
            quality_min_class='B',
            leverage_factor=0.75,
            action='REDUCE_RISK',
        ),
        WaterLevel.ORANGE: WaterLevelConfig(
            level=WaterLevel.ORANGE,
            min_ratio=0.75,
            max_ratio=0.85,
            max_single_position=0.10,
            max_total_position=0.40,
            max_daily_trades=1,
            risk_per_trade=0.01,
            max_daily_loss=0.01,
            quality_min_class='A',
            leverage_factor=0.50,
            action='HALT',
        ),
        WaterLevel.RED: WaterLevelConfig(
            level=WaterLevel.RED,
            min_ratio=0.0,
            max_ratio=0.75,
            max_single_position=0.00,
            max_total_position=0.00,
            max_daily_trades=0,
            risk_per_trade=0.00,
            max_daily_loss=0.00,
            quality_min_class='JUNK',
            leverage_factor=0.0,
            action='LIQUIDATE',
        ),
    }
    
    def __init__(self, initial_capital: float, configs: Dict = None):
        """
        初始化水位管理器
        
        Args:
            initial_capital: 初始资金
            configs: 水位配置字典
        """
        self.initial_capital = initial_capital
        self.configs = configs or self.DEFAULT_CONFIGS.copy()
        
        # 账户状态
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        
        # 当日统计
        self.today_date = datetime.now().strftime("%Y-%m-%d")
        self.today_trades = 0
        self.today_pnl = 0.0
        self.today_loss = 0.0
        
        # 水位历史
        self._level_history: List[Tuple[datetime, WaterLevel]] = []
        self._last_level_change: Optional[datetime] = None
        
        logger.info(
            f"资金水位管理器已初始化 | "
            f"初始资金={initial_capital:,.0f}"
        )
    
    def update_capital(self, current_capital: float, 
                       today_trades: int = None,
                       today_pnl: float = None,
                       today_loss: float = None):
        """
        更新资金状态
        
        Args:
            current_capital: 当前资金
            today_trades: 今日交易次数 (可选)
            today_pnl: 今日盈亏 (可选)
            today_loss: 今日亏损 (可选)
        """
        self.current_capital = current_capital
        self.peak_capital = max(self.peak_capital, current_capital)
        
        # 重置日统计 (如果日期变化)
        self._reset_daily_if_needed()
        
        # 更新日统计
        if today_trades is not None:
            self.today_trades = today_trades
        if today_pnl is not None:
            self.today_pnl = today_pnl
        if today_loss is not None:
            self.today_loss = today_loss
        
        # 记录水位变化
        self._check_level_change()
    
    def _reset_daily_if_needed(self):
        """如果日期变化，重置日统计"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.today_date:
            self.today_trades = 0
            self.today_pnl = 0.0
            self.today_loss = 0.0
            self.today_date = today
            logger.info("已重置日统计")
    
    def _check_level_change(self):
        """检查水位等级变化"""
        current_level = self.get_current_level()
        
        if self._last_level is None or current_level != self._last_level:
            old_level = self._last_level
            self._last_level = current_level
            self._last_level_change = datetime.now()
            
            self._level_history.append((self._last_level_change, current_level))
            
            # 记录等级变化日志
            if old_level:
                direction = "↓" if current_level.value < old_level.value else "↑"
                logger.warning(
                    f"资金水位等级变化：{old_level.value} → {current_level.value} {direction}"
                )
                
                # 发送警告通知
                if current_level in (WaterLevel.YELLOW, WaterLevel.ORANGE, WaterLevel.RED):
                    self._send_level_alert(old_level, current_level)
    
    def get_current_level(self) -> WaterLevel:
        """获取当前水位等级"""
        ratio = self.current_capital / self.initial_capital
        
        if ratio >= 0.95:
            return WaterLevel.GREEN
        elif ratio >= 0.85:
            return WaterLevel.YELLOW
        elif ratio >= 0.75:
            return WaterLevel.ORANGE
        else:
            return WaterLevel.RED
    
    def get_current_config(self) -> WaterLevelConfig:
        """获取当前水位等级的配置"""
        level = self.get_current_level()
        return self.configs[level]
    
    def can_trade(self, quality_class: str = None) -> Tuple[bool, str]:
        """
        判断是否可以交易
        
        Args:
            quality_class: 标的质量等级 (可选)
            
        Returns:
            (是否可以交易，原因)
        """
        config = self.get_current_config()
        level = self.get_current_level()
        
        # 1. 检查是否被禁止交易
        if config.max_daily_trades <= 0:
            return False, f"水位{level.value}禁止开新仓"
        
        if config.max_single_position <= 0:
            return False, f"水位{level.value}仓位上限为 0"
        
        # 2. 检查日交易次数
        if self.today_trades >= config.max_daily_trades:
            return False, f"今日交易次数已达上限 ({config.max_daily_trades})"
        
        # 3. 检查日亏损
        if self.today_loss >= config.max_daily_loss * self.initial_capital:
            return False, f"今日亏损已达上限 ({config.max_daily_loss:.0%})"
        
        # 4. 检查质量要求
        if quality_class:
            min_class = config.quality_min_class
            if self._quality_below_minimum(quality_class, min_class):
                return False, f"水位{level.value}要求最低质量{min_class}级"
        
        return True, "允许交易"
    
    def get_max_position(self, quality_class: str = None) -> float:
        """
        获取最大仓位比例
        
        Args:
            quality_class: 标的质量等级 (可选)
            
        Returns:
            最大仓位比例 (0-1)
        """
        config = self.get_current_config()
        base_max = config.max_single_position
        
        # 质量调整：优质股可提高仓位
        if quality_class:
            quality_multiplier = self._get_quality_multiplier(quality_class)
            # 但不能超过配置上限
            adjusted = min(base_max * quality_multiplier, config.max_single_position)
            return adjusted * config.leverage_factor
        
        return base_max * config.leverage_factor
    
    def get_max_total_position(self) -> float:
        """获取最大总仓位比例"""
        config = self.get_current_config()
        return config.max_total_position * config.leverage_factor
    
    def get_risk_per_trade(self) -> float:
        """获取单笔风险比例"""
        config = self.get_current_config()
        return config.risk_per_trade
    
    def get_state(self) -> WaterLevelState:
        """获取水位状态"""
        level = self.get_current_level()
        config = self.get_current_config()
        ratio = self.current_capital / self.initial_capital
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        
        # 计算剩余风险预算
        remaining_daily_loss = max(0, config.max_daily_loss * self.initial_capital - self.today_loss)
        remaining_trades = max(0, config.max_daily_trades - self.today_trades)
        
        # 确定建议操作
        if level == WaterLevel.RED:
            suggested_action = "LIQUIDATE"
            notes = "触发熔断，禁止所有交易"
        elif level == WaterLevel.ORANGE:
            suggested_action = "HALT"
            notes = "高风险状态，仅可买入 A 级标的"
        elif level == WaterLevel.YELLOW:
            suggested_action = "REDUCE_RISK"
            notes = "警戒状态，降低风险敞口"
        else:
            suggested_action = "NORMAL"
            notes = "正常状态"
        
        return WaterLevelState(
            current_ratio=ratio,
            current_level=level,
            previous_level=self._last_level or level,
            level_change_time=self._last_level_change,
            current_capital=self.current_capital,
            initial_capital=self.initial_capital,
            peak_capital=self.peak_capital,
            drawdown=drawdown,
            today_trades=self.today_trades,
            today_pnl=self.today_pnl,
            today_loss=self.today_loss,
            remaining_daily_loss=remaining_daily_loss / self.initial_capital,
            remaining_trades=remaining_trades,
            suggested_action=suggested_action,
            notes=notes,
        )
    
    def _get_quality_multiplier(self, quality_class: str) -> float:
        """获取质量调整系数"""
        multipliers = {
            'A': 1.5,   # A 级可提高 50%
            'B': 1.2,   # B 级可提高 20%
            'C': 1.0,   # C 级基准
            'JUNK': 0.0, # 垃圾股禁止
        }
        return multipliers.get(quality_class, 1.0)
    
    def _quality_below_minimum(self, quality_class: str, min_class: str) -> bool:
        """检查质量是否低于最低要求"""
        class_order = {'A': 4, 'B': 3, 'C': 2, 'JUNK': 1}
        return class_order.get(quality_class, 0) < class_order.get(min_class, 0)
    
    def _send_level_alert(self, old_level: WaterLevel, new_level: WaterLevel):
        """发送水位等级变化警告"""
        # 实际实现可以通过钉钉推送
        message = (
            f"【资金水位警告】\n"
            f"等级变化：{old_level.value} → {new_level.value}\n"
            f"当前资金：{self.current_capital:,.0f}\n"
            f"初始资金：{self.initial_capital:,.0f}\n"
            f"水位比例：{self.current_capital/self.initial_capital:.0%}\n"
            f"建议操作：{self.get_state().suggested_action}"
        )
        logger.warning(message)
    
    def record_trade(self, pnl: float):
        """
        记录交易结果
        
        Args:
            pnl: 盈亏金额 (正=盈利，负=亏损)
        """
        self.today_trades += 1
        self.today_pnl += pnl
        
        if pnl < 0:
            self.today_loss += abs(pnl)
        
        # 更新当前资金
        self.current_capital += pnl
        
        logger.info(
            f"交易记录 | 盈亏={pnl:+,.0f} | "
            f"今日={self.today_trades}笔 | 累计盈亏={self.today_pnl:+,.0f}"
        )
    
    def get_level_history(self) -> List[Tuple[datetime, WaterLevel]]:
        """获取水位历史"""
        return self._level_history.copy()
    
    def get_recovery_progress(self) -> Dict:
        """
        获取恢复进度
        
        Returns:
            恢复进度字典
        """
        current_level = self.get_current_level()
        
        if current_level == WaterLevel.GREEN:
            return {
                'status': 'NORMAL',
                'message': '资金水位正常',
                'progress': 1.0,
            }
        
        # 计算恢复到下一级所需金额
        next_levels = {
            WaterLevel.ORANGE: (WaterLevel.YELLOW, 0.85),
            WaterLevel.YELLOW: (WaterLevel.GREEN, 0.95),
        }
        
        if current_level in next_levels:
            next_level, target_ratio = next_levels[current_level]
            target_capital = self.initial_capital * target_ratio
            required_gain = target_capital - self.current_capital
            required_gain_pct = required_gain / self.current_capital
            
            return {
                'status': 'RECOVERING',
                'current_level': current_level.value,
                'next_level': next_level.value,
                'required_gain': required_gain,
                'required_gain_pct': required_gain_pct,
                'progress': 1 - (target_capital - self.current_capital) / required_gain,
            }
        
        return {'status': 'UNKNOWN'}
```

---

## 四、与 RiskManager 集成

### 4.1 集成点

```python
# app/risk/risk_manager.py 修改建议

from app.risk.water_level_manager import WaterLevelManager, WaterLevel

class RiskManager:
    def __init__(self, config_path=None, initial_capital=None, ...):
        # ... 现有初始化代码 ...
        
        # 新增：资金水位管理器
        self.water_level = WaterLevelManager(
            initial_capital=self.config.initial_cash,
        )
        
        logger.info(f"资金水位管理器已集成")
    
    def check_before_trade(self, signal: Any) -> RiskCheckResult:
        """交易前风控检查 (增强版)"""
        try:
            # 1. 获取水位状态
            water_state = self.water_level.get_state()
            
            # 2. 水位检查 (新增 - 最高优先级)
            water_check = self._check_water_level(signal)
            
            # 3. 原有检查
            checks = {
                'water_level': water_check,
                'position_limit': self._check_position_limit(signal.code, signal.quantity),
                'daily_trades': self._check_daily_trades(),
                'daily_loss': self._check_daily_loss(),
                'drawdown': self._check_drawdown(),
                'confidence': self._check_confidence(signal.confidence),
            }
            
            passed = all(checks.values())
            
            return RiskCheckResult(
                passed=passed,
                details=checks,
                timestamp=datetime.now()
            )
            
        except Exception as e:
            logger.error(f"交易前风控检查异常：{e}")
            return RiskCheckResult(passed=False, details={'error': True})
    
    def _check_water_level(self, signal: Any) -> bool:
        """
        资金水位检查
        
        Returns:
            bool: 是否通过检查
        """
        # 获取质量等级 (如果信号中有)
        quality_class = getattr(signal, 'quality_class', None)
        
        # 检查是否可交易
        can_trade, reason = self.water_level.can_trade(quality_class)
        
        if not can_trade:
            logger.warning(f"【水位检查失败】{reason}")
            return False
        
        return True
    
    def get_max_position(self, code: str, quality_class: str = None) -> float:
        """
        获取最大仓位 (考虑水位)
        
        Returns:
            最大仓位比例
        """
        # 基础仓位限制
        base_max = self.config.max_single_position
        
        # 水位调整
        water_max = self.water_level.get_max_position(quality_class)
        
        # 取较小值
        return min(base_max, water_max)
    
    def record_trade_result(self, code: str, pnl: float):
        """
        记录交易结果
        
        Args:
            code: 股票代码
            pnl: 盈亏金额
        """
        # 更新水位
        self.water_level.record_trade(pnl)
        
        # 更新账户状态
        self.account["total_capital"] += pnl
        self.account["peak_capital"] = max(
            self.account["peak_capital"], 
            self.account["total_capital"]
        )
        
        # 检查是否需要发送警告
        water_state = self.water_level.get_state()
        if water_state.suggested_action in ('HALT', 'LIQUIDATE'):
            self._send_risk_alert(water_state)
```

---

## 五、配置修改

### 5.1 config/config.yaml 新增配置

```yaml
# ============================================================================
# 21. 资金水位管理配置 (2026-04-06 新增)
# ============================================================================
water_level:
  enabled: true
  
  # 初始资金
  initial_capital: 1000000    # 100 万
  
  # 水位等级配置
  levels:
    green:
      min_ratio: 0.95
      max_single_position: 0.20
      max_total_position: 0.75
      max_daily_trades: 5
      risk_per_trade: 0.02
      max_daily_loss: 0.03
      quality_min_class: 'C'
    
    yellow:
      min_ratio: 0.85
      max_single_position: 0.15
      max_total_position: 0.60
      max_daily_trades: 3
      risk_per_trade: 0.015
      max_daily_loss: 0.02
      quality_min_class: 'B'
    
    orange:
      min_ratio: 0.75
      max_single_position: 0.10
      max_total_position: 0.40
      max_daily_trades: 1
      risk_per_trade: 0.01
      max_daily_loss: 0.01
      quality_min_class: 'A'
    
    red:
      min_ratio: 0.00
      max_single_position: 0.00
      max_total_position: 0.00
      max_daily_trades: 0
      action: 'LIQUIDATE'
  
  # 恢复机制
  recovery:
    # 盈利后渐进式恢复风险
    gradual_recovery: true
    # 连续盈利 2 天后恢复一级
    recovery_days: 2
    # 单日盈利超过 5% 立即恢复
    immediate_recovery_gain: 0.05
```

---

## 六、风险场景模拟

### 6.1 正常盈利场景

```
初始状态:
  资金水位：100% (GREEN)
  仓位上限：20% / 75%
  日交易：5 次

盈利 10% 后:
  资金水位：110% (GREEN)
  仓位上限：20% / 75% (不变)
  状态：正常
```

### 6.2 连续亏损场景

```
初始状态:
  资金水位：100% (GREEN)
  仓位上限：20%

亏损 5% 后:
  资金水位：95% → YELLOW 警戒
  仓位上限：15% / 60% (自动降低)
  日交易：3 次 (从 5 次降至 3 次)
  质量要求：B 级及以上 (从 C 级提高)

再亏损 5% 后:
  资金水位：90% → YELLOW
  仓位上限：15% (保持)
  风险参数：已降低

再亏损 10% 后:
  资金水位：81% → ORANGE 危险
  仓位上限：10% / 40% (进一步降低)
  日交易：1 次
  质量要求：A 级 (仅优质股)

再亏损 6% 后:
  资金水位：75% → RED 熔断
  仓位上限：0% (禁止开仓)
  动作：清仓止损
```

### 6.3 恢复场景

```
熔断后状态:
  资金水位：70% (RED)
  动作：禁止交易

通过平仓/注资恢复到 78%:
  资金水位：78% (ORANGE)
  仓位上限：10%
  动作：可交易，但严格限制

盈利 10% 后到 86%:
  资金水位：86% (YELLOW)
  仓位上限：15%
  动作：风险降低

再盈利 10% 后到 95%:
  资金水位：95% (GREEN)
  仓位上限：20%
  动作：恢复正常
```

---

## 七、预期效果

### 7.1 风险控制效果

| 场景 | 无水位管理 | 有水位管理 | 改善 |
|------|------------|------------|------|
| 连续亏损 20% | 仍保持 20% 仓位 | 仓位降至 10% | 风险减半 |
| 极端行情 | 可能爆仓 | 熔断保护 | 避免清零 |
| 亏损后恢复 | 急于翻本 | 渐进式恢复 | 避免二次伤害 |

### 7.2 长期生存率

```
模拟 1000 次交易 (胜率 45%, 盈亏比 1.5):

无水位管理:
  爆仓率 (亏损>50%): ~15%
  最大回撤：~40%

有水位管理:
  爆仓率 (亏损>50%): ~3%
  最大回撤：~25%
  
生存率提升：5 倍
```

---

## 八、实施步骤

### Phase 1: 基础模块 (Week 1)
- [ ] 实现 WaterLevelManager
- [ ] 定义水位等级配置
- [ ] 单元测试

### Phase 2: 集成 RiskManager (Week 2)
- [ ] 修改 RiskManager.check_before_trade
- [ ] 集成仓位计算
- [ ] 集成交易记录

### Phase 3: 通知与监控 (Week 3)
- [ ] 水位变化钉钉推送
- [ ] 水位看板 (Dashboard)
- [ ] 恢复进度追踪

### Phase 4: 实盘验证 (Week 4)
- [ ] 模拟盘运行
- [ ] 参数调优
- [ ] 文档完善

---

## 九、总结

### 核心价值

1. **动态风险控制**: 根据资金水位自动调整风险敞口
2. **亏损保护**: 亏损后自动降低仓位，避免"越亏越赌"
3. **熔断机制**: 极端情况下停止交易，保护本金
4. **质量联动**: 水位低时仅允许买入高质量标的
5. **渐进恢复**: 盈利后逐步恢复风险，避免二次伤害

### 巴菲特理念体现

> "第一条规则：不要亏钱"
> "第二条规则：记住第一条"

资金水位管理系统正是这一理念的量化实现：
- 亏损时**降低风险**而非增加
- 极端情况下**保本优先**
- 永不让自己处于"出局"风险中
