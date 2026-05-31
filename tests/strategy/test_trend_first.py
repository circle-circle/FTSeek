"""RFC-054: Trend-First 策略单元测试

覆盖四层过滤架构:
  Layer 0: 防御链 (数据完整性 + 闪崩检测)
  Layer 1: 环境判断 (仅 TREND_UP / TREND_DOWN 时激活)
  Layer 2: 选股评分 (板块热度 + 龙头加分 + 资金流修正)
  Layer 3: 双模式入场 (PULLBACK 回调 / BREAKOUT 突破)
"""
import pytest
from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure.trend_first import evaluate


def _make_ctx(**overrides):
    """创建测试用 SymbolContext，默认值满足所有层通过条件"""
    defaults = {
        'symbol': 'US.AAPL',
        'rsi': 45.0,
        'atr': 2.5,
        'market_regime': 'TREND_UP',
        'trend': 'UP',
        'volume_ratio': 1.5,
        'slope_5m': 0.3,
        'order_flow_imbalance': 0.1,
        'ticker_buy_sell_ratio': 1.2,
        'high_distance_pct': -1.5,
        'spy_ma20': 430.0,
        'spy_ma50': 425.0,
    }
    defaults.update(overrides)

    ctx = SymbolContext()
    for k, v in defaults.items():
        if hasattr(ctx, k):
            setattr(ctx, k, v)

    # metadata for sector_info
    if 'metadata' not in defaults:
        ctx.metadata = {}
    sector_info = overrides.get('sector_info', {
        'sector_score': 60, 'is_leader': True, 'sector_name': 'Technology'
    })
    ctx.metadata['sector_info'] = sector_info

    return ctx


def _default_cfg():
    """标准测试配置（与 trend_first 策略 YAML 对齐）"""
    return {
        'enabled': True,
        'observe_only': True,
        'min_sector_score': 40,
        'leader_bonus': 15,
        'overheat_threshold': 80,
        'overheat_penalty': 0.8,
        'sector_max_age_hours': 2,
        'pullback_rsi_long': [35, 55],
        'pullback_rsi_short': [45, 65],
        'breakout_min_volume_ratio': 2.0,
        'stop_loss_atr_mult': 1.5,
        'breakout_stop_loss_atr_mult': 1.0,
        'trail_activation_atr': 1.0,
        'trail_atr_mult': 1.5,
        'min_score': 55,
    }


# ═══════════════════════════════════════════════════════════════════
#  Layer 0: 防御链
# ═══════════════════════════════════════════════════════════════════

class TestLayer0Defense:
    """Layer 0: 防御链 — 数据完整性 + 闪崩检测"""

    def test_missing_rsi_blocks(self):
        """RSI=0 → data_incomplete → 返回 None"""
        ctx = _make_ctx(rsi=0)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_none_rsi_blocks(self):
        """RSI=None → data_incomplete → 返回 None"""
        ctx = _make_ctx(rsi=None)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_missing_atr_blocks(self):
        """ATR=0 → data_incomplete → 返回 None"""
        ctx = _make_ctx(atr=0)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_none_atr_blocks(self):
        """ATR=None → data_incomplete → 返回 None"""
        ctx = _make_ctx(atr=None)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_flash_crash_blocks(self):
        """slope_5m < -2.0 且 volume_ratio > 2.0 → flash_crash_detected"""
        ctx = _make_ctx(slope_5m=-3.0, volume_ratio=2.5)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_flash_crash_borderline_passes(self):
        """slope_5m = -2.0 (边界) → 不触发闪崩（需 < -2.0）"""
        ctx = _make_ctx(slope_5m=-2.0, volume_ratio=2.5, rsi=45)
        result = evaluate(ctx, _default_cfg())
        # slope_5m=-2.0 不满足 < -2.0, 不触发L0拒绝
        assert result is not None

    def test_normal_data_passes(self):
        """正常数据通过 L0"""
        ctx = _make_ctx(rsi=50, atr=2.0, slope_5m=0.1, volume_ratio=1.2)
        result = evaluate(ctx, _default_cfg())
        # 通过L0，继续到后续层
        assert result is not None


# ═══════════════════════════════════════════════════════════════════
#  Layer 1: 环境判断
# ═══════════════════════════════════════════════════════════════════

