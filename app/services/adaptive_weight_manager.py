#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AdaptiveWeightManager — 策略权重自适应学习管理器

从 app/strategy/consensus_engine.py 提取的独立服务。

版本历史:
  2026-05-19 v1: 从 consensus_engine.py 提取 (Phase 17)
"""

import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# 默认基准权重 (从 consensus_engine.py 复制)
DEFAULT_BASE_WEIGHTS = {
    'confluence_v2': 1.0,
    'sentiment': 0.8,
    'event_driven': 0.7,
    'cross_market': 0.6,
    'mean_reversion': 0.5,
    'momentum': 0.5,
}

# 场景连续失败限制 (从 consensus_engine.py 复制)
SCENARIO_CONSECUTIVE_FAIL_LIMIT = 5
SCENARIO_FAIL_PENALTY = -0.15


class AdaptiveWeightManager:
    """
    策略权重自适应学习管理器 v2（贝叶斯收缩 + 混合模型 + 持久化）

    v1→v2 变更 (2026-04-11):
    - 过拟合修复：贝叶斯收缩替代原线性映射，小样本权重向先验 (1.0) 靠拢
    - 模型重构：场景维度从乘法改为加法 delta（±SCENARIO_DELTA_MAX），避免级联放大
    - 反馈延迟：从每天 1 次改为每 N 笔交易触发（UPDATE_EVERY_N_TRADES=5）
    - 数据孤岛：权重快照持久化到 SQLite，进程重启后恢复上次学习成果
    - UCB 探索：保留不变

    公式:
        confidence = min(1.0, trades / CONFIDENCE_TRADES)
        shrunk_rate = confidence × actual_win_rate + (1 - confidence) × PRIOR_WIN_RATE
        base_multiplier = MAX_DECAY + (MAX_BOOST - MAX_DECAY) × shrunk_rate
        scenario_delta = confidence_s × (scenario_win_rate - 0.5) × 2 × SCENARIO_DELTA_MAX
        ucb_delta = UCB_EXPLORATION / sqrt(trades)
        final_multiplier = clamp(base_multiplier + scenario_delta + ucb_delta, MAX_DECAY, MAX_BOOST)
        final_weight = base_weight × final_multiplier
    """

    # 调整边界（相对基准权重的比例）
    MAX_BOOST  = 1.30   # 最多上调 30%
    MAX_DECAY  = 0.70   # 最多下调 30%
    MIN_TRADES = 3      # v2: 配合贝叶斯收缩，3 笔即可开始学习
    MIN_TRADES_PER_REGIME = 3  # 每个市场状态最少交易数

    # v2 新增参数
    CONFIDENCE_TRADES = 20       # 达到此笔数时完全信任实际胜率
    UPDATE_EVERY_N_TRADES = 5    # 每 5 笔触发增量更新
    PRIOR_WIN_RATE = 0.5         # 贝叶斯先验胜率
    SCENARIO_DELTA_MAX = 0.15    # 场景加法最大偏移 ±0.15
    DATA_RETENTION_DAYS = 30     # 数据保留天数

    # UCB 探索参数
    UCB_EXPLORATION = 0.1  # 探索系数（10% 的探索空间）

    def __init__(self, base_weights: Dict[str, float], config: Dict = None):
        """
        初始化自适应权重管理器

        Args:
            base_weights: 基准权重的引用 (用于更新权重)
            config: 配置参数
        """
        self.base_weights = base_weights  # 引用，直接修改
        # 从 config 加载参数（硬编码作为 fallback）
        self._load_config(config)
        # {策略：[{'pnl': float, 'ts': datetime, 'exit_reason': str}, ...]}
        self._trade_history: Dict[str, List[Dict]] = defaultdict(list)
        # {策略：{市场状态：[{'pnl': float, 'ts': datetime, 'adx': float}, ...]}}
        self._regime_performance: Dict[str, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
        # {策略：{场景：[{'pnl': float, 'ts': datetime}, ...]}}
        self._scenario_performance: Dict[str, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
        self._last_update: Optional[datetime] = None
        self._trades_since_update: int = 0  # v2: 笔数触发计数器
        self._lock = threading.Lock()
        # v2: 启动时从 SQLite 恢复上次权重
        self._load_from_db()
        logger.info(
            f"自适应权重管理器 v2 已初始化 | "
            f"置信笔数={self.CONFIDENCE_TRADES}, 更新间隔={self.UPDATE_EVERY_N_TRADES}笔，"
            f"场景 delta_max=±{self.SCENARIO_DELTA_MAX}"
        )

    def _load_config(self, config = None):
        """从 config 加载自适应权重参数（覆盖类常量）"""
        if not config:
            return
        # ConfigProxy → dict 转换
        if hasattr(config, 'to_dict'):
            config = config.to_dict()
        elif not isinstance(config, dict):
            return
        aw_cfg = config.get('adaptive_weight', {})
        if not aw_cfg:
            return
        if hasattr(aw_cfg, 'to_dict'):
            aw_cfg = aw_cfg.to_dict()
        elif not isinstance(aw_cfg, dict):
            return
        if 'confidence_trades' in aw_cfg:
            self.CONFIDENCE_TRADES = int(aw_cfg['confidence_trades'])
        if 'min_trades' in aw_cfg:
            self.MIN_TRADES = int(aw_cfg['min_trades'])
        if 'update_every_n_trades' in aw_cfg:
            self.UPDATE_EVERY_N_TRADES = int(aw_cfg['update_every_n_trades'])
        if 'max_boost' in aw_cfg:
            self.MAX_BOOST = float(aw_cfg['max_boost'])
        if 'max_decay' in aw_cfg:
            self.MAX_DECAY = float(aw_cfg['max_decay'])
        if 'prior_win_rate' in aw_cfg:
            self.PRIOR_WIN_RATE = float(aw_cfg['prior_win_rate'])
        if 'ucb_exploration' in aw_cfg:
            self.UCB_EXPLORATION = float(aw_cfg['ucb_exploration'])
        if 'scenario_delta_max' in aw_cfg:
            self.SCENARIO_DELTA_MAX = float(aw_cfg['scenario_delta_max'])
        if 'data_retention_days' in aw_cfg:
            self.DATA_RETENTION_DAYS = int(aw_cfg['data_retention_days'])
        logger.info(f"[自适应权重] 从配置加载参数：{aw_cfg}")

    def record_trade(
        self,
        agree_strategies: List[str],
        pnl: float,
        market_regime: str = "neutral",
        scenario: str = None,
        adx_value: float = 0.0,
        exit_reason: str = None,
    ):
        """
        记录一笔已平仓的交易结果

        Args:
            agree_strategies: 本次共识中同向投票的策略列表
            pnl: 已实现盈亏（正=盈利，负=亏损）
            market_regime: 市场状态 (trending/ranging/neutral)
            scenario: 场景标签
            adx_value: ADX 值
            exit_reason: 退出理由 (stop_loss/take_profit/trailing_stop/risk_control 等)
        """
        now = datetime.now()
        with self._lock:
            for strategy in agree_strategies:
                self._trade_history[strategy].append({
                    'pnl': pnl, 'ts': now, 'exit_reason': exit_reason or 'unknown'
                })
                self._regime_performance[strategy][market_regime].append({
                    'pnl': pnl,
                    'ts': now,
                    'adx': adx_value,
                })
                if scenario:
                    self._scenario_performance[strategy][scenario].append({
                        'pnl': pnl,
                        'ts': now,
                    })
            # v2: 累计笔数计数器
            self._trades_since_update += 1

        logger.debug(
            f"[自适应权重] 记录交易：策略={agree_strategies}, pnl={pnl:.2f}, "
            f"市场状态={market_regime}, 场景={scenario or 'N/A'}, ADX={adx_value:.1f}, "
            f"累计={self._trades_since_update}/{self.UPDATE_EVERY_N_TRADES}"
        )
        self.update_weights_if_ready()

    def update_weights_if_ready(self):
        """
        检查是否满足更新条件，满足则重新计算并应用权重

        v2 触发条件（满足其一即可）:
        1. 累计交易笔数 >= UPDATE_EVERY_N_TRADES
        2. 距上次更新超过 1 天（兜底，防止无交易时永不更新）
        """
        with self._lock:
            should_update = False
            # 条件 1: 笔数触发
            if self._trades_since_update >= self.UPDATE_EVERY_N_TRADES:
                should_update = True
            # 条件 2: 每日兜底
            elif self._last_update is not None:
                elapsed = datetime.now() - self._last_update
                if elapsed >= timedelta(days=1):
                    should_update = True
            else:
                # 首次运行，有足够交易就更新
                total = sum(len(v) for v in self._trade_history.values())
                if total >= self.MIN_TRADES:
                    should_update = True

            if should_update:
                self._do_update_weights()

    def _do_update_weights(self):
        """
        执行权重重计算 v2（调用时须持有 _lock）

        核心变更:
        1. 贝叶斯收缩：shrunk_rate = confidence × actual + (1-confidence) × prior
        2. 场景从乘法改为加法 delta
        3. 市场状态保持乘法但加入置信度
        4. 更新后持久化到 SQLite
        """
        # 清理过期数据
        cutoff = datetime.now() - timedelta(days=self.DATA_RETENTION_DAYS)
        for strategy in list(self._trade_history.keys()):
            self._trade_history[strategy] = [
                r for r in self._trade_history[strategy] if r['ts'] > cutoff
            ]
        for strategy in list(self._regime_performance.keys()):
            for regime in list(self._regime_performance[strategy].keys()):
                self._regime_performance[strategy][regime] = [
                    r for r in self._regime_performance[strategy][regime]
                    if r['ts'] > cutoff
                ]
        for strategy in list(self._scenario_performance.keys()):
            for scenario in list(self._scenario_performance[strategy].keys()):
                self._scenario_performance[strategy][scenario] = [
                    r for r in self._scenario_performance[strategy][scenario]
                    if r['ts'] > cutoff
                ]

        updated = {}
        for strategy in set(list(self._trade_history.keys()) + list(self._regime_performance.keys())):
            records = self._trade_history.get(strategy, [])
            if len(records) < self.MIN_TRADES:
                continue

            # 1. 贝叶斯收缩胜率 (退出归因感知：风控退出不计入策略评价)
            effective_records = []
            for r in records:
                er = r.get('exit_reason', 'unknown')
                if er == 'risk_control':
                    continue  # 风控退出不计入策略评价
                if er == 'time_exit' and r['pnl'] < 0:
                    # 时间退出微亏：降低负面评价 (pnl * 0.5)
                    effective_records.append({**r, 'pnl': r['pnl'] * 0.5})
                else:
                    effective_records.append(r)

            if len(effective_records) < self.MIN_TRADES:
                continue

            wins = sum(1 for r in effective_records if r['pnl'] > 0)
            actual_win_rate = wins / len(effective_records)

            # 分层胜率时间窗
            SHORT_WIN = 20
            MEDIUM_WIN = 60
            LONG_WIN = 200
            SW = 0.3
            MW = 0.5
            LW = 0.2

            recent = effective_records[-SHORT_WIN:]
            medium = effective_records[-MEDIUM_WIN:]
            long_term = effective_records[-LONG_WIN:]

            wr_short = sum(1 for r in recent if r['pnl'] > 0) / max(1, len(recent))
            wr_medium = sum(1 for r in medium if r['pnl'] > 0) / max(1, len(medium))
            wr_long = sum(1 for r in long_term if r['pnl'] > 0) / max(1, len(long_term))

            # 加权胜率
            weighted_win_rate = wr_short * SW + wr_medium * MW + wr_long * LW

            confidence = min(1.0, len(records) / self.CONFIDENCE_TRADES)
            shrunk_rate = confidence * weighted_win_rate + (1 - confidence) * self.PRIOR_WIN_RATE

            # 2. 基础倍数（贝叶斯收缩后映射）
            base_multiplier = self.MAX_DECAY + (self.MAX_BOOST - self.MAX_DECAY) * shrunk_rate

            # 3. 市场状态调整（保持乘法，加入置信度）
            regime_adjustment = self._calculate_regime_adjustment(strategy)
            regime_adjustment = 1.0 + confidence * (regime_adjustment - 1.0)

            # 4. 场景加法 delta（v2: 从乘法改为加法）
            scenario_delta = self._calculate_scenario_delta(strategy)

            # 5. UCB 探索加成
            ucb_delta = self._calculate_ucb_bonus(strategy, len(records))

            # 6. 综合：base_multiplier × regime (乘法) + scenario + ucb (加法)
            final_multiplier = base_multiplier * regime_adjustment + scenario_delta + ucb_delta

            # 6b. 长窗策略保留判断
            long_disable_threshold = 0.30
            if len(long_term) >= LONG_WIN and wr_long < long_disable_threshold:
                logger.warning(
                    f"[自适应权重/长窗警告] {strategy} 长期胜率={wr_long:.0%} < {long_disable_threshold:.0%}, "
                    f"建议评伋是否停用"
                )
                final_multiplier *= 0.85  # 额外惩罚

            # 7. 截断
            final_multiplier = max(self.MAX_DECAY, min(self.MAX_BOOST, final_multiplier))

            base_weight = DEFAULT_BASE_WEIGHTS.get(strategy, 0.5)
            new_weight = round(base_weight * final_multiplier, 3)

            old_weight = self.base_weights.get(strategy, base_weight)
            self.base_weights[strategy] = new_weight
            updated[strategy] = {
                'old': old_weight,
                'new': new_weight,
                'actual_wr': round(actual_win_rate, 3),
                'layered_wr': round(weighted_win_rate, 3),
                'wr_short': round(wr_short, 3),
                'wr_medium': round(wr_medium, 3),
                'wr_long': round(wr_long, 3),
                'shrunk_wr': round(shrunk_rate, 3),
                'confidence': round(confidence, 2),
                'trades': len(records),
                'regime_adj': round(regime_adjustment, 3),
                'scenario_delta': round(scenario_delta, 3),
                'ucb_delta': round(ucb_delta, 3),
            }

        if updated:
            logger.info(
                f"[自适应权重 v2] 权重已更新 ({len(updated)} 个策略): "
                + ", ".join(
                    f"{s}={v['old']:.2f}→{v['new']:.2f}"
                    f"(实际{v['actual_wr']:.0%}→收缩{v['shrunk_wr']:.0%}, "
                    f"置信{v['confidence']:.0%}/{v['trades']}笔，"
                    f"状态×{v['regime_adj']:.2f}, 场景Δ{v['scenario_delta']:+.3f}, "
                    f"UCBΔ{v['ucb_delta']:+.3f})"
                    for s, v in updated.items()
                )
            )
            # v2: 持久化到 SQLite
            self._save_to_db(updated)
        else:
            logger.debug("[自适应权重 v2] 无策略满足最少交易笔数要求，跳过本次更新")

        self._last_update = datetime.now()
        self._trades_since_update = 0  # 重置计数器

    def _calculate_regime_adjustment(self, strategy: str) -> float:
        """
        计算市场状态调整系数

        逻辑：
        - 如果策略在某个市场状态表现特别好（胜率>70%），且该状态频繁出现，给予加成
        - 如果策略在某个市场状态表现特别差（胜率<30%），且该状态频繁出现，给予惩罚
        - 否则返回 1.0（无调整）
        """
        regime_perf = self._regime_performance.get(strategy, {})
        if not regime_perf:
            return 1.0

        total_adjustment = 0.0
        total_weight = 0.0

        for regime, records in regime_perf.items():
            if len(records) < self.MIN_TRADES_PER_REGIME:
                continue

            wins = sum(1 for r in records if r['pnl'] > 0)
            win_rate = wins / len(records)

            if win_rate > 0.7:
                adjustment = 1.1
            elif win_rate < 0.3:
                adjustment = 0.9
            else:
                adjustment = 1.0

            total_adjustment += adjustment * len(records)
            total_weight += len(records)

        if total_weight == 0:
            return 1.0

        return total_adjustment / total_weight

    def _calculate_scenario_delta(self, strategy: str) -> float:
        """
        计算场景维度加法 delta (v2: 从乘法改为加法)

        公式：delta = confidence_s × (weighted_win_rate - 0.5) × 2 × SCENARIO_DELTA_MAX
        范围：[-SCENARIO_DELTA_MAX, +SCENARIO_DELTA_MAX]
        """
        scenario_perf = self._scenario_performance.get(strategy, {})
        if not scenario_perf:
            return 0.0

        total_wr_weighted = 0.0
        total_samples = 0

        for scenario, records in scenario_perf.items():
            if len(records) < self.MIN_TRADES_PER_REGIME:
                continue

            # 策略场景失效检测
            fail_limit = SCENARIO_CONSECUTIVE_FAIL_LIMIT
            fail_penalty = SCENARIO_FAIL_PENALTY
            recent = records[-fail_limit:]
            if len(recent) >= fail_limit and all(r['pnl'] < 0 for r in recent):
                logger.info(
                    f"[策略失效] {strategy} 在 {scenario} 场景连续{len(recent)}笔亏损，"
                    f"触发降权惩罚 {fail_penalty:+.2f}"
                )
                return fail_penalty

            wins = sum(1 for r in records if r['pnl'] > 0)
            win_rate = wins / len(records)
            total_wr_weighted += win_rate * len(records)
            total_samples += len(records)

        if total_samples == 0:
            return 0.0

        weighted_win_rate = total_wr_weighted / total_samples
        confidence_s = min(1.0, total_samples / self.CONFIDENCE_TRADES)
        delta = confidence_s * (weighted_win_rate - 0.5) * 2 * self.SCENARIO_DELTA_MAX
        return max(-self.SCENARIO_DELTA_MAX, min(self.SCENARIO_DELTA_MAX, delta))

    def _calculate_ucb_bonus(self, strategy: str, total_trades: int) -> float:
        """
        计算 UCB 探索加成

        UCB (Upper Confidence Bound) 算法简化版：
        - 交易次数少的策略给予探索加成，避免过早忽略
        - 交易次数多的策略探索加成逐渐减小
        """
        if total_trades < self.MIN_TRADES:
            return self.UCB_EXPLORATION

        ucb = self.UCB_EXPLORATION / (total_trades ** 0.5)
        return min(ucb, self.UCB_EXPLORATION)

    def _save_to_db(self, updated: Dict[str, Dict]):
        """将更新后的权重快照保存到 SQLite"""
        try:
            import json
            from app.storage.database import get_database
            db = get_database()
            for strategy, info in updated.items():
                regime_detail = {}
                for regime, records in self._regime_performance.get(strategy, {}).items():
                    wins = sum(1 for r in records if r['pnl'] > 0)
                    regime_detail[regime] = {'wins': wins, 'total': len(records)}

                scenario_detail = {}
                for scenario, records in self._scenario_performance.get(strategy, {}).items():
                    wins = sum(1 for r in records if r['pnl'] > 0)
                    scenario_detail[scenario] = {'wins': wins, 'total': len(records)}

                db.save_weight_snapshot(
                    strategy=strategy,
                    weight=info['new'],
                    win_rate=info['shrunk_wr'],
                    trades=info['trades'],
                    regime_detail=json.dumps(regime_detail) if regime_detail else None,
                    scenario_detail=json.dumps(scenario_detail) if scenario_detail else None,
                )
            logger.info(f"[自适应权重 v2] 已持久化 {len(updated)} 个策略权重快照")
        except Exception as e:
            logger.warning(f"[自适应权重 v2] 持久化失败（降级为内存模式）: {e}")

    def _load_from_db(self):
        """启动时从 SQLite 恢复上次权重"""
        try:
            from app.storage.database import get_database
            db = get_database()
            snapshots = db.load_weight_snapshots()
            if not snapshots:
                logger.info("[自适应权重 v2] 无历史快照，使用默认权重")
                return

            restored = 0
            for strategy, info in snapshots.items():
                if strategy in DEFAULT_BASE_WEIGHTS:
                    base = DEFAULT_BASE_WEIGHTS[strategy]
                    saved_weight = info['weight']
                    if base * self.MAX_DECAY <= saved_weight <= base * self.MAX_BOOST:
                        self.base_weights[strategy] = saved_weight
                        restored += 1
                        logger.debug(
                            f"[自适应权重 v2] 恢复 {strategy}: {saved_weight:.3f} "
                            f"(胜率={info['win_rate']:.0%}, {info['trades']}笔，"
                            f"更新于={info.get('updated_at', 'N/A')})"
                        )
                    else:
                        logger.warning(
                            f"[自适应权重 v2] {strategy} 快照权重 {saved_weight:.3f} "
                            f"超出安全范围 [{base*self.MAX_DECAY:.3f}, {base*self.MAX_BOOST:.3f}]，"
                            f"使用默认值"
                        )

            if restored:
                logger.info(f"[自适应权重 v2] 已从 SQLite 恢复 {restored} 个策略权重")
        except Exception as e:
            logger.warning(f"[自适应权重 v2] 恢复失败（使用默认权重）: {e}")

    def get_strategy_stats(self) -> Dict[str, Dict]:
        """获取各策略历史统计"""
        stats = {}
        cutoff = datetime.now() - timedelta(days=self.DATA_RETENTION_DAYS)
        with self._lock:
            for strategy, records in self._trade_history.items():
                recent = [r for r in records if r['ts'] > cutoff]
                if not recent:
                    continue
                wins = sum(1 for r in recent if r['pnl'] > 0)
                total_pnl = sum(r['pnl'] for r in recent)
                confidence = min(1.0, len(recent) / self.CONFIDENCE_TRADES)
                stats[strategy] = {
                    'trades': len(recent),
                    'wins': wins,
                    'win_rate': round(wins / len(recent), 3),
                    'confidence': round(confidence, 2),
                    'total_pnl': round(total_pnl, 2),
                    'current_weight': self.base_weights.get(strategy, 0.5),
                }
        return stats

    def get_kelly_stats(self, strategies: list, market_regime: str = None):
        """获取 Kelly 公式所需的统计数据"""
        with self._lock:
            all_records = []
            for s in strategies:
                records = self._trade_history.get(s, [])
                if market_regime:
                    regime_records = []
                    for rgn, recs in self._regime_performance.get(s, {}).items():
                        if rgn == market_regime:
                            regime_records.extend(recs)
                    records = records + regime_records
                all_records.extend(records)

            if len(all_records) < self.MIN_TRADES:
                return None

            wins = [r['pnl'] for r in all_records if r.get('pnl', 0) > 0]
            losses = [r['pnl'] for r in all_records if r.get('pnl', 0) < 0]

            total = len(all_records)
            wr = len(wins) / total if total > 0 else 0.0
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0

            if avg_loss == 0 or avg_win == 0:
                return None

            rr = avg_win / avg_loss
            kelly_f = (wr * (rr + 1) - 1) / rr
            half_kelly = max(0.01, min(kelly_f * 0.5, 0.15))

            return {
                'win_rate': round(wr, 3),
                'avg_win': round(avg_win, 2),
                'avg_loss': round(avg_loss, 2),
                'reward_risk': round(rr, 2),
                'kelly_fraction': round(kelly_f, 4),
                'half_kelly': round(half_kelly, 4),
                'total_trades': total,
            }

    def apply_drift_penalty(self, code: str, penalty: float = -0.05):
        """对未平仓亏损的策略施加即时权重惩罚"""
        penalty = max(-0.15, min(0.0, penalty))
        with self._lock:
            affected = set()
            for s, history in self._trade_history.items():
                for r in history[-5:]:
                    if r.get('code') == code:
                        affected.add(s)

            if not affected:
                return

            for s in affected:
                current = self.base_weights.get(s, 1.0)
                new_w = max(0.3, current + penalty)
                self.base_weights[s] = round(new_w, 2)
                logger.info(
                    f"[漂移惩罚] {s} {code} 浮亏漂移，"
                    f"权重 {current:.2f} -> {new_w:.2f} (penalty={penalty})"
                )
