"""
影子账户 (Shadow Account) V2 — KMeans 策略提取 + 五因子归因分解

借鉴 Vibe-Trading 的 extractor.py + backtester.py 核心算法:
  1. KMeans 聚类自动提取隐含策略规则 (非硬编码阈值)
  2. 五因子归因分解: noise/early_exit/late_exit/overtrading/missed_signals

非交易路径: 盘后异步运行，零侵入交易管线
"""

import logging
import math
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from app.diagnostics.base import BaseDiagnostic, DiagnosticResult
from app.diagnostics.models import (
    ShadowRule, ClusterResult, AttributionBreakdown, FactorExposure
)

logger = logging.getLogger(__name__)


class ShadowAccountDiagnostic(BaseDiagnostic):

    @property
    def name(self) -> str:
        return "shadow_account_v2"

    @property
    def schedule_hint(self) -> str:
        return "post_market"

    def is_applicable(self, **kwargs) -> bool:
        return kwargs.get("has_trades", False)

    def run(self, **kwargs) -> DiagnosticResult:
        db_path = kwargs.get("db_path")
        lookback_days = kwargs.get("lookback_days", 30)
        use_kmeans = kwargs.get("use_kmeans", True)
        use_attribution = kwargs.get("use_attribution", True)

        # ── 1. 加载成交数据 ──
        trades = self._load_via_store(db_path, lookback_days)
        if len(trades) < 5:
            return DiagnosticResult(
                module_name=self.name,
                status="skipped",
                data={"reason": f"样本不足 (n={len(trades)} < 5)"},
                summary="成交记录不足 5 笔，无法进行策略提取",
            )

        roundtrips = self._pair_trades_fifo(trades)
        profitable = [r for r in roundtrips if r.get("pnl", 0) > 0]

        result_data = {"total_trades": len(trades), "roundtrips": len(roundtrips), "profitable": len(profitable)}

        # ── 2. KMeans 策略规则提取 (Vibe-Trading extractor.py 核心) ──
        rules: List[ShadowRule] = []
        cluster_result: Optional[ClusterResult] = None

        if use_kmeans and len(profitable) >= 5:
            try:
                cluster_result, rules = self._extract_rules_kmeans(profitable)
                result_data["cluster_result"] = cluster_result.to_dict()
                result_data["rules"] = [r.to_dict() for r in rules]
            except Exception as e:
                logger.warning(f"[{self.name}] KMeans 提取失败，降级到启发式: {e}")
                rules = self._heuristic_single_rule(profitable)
                result_data["rules"] = [r.to_dict() for r in rules]
                result_data["kmeans_failed"] = True
        else:
            rules = self._heuristic_single_rule(profitable)
            result_data["rules"] = [r.to_dict() for r in rules]
            result_data["kmeans_skipped"] = True

        # ── 3. 五因子归因分解 (Vibe-Trading backtester.py 行 394-475) ──
        attribution: Optional[AttributionBreakdown] = None
        if use_attribution and rules:
            try:
                attribution = self._five_factor_decomposition(roundtrips, rules)
                result_data["attribution"] = attribution.to_dict()
            except Exception as e:
                logger.warning(f"[{self.name}] 归因分解失败: {e}")
                result_data["attribution_failed"] = True

        # ── 4. 行为偏差诊断 (保留原有 4 类) ──
        disposition = self._analyze_disposition_effect(roundtrips)
        chasing = self._analyze_chasing(trades)
        anchoring = self._analyze_anchoring(roundtrips)

        result_data.update({
            "disposition_effect": disposition,
            "chasing": chasing,
            "anchoring": anchoring,
        })

        # ── 5. 综合评分 ──
        score = self._compute_behavioral_score(
            disposition, chasing, anchoring, attribution, rules
        )
        result_data["behavioral_score"] = score

        return DiagnosticResult(
            module_name=self.name,
            status="success",
            data=result_data,
            summary=self._build_summary(roundtrips, rules, attribution, score),
        )

    # ═══════════════════════════════════════════════════
    #  数据加载
    # ═══════════════════════════════════════════════════

    def _load_via_store(self, db_path: str, lookback_days: int) -> List[Dict]:
        """从 trade_records 表加载成交数据 (适配 FTSeek-US 实际 schema)"""
        import sqlite3

        if not db_path:
            db_path = self._get_db_path()

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                from app.utils.timezone import now_et as _now_et
                cutoff = (_now_et() - timedelta(days=lookback_days)).isoformat()
            except Exception:
                cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()

            # ⚠️ P0 修复: 实际表名是 trade_records (不是 trades)
            # 2026-06-30: 过滤 trade_mode='live' (实盘不混入SIM归因) + 排除 shadow/scalping
            query = """
                SELECT symbol, direction, entry_price, exit_price,
                       entry_time, exit_time, realized_pnl as pnl,
                       pnl_pct, exit_reason, entry_reason, decision_reason,
                       quantity as size, holding_hours, signal_strength,
                       signal_confidence, strategy_id, source_type,
                       trace_id, trade_date
                FROM trade_records
                WHERE exit_time IS NOT NULL AND exit_time > ?
                AND trade_mode != 'live'
                AND source_type NOT IN ('PLAYBOOK_SHADOW', 'SCALPING')
                ORDER BY exit_time DESC
            """
            rows = conn.execute(query, (cutoff,)).fetchall()
            conn.close()

            trades = []
            for row in rows:
                row = dict(row)  # sqlite3.Row → dict (Row 不支持 .get())
                trades.append({
                    "symbol": row["symbol"],
                    "direction": row.get("direction", "LONG"),
                    "entry_price": float(row["entry_price"] or 0),
                    "exit_price": float(row["exit_price"] or 0),
                    "entry_time": row.get("entry_time"),
                    "exit_time": row.get("exit_time"),
                    "pnl": float(row.get("pnl", 0) or 0),
                    "pnl_pct": float(row.get("pnl_pct", 0) or 0),
                    "signal_price": None,  # trade_records 无此列
                    "signal_time": None,
                    "size": int(row.get("size", 0) or 0),
                    "exit_reason": row.get("exit_reason", ""),
                    "entry_reason": row.get("entry_reason", ""),
                    "decision_reason": row.get("decision_reason", ""),
                    "holding_hours": float(row.get("holding_hours", 0) or 0),
                    "holding_days": float(row.get("holding_hours", 0) or 0) / 24,
                    "signal_strength": int(row.get("signal_strength", 0) or 0),
                    "trace_id": row.get("trace_id", ""),
                    "strategy_id": row.get("strategy_id", ""),
                })
            return trades
        except Exception as e:
            logger.debug(f"[{self.name}] 加载交易数据失败: {e}")
            return []

    @staticmethod
    def _pair_trades_fifo(trades: List[Dict]) -> List[Dict]:
        """
        简化配对: trade_records 已经是 roundtrip (entry+exit 配对好)
        直接使用 holding_days 字段
        """
        roundtrips = []
        for t in trades:
            hold_days = t.get("holding_days", 0)
            if hold_days <= 0:
                hold_days = t.get("holding_hours", 0) / 24

            entry_hour = 9.5
            if t.get("entry_time"):
                try:
                    dt = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
                    entry_hour = dt.hour + dt.minute / 60
                except (ValueError, TypeError):
                    pass

            roundtrips.append({
                "symbol": t.get("symbol", "UNKNOWN"),
                "direction": t.get("direction", "LONG"),
                "pnl": t.get("pnl", 0),
                "pnl_pct": t.get("pnl_pct", 0),
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "entry_time": t.get("entry_time"),
                "exit_time": t.get("exit_time"),
                "holding_hours": t.get("holding_hours", 0),
                "holding_days": hold_days,
                "entry_hour": entry_hour,
                "size": t.get("size", 0),
                "exit_reason": t.get("exit_reason", ""),
                "entry_reason": t.get("entry_reason", ""),
                "decision_reason": t.get("decision_reason", ""),
                "trace_id": t.get("trace_id", ""),
                "strategy_id": t.get("strategy_id", ""),
            })

        return roundtrips

    # ═══════════════════════════════════════════════════
    #  KMeans 策略提取 (Vibe-Trading extractor.py)
    # ═══════════════════════════════════════════════════

    def _extract_rules_kmeans(self, profitable: List[Dict]) -> Tuple[ClusterResult, List[ShadowRule]]:
        """
        KMeans 聚类自动提取策略规则

        算法 (extractor.py 行 202-284):
          1. 提取特征: holding_days, pnl_pct, entry_hour
          2. StandardScaler 标准化
          3. KMeans k=2..5, 轮廓系数选最优 k
          4. 对每个 cluster 取 p10-p90 分位数 → ShadowRule
        """
        # 提取特征矩阵
        features = []
        for t in profitable:
            hold = max(0.01, t.get("holding_days", 1))
            pnl = t.get("pnl_pct", 0)
            hour = 9.5  # 默认开盘价
            if t.get("entry_time"):
                try:
                    dt = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
                    hour = dt.hour + dt.minute / 60
                except (ValueError, TypeError):
                    pass
            features.append([hold, pnl, hour])

        X = np.array(features)

        # StandardScaler 标准化
        means = X.mean(axis=0)
        stds = X.std(axis=0) + 1e-8
        X_scaled = (X - means) / stds

        # KMeans k=2..5, 轮廓系数选最优
        try:
            from sklearn.cluster import KMeans
            from sklearn.metrics import silhouette_score

            best_k = 2
            best_score = -1
            best_labels = None

            for k in range(2, min(6, len(profitable))):
                km = KMeans(n_clusters=k, random_state=42, n_init=10)
                labels = km.fit_predict(X_scaled)
                if len(set(labels)) > 1:
                    score = silhouette_score(X_scaled, labels)
                    if score > best_score:
                        best_score = score
                        best_k = k
                        best_labels = labels
        except ImportError:
            logger.warning(f"[{self.name}] sklearn 不可用，降级到启发式规则")
            rules = self._heuristic_single_rule(profitable)
            return None, rules

        if best_labels is None:
            rules = self._heuristic_single_rule(profitable)
            return None, rules

        # 构建 ClusterResult
        cluster_sizes = {}
        cluster_avg_pnl = {}
        for i, label in enumerate(best_labels):
            cluster_sizes[label] = cluster_sizes.get(label, 0) + 1
            cluster_avg_pnl[label] = cluster_avg_pnl.get(label, 0) + profitable[i].get("pnl_pct", 0)

        for k in cluster_avg_pnl:
            cluster_avg_pnl[k] /= cluster_sizes[k]

        cluster_result = ClusterResult(
            n_clusters=best_k,
            silhouette_score=best_score,
            labels=best_labels.tolist(),
            cluster_sizes=cluster_sizes,
            cluster_avg_pnl={k: round(v, 4) for k, v in cluster_avg_pnl.items()},
        )

        # 对每个 cluster 生成 ShadowRule
        rules = []
        for cluster_id in range(best_k):
            mask = np.array(best_labels) == cluster_id
            cluster_data = [profitable[i] for i in range(len(profitable)) if mask[i]]

            if len(cluster_data) < 2:
                continue

            rule = self._cluster_to_rule(cluster_id, cluster_data, len(profitable))
            if rule:
                rules.append(rule)

        return cluster_result, rules

    def _cluster_to_rule(self, cluster_id: int, cluster_data: List[Dict],
                         total: int) -> Optional[ShadowRule]:
        """将单个聚类转换为 ShadowRule"""
        holds = [t.get("holding_days", 1) for t in cluster_data]
        hours = []
        for t in cluster_data:
            if t.get("entry_time"):
                try:
                    dt = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
                    hours.append(dt.hour + dt.minute / 60)
                except (ValueError, TypeError):
                    hours.append(9.5)
            else:
                hours.append(9.5)

        hold_lo, hold_hi = np.percentile(holds, [10, 90])
        hour_lo, hour_hi = np.percentile(hours, [10, 90])
        avg_pnl = np.mean([t.get("pnl_pct", 0) for t in cluster_data])

        human_text = (
            f"持有 {hold_lo:.1f}-{hold_hi:.1f} 天，"
            f"{hour_lo:.1f}-{hour_hi:.1f} 点入场，"
            f"平均盈利 {avg_pnl*100:.1f}%"
        )

        return ShadowRule(
            rule_id=f"R{cluster_id + 1}",
            human_text=human_text,
            entry_hour_range=(round(hour_lo, 1), round(hour_hi, 1)),
            holding_days_range=(round(hold_lo, 1), round(hold_hi, 1)),
            support_count=len(cluster_data),
            coverage_rate=len(cluster_data) / total,
            avg_pnl_pct=round(avg_pnl, 4),
        )

    def _heuristic_single_rule(self, profitable: List[Dict]) -> List[ShadowRule]:
        """降级: 启发式单规则"""
        holds = [t.get("holding_days", 1) for t in profitable]
        avg_hold = sum(holds) / len(holds)
        avg_pnl = sum(t.get("pnl_pct", 0) for t in profitable) / len(profitable)

        return [ShadowRule(
            rule_id="R1",
            human_text=f"平均持有 {avg_hold:.1f} 天，平均盈利 {avg_pnl*100:.1f}%",
            entry_hour_range=(9.0, 16.0),
            holding_days_range=(max(0.1, avg_hold * 0.5), avg_hold * 1.5),
            support_count=len(profitable),
            coverage_rate=1.0,
            avg_pnl_pct=round(avg_pnl, 4),
        )]

    # ═══════════════════════════════════════════════════
    #  五因子归因分解 (Vibe-Trading backtester.py 行 394-475)
    # ═══════════════════════════════════════════════════

    def _five_factor_decomposition(self, roundtrips: List[Dict],
                                    rules: List[ShadowRule]) -> AttributionBreakdown:
        """
        五因子归因分解 (纯算术)

        公式: shadow_pnl - real_pnl = noise + early_exit + late_exit + overtrading + missed_signals

        每个因子的符号: 正号 = Shadow 策略能赚更多 (用户行为偏离规则导致亏损)
        """
        if not rules:
            return AttributionBreakdown(0, 0, 0, 0, 0)

        # 使用所有规则的加权平均范围
        total_weight = sum(r.weight for r in rules)
        hold_lo = sum(r.holding_days_range[0] * r.weight for r in rules) / max(total_weight, 0.01)
        hold_hi = sum(r.holding_days_range[1] * r.weight for r in rules) / max(total_weight, 0.01)

        noise_pnl = 0.0
        early_pnl = 0.0
        late_pnl = 0.0

        for t in roundtrips:
            hold = t.get("holding_days", 0)
            pnl = t.get("pnl", 0)

            in_rule_range = hold_lo <= hold <= hold_hi

            if not in_rule_range:
                # Factor 1: noise trades - 规则外的交易
                noise_pnl -= pnl
            else:
                # Factor 2: early exit - 盈利但持仓过短
                if pnl > 0 and hold < hold_lo:
                    early_pnl += pnl * (hold_lo - hold) / max(hold_lo, 0.1)

                # Factor 3: late exit - 亏损但持仓过长
                if pnl < 0 and hold > hold_hi:
                    late_pnl += abs(pnl) * (hold - hold_hi) / max(hold_hi, 0.1)

        # Factor 4: overtrading - 过度交易
        total_trades = len(roundtrips)
        overtrading_pnl = 0.0
        if roundtrips:
            try:
                dates = set(t.get("entry_time", "")[:10] for t in roundtrips if t.get("entry_time"))
                span_days = max(1, len(dates))
                median_hold = sum(t.get("holding_days", 1) for t in roundtrips) / len(roundtrips)
                expected_trades = span_days / (2 * max(median_hold, 0.1))
                extra_trades = max(0, total_trades - expected_trades)
                if extra_trades > 0:
                    sorted_pnl = sorted(roundtrips, key=lambda x: abs(x.get("pnl", 0)))
                    for i in range(int(min(extra_trades, len(sorted_pnl)))):
                        overtrading_pnl -= sorted_pnl[i].get("pnl", 0)
            except Exception:
                pass

        # Factor 5: missed signals (残差)
        # 重构: missed = (规则内交易的理想 PnL) - (规则内交易的实际 PnL)
        # 简化: 使用规则平均 PnL 作为"理想"值
        real_pnl = sum(t.get("pnl", 0) for t in roundtrips)
        in_rule_trades = [t for t in roundtrips if hold_lo <= t.get("holding_days", 0) <= hold_hi]
        if in_rule_trades and rules:
            avg_rule_pnl = sum(r.avg_pnl_pct * r.weight for r in rules) / max(total_weight, 0.01)
            ideal_rule_pnl = sum(
                t.get("entry_price", 0) * t.get("size", 0) * avg_rule_pnl
                for t in in_rule_trades
            )
            actual_rule_pnl = sum(t.get("pnl", 0) for t in in_rule_trades)
            missed_pnl = ideal_rule_pnl - actual_rule_pnl
        else:
            missed_pnl = 0.0

        return AttributionBreakdown(
            noise_trades_pnl=round(noise_pnl, 2),
            early_exit_pnl=round(early_pnl, 2),
            late_exit_pnl=round(late_pnl, 2),
            overtrading_pnl=round(overtrading_pnl, 2),
            missed_signals_pnl=round(missed_pnl, 2),
        )

    # ═══════════════════════════════════════════════════
    #  行为偏差诊断 (保留原有逻辑)
    # ═══════════════════════════════════════════════════

    def _analyze_disposition_effect(self, roundtrips: List[Dict]) -> Dict[str, Any]:
        winners = [t for t in roundtrips if t.get("pnl", 0) > 0]
        losers = [t for t in roundtrips if t.get("pnl", 0) <= 0]

        if not winners or not losers:
            return {"severity": "insufficient_data", "ratio": None}

        avg_hold_w = self._avg_hold_hours(winners) / 24
        avg_hold_l = self._avg_hold_hours(losers) / 24
        ratio = avg_hold_l / avg_hold_w if avg_hold_w > 0 else 999

        severity = "low"
        if ratio > 3: severity = "high"
        elif ratio > 2: severity = "medium"

        return {
            "severity": severity,
            "avg_hold_winners_days": round(avg_hold_w, 1),
            "avg_hold_losers_days": round(avg_hold_l, 1),
            "loser_winner_hold_ratio": round(ratio, 2),
            "interpretation": f"亏损持仓是盈利的 {ratio:.1f}x — {'存在死扛倾向' if severity in ('medium', 'high') else '健康'}",
        }

    def _analyze_chasing(self, trades: List[Dict]) -> Dict[str, Any]:
        with_signal = [t for t in trades if t.get("signal_price") and t.get("entry_price")]
        if not with_signal:
            return {"severity": "insufficient_data", "avg_deviation_pct": 0}

        devs = [abs(t["entry_price"] - t["signal_price"]) / t["signal_price"] for t in with_signal]
        avg_dev = sum(devs) / len(devs)

        severity = "low"
        if avg_dev > 0.01: severity = "high"
        elif avg_dev > 0.005: severity = "medium"

        return {
            "severity": severity,
            "avg_deviation_pct": round(avg_dev * 100, 3),
            "interpretation": f"平均入场偏离 {avg_dev*100:.3f}% — {'存在追涨倾向' if severity in ('medium', 'high') else '入场点吻合度高'}",
        }

    def _analyze_anchoring(self, roundtrips: List[Dict]) -> Dict[str, Any]:
        losing = [t for t in roundtrips if t.get("pnl", 0) < 0]
        if not losing:
            return {"severity": "low", "anchored_ratio": 0}

        anchored = [t for t in losing if t.get("pnl_pct", 0) < -0.08]
        ratio = len(anchored) / len(losing)

        severity = "low"
        if ratio > 0.3: severity = "high"
        elif ratio > 0.15: severity = "medium"

        return {
            "severity": severity,
            "anchored_ratio": round(ratio, 2),
            "interpretation": f"{ratio:.0%} 的亏损单超过止损设计 — {'可能存在锚定效应' if severity in ('medium', 'high') else '止损纪律良好'}",
        }

    # ═══════════════════════════════════════════════════
    #  综合评分
    # ═══════════════════════════════════════════════════

    def _compute_behavioral_score(self, disposition, chasing, anchoring,
                                   attribution, rules) -> Dict[str, Any]:
        penalty = {"low": 0, "medium": 15, "high": 35, "insufficient_data": 5}
        score = 100
        score -= penalty.get(disposition.get("severity", "low"), 0) * 0.4
        score -= penalty.get(chasing.get("severity", "low"), 0) * 0.3
        score -= penalty.get(anchoring.get("severity", "low"), 0) * 0.3

        # 归因加分: 如果有显著的 noise/overtrading 问题，额外扣分
        if attribution:
            if abs(attribution.noise_trades_pnl) > 1000:
                score -= 10
            if abs(attribution.overtrading_pnl) > 500:
                score -= 5

        score = max(0, min(100, score))
        grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"

        return {"score": round(score), "grade": grade}

    def _build_summary(self, roundtrips, rules, attribution, score):
        parts = [f"📊 {len(roundtrips)} 笔成交 | 评分 {score['grade']} ({score['score']}/100)"]

        if rules:
            parts.append(f"规则数: {len(rules)}")
            if attribution:
                top = attribution.get_top_impact_factors(2)
                for name, val in top:
                    if abs(val) > 0:
                        parts.append(f"  {name}: ${val:+,.0f}")

        return " | ".join(parts)

    def get_trade_details(self, roundtrips: List[Dict], attribution: Optional[AttributionBreakdown] = None) -> List[Dict]:
        """
        逐笔交易详情 (用于日报推送)

        返回: 每笔交易的格式化信息
        """
        if not roundtrips:
            return []

        details = []
        for t in roundtrips:
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            direction = "多" if t.get("direction", "LONG") == "LONG" else "空"

            # 止盈止损推断
            exit_reason = t.get("exit_reason", "")
            if not exit_reason:
                if pnl_pct > 0.03:
                    exit_reason = "止盈"
                elif pnl_pct < -0.05:
                    exit_reason = "止损"
                else:
                    exit_reason = "平仓"

            details.append({
                "symbol": t.get("symbol", ""),
                "direction": direction,
                "entry_price": t.get("entry_price", 0),
                "exit_price": t.get("exit_price", 0),
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "pnl_emoji": pnl_emoji,
                "exit_reason": exit_reason,
                "holding_hours": t.get("holding_hours", 0),
                "entry_time": t.get("entry_time", ""),
                "exit_time": t.get("exit_time", ""),
                "size": t.get("size", 0),
                "trace_id": t.get("trace_id", ""),
                "strategy_id": t.get("strategy_id", ""),
            })

        return details

    @staticmethod
    def _avg_hold_hours(trades: List[Dict]) -> float:
        holds = []
        for t in trades:
            if t.get("entry_time") and t.get("exit_time"):
                try:
                    entry = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
                    exit_ = datetime.fromisoformat(str(t["exit_time"]).replace("Z", "+00:00"))
                    holds.append((exit_ - entry).total_seconds() / 3600)
                except (ValueError, TypeError):
                    pass
        return sum(holds) / len(holds) if holds else 0


def create_shadow_diagnostic() -> ShadowAccountDiagnostic:
    return ShadowAccountDiagnostic()
