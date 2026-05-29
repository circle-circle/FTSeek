"""
信号质量追踪与反馈闭环 (Signal Quality Tracker)

Task #12 实现 (2026-05-27)

目标:
  - 量化各策略信号胜率, 找出最可靠的因子组合
  - 提供 confidence -> 实际胜率映射表 (每日更新)
  - 输出动态阈值调整建议 (仅建议, 不自动改配置)
  - 识别连续亏损策略, 建议降权 / 禁用

数据源:
  /opt/ftseek/FTSeek-US/data/ftseek_us.db -> trade_records
  关键字段: entry_reason (JSON 数组), signal_confidence, realized_pnl,
           direction, owner_strategy, source_type, trade_date

输出:
  /opt/ftseek/FTSeek-US/data/signal_quality/
    - confidence_winrate_map_YYYY-MM-DD.json     # confidence 桶 -> 胜率
    - strategy_performance_YYYY-MM-DD.json       # 各策略汇总
    - factor_contribution_YYYY-MM-DD.json        # 因子组合贡献度
    - threshold_recommendations_YYYY-MM-DD.json  # 阈值调整建议
    - daily_summary_YYYY-MM-DD.json              # 一站式聚合
    - latest.json (软链/拷贝)                     # 最近一份

使用:
  from app.analysis.signal_quality_tracker import SignalQualityTracker
  tracker = SignalQualityTracker()
  summary = tracker.run_daily_update()
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
)
DEFAULT_OUTPUT_DIR = str(
    Path(__file__).resolve().parent.parent.parent / "data" / "signal_quality"
)

# confidence 分桶 (左闭右开, 末桶右闭)
CONFIDENCE_BUCKETS: List[Tuple[float, float, str]] = [
    (0.0, 0.3, "0-0.3"),
    (0.3, 0.5, "0.3-0.5"),
    (0.5, 0.7, "0.5-0.7"),
    (0.7, 1.0001, "0.7-1.0"),
]

# 策略类别归一化 (entry_reason / owner_strategy / source_type 联合分类)
KNOWN_CATEGORIES = (
    "technical",
    "llm_vote",
    "battle_plan",
    "news",
    "scalping",
    "其他",
)

# 滚动窗口
ROLLING_WINDOWS = (7, 30)

# 连续亏损警报阈值 (近 N 笔)
RECENT_TRADES_FOR_DISABLE = 10
DISABLE_WINRATE_THRESHOLD = 0.20

# 阈值调整建议: 默认基线 (与 risk 配置保持一致)
DEFAULT_MIN_CONFIDENCE = 0.50


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_entry_reason(raw: Optional[str]) -> List[str]:
    """entry_reason 在 DB 中存为 JSON 数组字符串, 兼容空值与裸字符串"""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x).strip().lower() for x in val if x is not None]
        if isinstance(val, str):
            return [val.strip().lower()]
    except (json.JSONDecodeError, TypeError):
        pass
    return [str(raw).strip().lower()]


def _classify_strategy(
    entry_reasons: List[str],
    owner_strategy: Optional[str],
    source_type: Optional[str],
    decision_reason: Optional[str],
) -> str:
    """
    将单笔交易归类到 KNOWN_CATEGORIES 之一
    优先级: scalping (硬归类) > entry_reason 关键词 > 其他
    """
    owner = (owner_strategy or "").lower()
    src = (source_type or "").lower()
    if "scalp" in owner or "scalp" in src:
        return "scalping"

    joined = " ".join(entry_reasons + [decision_reason or ""]).lower()

    # 优先关键字匹配
    if any(k in joined for k in ("llm_vote", "vote", "consensus")):
        return "llm_vote"
    if any(k in joined for k in ("battle_plan", "battle", "plan")):
        return "battle_plan"
    if any(k in joined for k in ("news", "catalyst", "earning")):
        return "news"
    # 常见技术因子关键字
    technical_kw = (
        "rsi", "vwap", "bollinger", "macd", "ema", "sma",
        "atr", "breakout", "reversal", "trend", "momentum",
    )
    if any(k in joined for k in technical_kw):
        return "technical"
    return "其他"


def _bucket_for_confidence(c: float) -> str:
    for lo, hi, label in CONFIDENCE_BUCKETS:
        if lo <= c < hi:
            return label
    return CONFIDENCE_BUCKETS[-1][2]


def _empty_stats() -> Dict[str, Any]:
    return {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_pnl": 0.0,
        "total_pnl": 0.0,
        "max_loss": 0.0,
        "max_win": 0.0,
    }


def _accumulate(stats: Dict[str, Any], pnl: float) -> None:
    stats["total"] += 1
    stats["total_pnl"] += pnl
    if pnl > 0:
        stats["wins"] += 1
        if pnl > stats["max_win"]:
            stats["max_win"] = pnl
    elif pnl < 0:
        stats["losses"] += 1
        if pnl < stats["max_loss"]:
            stats["max_loss"] = pnl


def _finalize(stats: Dict[str, Any]) -> Dict[str, Any]:
    n = stats["total"]
    if n > 0:
        stats["win_rate"] = round(stats["wins"] / n, 4)
        stats["avg_pnl"] = round(stats["total_pnl"] / n, 4)
    stats["total_pnl"] = round(stats["total_pnl"], 4)
    stats["max_loss"] = round(stats["max_loss"], 4)
    stats["max_win"] = round(stats["max_win"], 4)
    return stats


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class SignalQualityTracker:
    """
    信号质量追踪器 (单例友好, 但支持多实例以便测试)

    主要方法:
      - run_daily_update(as_of=None): 一站式生成所有报告并落盘, 返回 summary dict
      - build_confidence_winrate_map()
      - build_strategy_performance()
      - build_factor_contribution()
      - build_threshold_recommendations()
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        min_confidence_baseline: float = DEFAULT_MIN_CONFIDENCE,
    ):
        self.db_path = db_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.min_confidence_baseline = min_confidence_baseline
        self._lock = threading.Lock()

    # ── 数据加载 ────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_closed_trades(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """读取已平仓的交易记录 (exit_price IS NOT NULL)"""
        if not os.path.exists(self.db_path):
            logger.warning(
                "[SignalQuality] DB 不存在, 跳过加载: %s", self.db_path
            )
            return []

        conditions = ["exit_price IS NOT NULL"]
        params: List[Any] = []
        if since:
            conditions.append("trade_date >= ?")
            params.append(since)
        if until:
            conditions.append("trade_date <= ?")
            params.append(until)
        where = " AND ".join(conditions)

        sql = f"""
            SELECT trade_date, symbol, direction, entry_time, exit_time,
                   realized_pnl, pnl_pct, signal_confidence, signal_strength,
                   entry_reason, exit_reason, owner_strategy, source_type,
                   decision_reason, strategy_id
              FROM trade_records
             WHERE {where}
             ORDER BY trade_date ASC, COALESCE(exit_time, entry_time) ASC
        """

        conn = self._get_conn()
        try:
            cursor = conn.execute(sql, params)
            rows = [dict(r) for r in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("[SignalQuality] 查询 trade_records 失败: %s", e)
            return []
        finally:
            conn.close()

        # 解析 / 归类
        for r in rows:
            r["_entry_reasons"] = _parse_entry_reason(r.get("entry_reason"))
            r["_category"] = _classify_strategy(
                r["_entry_reasons"],
                r.get("owner_strategy"),
                r.get("source_type"),
                r.get("decision_reason"),
            )
            r["_pnl"] = _safe_float(r.get("realized_pnl"))
            r["_confidence"] = _safe_float(r.get("signal_confidence"))
        return rows

    # ── (a) confidence -> 胜率映射 ──────────────────────────────

    def build_confidence_winrate_map(
        self, trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """按 confidence 分桶计算实际胜率"""
        buckets: Dict[str, Dict[str, Any]] = {
            label: _empty_stats() for _, _, label in CONFIDENCE_BUCKETS
        }
        for t in trades:
            label = _bucket_for_confidence(t["_confidence"])
            _accumulate(buckets[label], t["_pnl"])

        for s in buckets.values():
            _finalize(s)

        # 单调性检查: 期望 confidence 越高 -> 胜率越高
        ordered_labels = [lbl for _, _, lbl in CONFIDENCE_BUCKETS]
        win_rates = [buckets[l]["win_rate"] for l in ordered_labels]
        is_monotonic = all(
            win_rates[i] <= win_rates[i + 1]
            for i in range(len(win_rates) - 1)
            if buckets[ordered_labels[i]]["total"] > 0
            and buckets[ordered_labels[i + 1]]["total"] > 0
        )

        return {
            "buckets": buckets,
            "ordered_labels": ordered_labels,
            "monotonic": is_monotonic,
            "total_samples": sum(s["total"] for s in buckets.values()),
        }

    # ── (b) 每日策略表现汇总 ─────────────────────────────────────

    def build_strategy_performance(
        self, trades: List[Dict[str, Any]], as_of: date
    ) -> Dict[str, Any]:
        """按 entry_reason 类别分组统计 + 7d/30d 滚动窗口"""
        all_stats: Dict[str, Dict[str, Any]] = {
            cat: _empty_stats() for cat in KNOWN_CATEGORIES
        }
        rolling: Dict[int, Dict[str, Dict[str, Any]]] = {
            w: {cat: _empty_stats() for cat in KNOWN_CATEGORIES}
            for w in ROLLING_WINDOWS
        }

        windows_since = {
            w: (as_of - timedelta(days=w)).isoformat() for w in ROLLING_WINDOWS
        }

        for t in trades:
            cat = t["_category"]
            if cat not in all_stats:
                cat = "其他"
            _accumulate(all_stats[cat], t["_pnl"])

            td = t.get("trade_date") or ""
            for w in ROLLING_WINDOWS:
                if td >= windows_since[w]:
                    _accumulate(rolling[w][cat], t["_pnl"])

        for s in all_stats.values():
            _finalize(s)
        for w in ROLLING_WINDOWS:
            for s in rolling[w].values():
                _finalize(s)

        return {
            "all_time": all_stats,
            "rolling": {
                f"{w}d": rolling[w] for w in ROLLING_WINDOWS
            },
            "as_of": as_of.isoformat(),
        }

    # ── (c) 因子贡献度分解 ──────────────────────────────────────

    def build_factor_contribution(
        self, trades: List[Dict[str, Any]], top_k: int = 15
    ) -> Dict[str, Any]:
        """
        分析因子组合胜率
          - 完整组合 (排序后 + 号连接): 'rsi+vwap+bollinger'
          - 单因子贡献 (拆分后单独统计)
        """
        combo_stats: Dict[str, Dict[str, Any]] = {}
        single_stats: Dict[str, Dict[str, Any]] = {}

        for t in trades:
            reasons = sorted(set(t["_entry_reasons"]))
            if not reasons:
                continue
            combo_key = "+".join(reasons)
            combo_stats.setdefault(combo_key, _empty_stats())
            _accumulate(combo_stats[combo_key], t["_pnl"])

            for r in reasons:
                single_stats.setdefault(r, _empty_stats())
                _accumulate(single_stats[r], t["_pnl"])

        for s in combo_stats.values():
            _finalize(s)
        for s in single_stats.values():
            _finalize(s)

        # 仅保留样本数 >=3 的组合, 按 win_rate * sqrt(total) 排序 (兼顾胜率与样本量)
        def _score(item):
            stats = item[1]
            n = stats["total"]
            if n < 3:
                return -1.0
            return stats["win_rate"] * (n ** 0.5)

        top_combos = sorted(combo_stats.items(), key=_score, reverse=True)
        top_combos = [(k, v) for k, v in top_combos if v["total"] >= 3][:top_k]
        top_singles = sorted(single_stats.items(), key=_score, reverse=True)
        top_singles = [(k, v) for k, v in top_singles if v["total"] >= 3][:top_k]

        return {
            "top_combos": [{"factors": k, **v} for k, v in top_combos],
            "top_singles": [{"factor": k, **v} for k, v in top_singles],
            "combo_count": len(combo_stats),
            "single_count": len(single_stats),
        }

    # ── (d) 动态阈值调整建议 ─────────────────────────────────────

    def build_threshold_recommendations(
        self,
        trades: List[Dict[str, Any]],
        as_of: date,
        confidence_map: Dict[str, Any],
        strategy_perf: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        基于近 30 天表现给出建议:
          1. 推荐 min_signal_confidence: 取近 30 天胜率 >= 50% 的最低桶下界
          2. 策略禁用建议: 近 N 笔胜率 < 20% -> warn
        注: 仅输出建议, 不写配置
        """
        thirty_days_ago = (as_of - timedelta(days=30)).isoformat()
        recent_30d = [t for t in trades if (t.get("trade_date") or "") >= thirty_days_ago]

        # 1) 推荐 min_confidence
        recent_buckets: Dict[str, Dict[str, Any]] = {
            label: _empty_stats() for _, _, label in CONFIDENCE_BUCKETS
        }
        for t in recent_30d:
            _accumulate(
                recent_buckets[_bucket_for_confidence(t["_confidence"])],
                t["_pnl"],
            )
        for s in recent_buckets.values():
            _finalize(s)

        suggested_min_conf: Optional[float] = None
        rationale_parts: List[str] = []
        for lo, _hi, label in CONFIDENCE_BUCKETS:
            stats = recent_buckets[label]
            if stats["total"] >= 5 and stats["win_rate"] >= 0.5:
                suggested_min_conf = lo
                rationale_parts.append(
                    f"近30d 桶 {label} 胜率 {stats['win_rate']:.2%} (n={stats['total']})"
                )
                break

        if suggested_min_conf is None:
            suggested_min_conf = self.min_confidence_baseline
            rationale_parts.append("近30d 无桶达到 50% 胜率, 维持基线")

        # 2) 策略级降权 / 禁用建议 (按 entry_reason 类别分组取近 N 笔)
        per_category_recent: Dict[str, List[Dict[str, Any]]] = {
            cat: [] for cat in KNOWN_CATEGORIES
        }
        for t in trades:
            cat = t["_category"] if t["_category"] in per_category_recent else "其他"
            per_category_recent[cat].append(t)

        strategy_actions: List[Dict[str, Any]] = []
        for cat, lst in per_category_recent.items():
            recent = lst[-RECENT_TRADES_FOR_DISABLE:]
            if len(recent) < RECENT_TRADES_FOR_DISABLE:
                continue
            wins = sum(1 for x in recent if x["_pnl"] > 0)
            wr = wins / len(recent)
            if wr < DISABLE_WINRATE_THRESHOLD:
                strategy_actions.append({
                    "category": cat,
                    "recent_trades": len(recent),
                    "recent_win_rate": round(wr, 4),
                    "recommendation": "DISABLE_OR_REDUCE_WEIGHT",
                    "severity": "HIGH",
                    "rationale": (
                        f"近 {len(recent)} 笔胜率 {wr:.2%} < "
                        f"{DISABLE_WINRATE_THRESHOLD:.0%} 阈值"
                    ),
                })

        # 3) confidence 单调性异常告警
        anomalies: List[str] = []
        if confidence_map.get("total_samples", 0) >= 20 and not confidence_map.get(
            "monotonic"
        ):
            anomalies.append(
                "confidence 桶胜率非单调递增 — 模型置信度可能与真实胜率脱钩"
            )

        return {
            "as_of": as_of.isoformat(),
            "suggested_min_signal_confidence": round(float(suggested_min_conf), 3),
            "current_baseline": self.min_confidence_baseline,
            "rationale": "; ".join(rationale_parts),
            "strategy_actions": strategy_actions,
            "anomalies": anomalies,
            "applied": False,  # 安全起见不自动落地
            "note": "本建议仅供参考, 不会自动修改 config; 由人工或灰度流程审批后再落地",
        }

    # ── 入口: 一站式生成 ────────────────────────────────────────

    def run_daily_update(
        self,
        as_of: Optional[date] = None,
        history_days: int = 90,
    ) -> Dict[str, Any]:
        """
        生成全部报告并写盘
        Args:
            as_of: 截止日期 (默认今日 ET 视角, 由调用方传入)
            history_days: 全量分析回溯窗口 (默认 90 天)
        Returns:
            summary dict
        """
        with self._lock:
            as_of = as_of or date.today()
            since = (as_of - timedelta(days=history_days)).isoformat()
            trades = self._fetch_closed_trades(since=since)

            confidence_map = self.build_confidence_winrate_map(trades)
            strategy_perf = self.build_strategy_performance(trades, as_of)
            factor_contrib = self.build_factor_contribution(trades)
            recommendations = self.build_threshold_recommendations(
                trades, as_of, confidence_map, strategy_perf
            )

            tag = as_of.isoformat()
            generated_at = datetime.now().isoformat(timespec="seconds")
            summary = {
                "as_of": tag,
                "generated_at": generated_at,
                "history_days": history_days,
                "total_closed_trades": len(trades),
                "confidence_winrate_map": confidence_map,
                "strategy_performance": strategy_perf,
                "factor_contribution": factor_contrib,
                "threshold_recommendations": recommendations,
            }

            self._write(f"confidence_winrate_map_{tag}.json", confidence_map)
            self._write(f"strategy_performance_{tag}.json", strategy_perf)
            self._write(f"factor_contribution_{tag}.json", factor_contrib)
            self._write(f"threshold_recommendations_{tag}.json", recommendations)
            self._write(f"daily_summary_{tag}.json", summary)
            self._write("latest.json", summary)

            self._log_summary(summary)
            return summary

    # ── 工具 ──────────────────────────────────────────────────

    def _write(self, filename: str, payload: Dict[str, Any]) -> None:
        path = self.output_dir / filename
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        except OSError as e:
            logger.error("[SignalQuality] 写盘失败 %s: %s", path, e)

    def _log_summary(self, summary: Dict[str, Any]) -> None:
        cmap = summary["confidence_winrate_map"]["buckets"]
        perf_30d = summary["strategy_performance"]["rolling"].get("30d", {})
        recs = summary["threshold_recommendations"]

        bucket_str = ", ".join(
            f"{lbl}: {cmap[lbl]['win_rate']:.1%}(n={cmap[lbl]['total']})"
            for _, _, lbl in CONFIDENCE_BUCKETS
        )
        perf_str = ", ".join(
            f"{cat}:{stats['win_rate']:.1%}(n={stats['total']})"
            for cat, stats in perf_30d.items()
            if stats["total"] > 0
        ) or "(无 30d 已平仓)"

        actions = recs.get("strategy_actions") or []
        action_str = (
            "; ".join(f"{a['category']}->{a['recommendation']}" for a in actions)
            or "无"
        )

        logger.info(
            "[SignalQuality] 每日更新完成 | as_of=%s | trades=%d | "
            "confidence: %s | 30d: %s | 推荐 min_conf=%s | actions=%s",
            summary["as_of"],
            summary["total_closed_trades"],
            bucket_str,
            perf_str,
            recs.get("suggested_min_signal_confidence"),
            action_str,
        )


# ---------------------------------------------------------------------------
# 单例 helper (供 scheduler 使用)
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton: Optional[SignalQualityTracker] = None


def get_signal_quality_tracker() -> SignalQualityTracker:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SignalQualityTracker()
    return _singleton


def run_signal_quality_daily(as_of: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """供 scheduler / CLI 调用的薄包装, 出错不抛异常"""
    try:
        tracker = get_signal_quality_tracker()
        return tracker.run_daily_update(as_of=as_of)
    except Exception as e:  # noqa: BLE001 - 防御性, 不影响主流程
        logger.error("[SignalQuality] 每日更新失败: %s", e, exc_info=True)
        return None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_signal_quality_daily()
    if result:
        print(json.dumps(
            {
                "as_of": result["as_of"],
                "total_closed_trades": result["total_closed_trades"],
                "suggested_min_conf": result["threshold_recommendations"].get(
                    "suggested_min_signal_confidence"
                ),
                "strategy_actions": result["threshold_recommendations"].get(
                    "strategy_actions"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ))