class TestLayer1Environment:
    """Layer 1: 仅 TREND_UP/TREND_DOWN 时激活"""

    def test_trend_up_activates_long(self):
        """TREND_UP → 方向 LONG"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=45)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert result.direction == 'LONG'

    def test_trend_down_activates_short(self):
        """TREND_DOWN + RSI in pullback_rsi_short [45,65] → SHORT"""
        ctx = _make_ctx(market_regime='TREND_DOWN', rsi=55)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert result.direction == 'SHORT'

    def test_range_blocks(self):
        """RANGE → 不激活 → None"""
        ctx = _make_ctx(market_regime='RANGE')
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_high_vol_blocks(self):
        """HIGH_VOL → 不激活 → None"""
        ctx = _make_ctx(market_regime='HIGH_VOL')
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_low_vol_blocks(self):
        """LOW_VOL → 不激活 → None"""
        ctx = _make_ctx(market_regime='LOW_VOL')
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_empty_regime_blocks(self):
        """空 regime → 不激活 → None"""
        ctx = _make_ctx(market_regime='')
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_case_insensitive_regime(self):
        """小写 trend_up → upper() → TREND_UP → 通过"""
        ctx = _make_ctx(market_regime='trend_up', rsi=45)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert result.direction == 'LONG'


# ═══════════════════════════════════════════════════════════════════
#  Layer 2: 选股评分
# ═══════════════════════════════════════════════════════════════════

class TestLayer2StockSelection:
    """Layer 2: 板块评分 + 龙头加分 + 资金流修正"""

    def test_low_sector_score_blocks(self):
        """sector_score=20, not leader → 20 < min_sector_score(40) → None"""
        ctx = _make_ctx(sector_info={'sector_score': 20, 'is_leader': False, 'sector_name': 'Energy'})
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_leader_bonus_applied(self):
        """sector_score=30 + leader_bonus=15 → 45 >= 40 → 通过 L2"""
        ctx = _make_ctx(
            sector_info={'sector_score': 30, 'is_leader': True, 'sector_name': 'Tech'},
            rsi=45, market_regime='TREND_UP'
        )
        result = evaluate(ctx, _default_cfg())
        # 30 + 15 = 45 >= 40, RSI=45 in [35,55] → PULLBACK 通过
        assert result is not None

    def test_no_leader_below_threshold(self):
        """sector_score=35, not leader → 35 < 40 → None"""
        ctx = _make_ctx(
            sector_info={'sector_score': 35, 'is_leader': False, 'sector_name': 'Tech'},
        )
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_overheat_penalty(self):
        """sector_score=70 + leader_bonus=15 = 85 > 80 → 85*0.8=68"""
        ctx = _make_ctx(
            sector_info={'sector_score': 70, 'is_leader': True, 'sector_name': 'AI'},
            rsi=45, market_regime='TREND_UP'
        )
        result = evaluate(ctx, _default_cfg())
        # 70+15=85>80 → 85*0.8=68, still >= 40, passes L2
        assert result is not None

    def test_flow_factor_boost(self):
        """正向资金流 → flow_factor > 1.0 → 增加评分"""
        ctx = _make_ctx(
            order_flow_imbalance=0.5,
            sector_info={'sector_score': 50, 'is_leader': False, 'sector_name': 'Tech'},
            rsi=45
        )
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        # 50 * max(0.85, min(1.15, 1.0 + 0.5*0.15)) = 50 * 1.075 = 53.75 >= 40

    def test_flow_factor_penalty(self):
        """负向资金流大到拉到 min_sector_score 以下"""
        ctx = _make_ctx(
            order_flow_imbalance=-1.0,
            sector_info={'sector_score': 42, 'is_leader': False, 'sector_name': 'Energy'},
            rsi=45
        )
        result = evaluate(ctx, _default_cfg())
        # 42 * max(0.85, min(1.15, 1.0 + (-1.0)*0.15)) = 42 * 0.85 = 35.7 < 40
        assert result is None


# ═══════════════════════════════════════════════════════════════════
#  Layer 3: 双模式入场
# ═══════════════════════════════════════════════════════════════════

class TestLayer3Entry:
    """Layer 3: PULLBACK 回调 / BREAKOUT 突破"""

    def test_pullback_long_rsi_in_range(self):
        """LONG + RSI=45 in [35,55] → PULLBACK"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=45)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert 'PULLBACK' in result.rationale

    def test_pullback_short_rsi_in_range(self):
        """SHORT + RSI=55 in [45,65] → PULLBACK"""
        ctx = _make_ctx(market_regime='TREND_DOWN', rsi=55)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert 'PULLBACK' in result.rationale

    def test_breakout_long_high_volume(self):
        """LONG + RSI=70 (>65) + volume_ratio=2.5 (>=2.0) → BREAKOUT"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=70, volume_ratio=2.5)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert 'BREAKOUT' in result.rationale

    def test_breakout_short_low_rsi_high_volume(self):
        """SHORT + RSI=30 (<35) + volume_ratio=2.5 (>=2.0) → BREAKOUT"""
        ctx = _make_ctx(market_regime='TREND_DOWN', rsi=30, volume_ratio=2.5)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert 'BREAKOUT' in result.rationale

    def test_no_entry_rsi_out_of_pullback_no_breakout(self):
        """LONG + RSI=60 (not in [35,55]) + volume_ratio=1.0 (<2.0) → 无入场"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=60, volume_ratio=1.0)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_high_rsi_low_volume_no_breakout(self):
        """LONG + RSI=70 (>65) 但 volume_ratio=1.5 (<2.0) → 不满足BREAKOUT"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=70, volume_ratio=1.5)
        result = evaluate(ctx, _default_cfg())
        assert result is None

    def test_pullback_boundary_lower(self):
        """LONG + RSI=35 (下界) → 满足 [35,55] → PULLBACK"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=35)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert 'PULLBACK' in result.rationale

    def test_pullback_boundary_upper(self):
        """LONG + RSI=55 (上界) → 满足 [35,55] → PULLBACK"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=55)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert 'PULLBACK' in result.rationale


# ═══════════════════════════════════════════════════════════════════
#  集成测试: SignalCandidate 输出验证
# ═══════════════════════════════════════════════════════════════════

class TestIntegration:
    """集成测试: 完整信号生成与属性验证"""

    def test_full_signal_generation(self):
        """完整信号生成: TREND_UP + 高板块分 + RSI回调区间"""
        ctx = _make_ctx(
            market_regime='TREND_UP',
            rsi=42,
            volume_ratio=1.8,
            slope_5m=0.5,
            sector_info={'sector_score': 65, 'is_leader': True, 'sector_name': 'Technology'}
        )
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert isinstance(result, SignalCandidate)
        assert result.direction == 'LONG'
        assert result.strategy_id == 'trend_first'
        assert result.source == 'trend_first'
        assert 30 <= result.strength <= 90
        assert 0.45 <= result.confidence <= 0.85
        assert result.symbol == 'US.AAPL'
        assert result.rationale  # non-empty

    def test_signal_score_computation(self):
        """验证动态评分: confidence/strength 由多层确认因子组合, 落在合理区间"""
        ctx = _make_ctx(
            market_regime='TREND_UP',
            rsi=45,
            order_flow_imbalance=0.0,
            sector_info={'sector_score': 65, 'is_leader': True, 'sector_name': 'Tech'}
        )
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        # Dynamic confidence: base 0.55 + volume(0.08) + ma_aligned(0.07) + rsi_quality + ofi
        assert 0.45 <= result.confidence <= 0.85
        # Dynamic strength: base 50 + bonuses
        assert 30 <= result.strength <= 90

    def test_confidence_lower_bound(self):
        """验证 confidence 下限 0.45"""
        ctx = _make_ctx(
            market_regime='TREND_UP',
            rsi=45,
            order_flow_imbalance=0.0,
            sector_info={'sector_score': 30, 'is_leader': True, 'sector_name': 'Tech'}
        )
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        # Dynamic confidence should be in [0.45, 0.85]
        assert 0.45 <= result.confidence <= 0.85

    def test_config_disabled_via_registry(self):
        """enabled=False 由注册表入口拦截（非策略函数本身）"""
        from app.strategy.pure import evaluate as registry_evaluate
        cfg = _default_cfg()
        cfg['enabled'] = False
        ctx = _make_ctx()
        # 通过注册表入口调用 → enabled 检查生效
        result = registry_evaluate('trend_first', ctx, cfg)
        assert result is None

    def test_sector_info_from_cfg(self):
        """sector_info 优先从 cfg 读取"""
        cfg = _default_cfg()
        cfg['sector_info'] = {'sector_score': 70, 'is_leader': False, 'sector_name': 'Energy'}
        ctx = _make_ctx(
            market_regime='TREND_UP', rsi=45,
            sector_info={'sector_score': 10, 'is_leader': False, 'sector_name': 'NA'}
        )
        result = evaluate(ctx, cfg)
        assert result is not None
        # cfg sector_info 70 >= 40 → 通过

    def test_context_preserved_in_output(self):
        """返回的 SignalCandidate.context 应引用原始 ctx"""
        ctx = _make_ctx(market_regime='TREND_UP', rsi=45)
        result = evaluate(ctx, _default_cfg())
        assert result is not None
        assert result.context is ctx
