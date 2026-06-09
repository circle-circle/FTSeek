"""
MAE/MFE 退出校准分析器 — 闭合反馈环路

读取已关闭交易的 MAE (max_adverse_excursion) 和 MFE (max_favorable_excursion)，
分类交易质量，生成退出参数调整建议。

数据流:
  trade_records (MAE/MFE) → 本模块分析 → config/exit_calibration.json
  → ExitMonitor 启动时加载 → 合并到 policy_dict → 影响下一笔交易

分类标准:
  NoiseShakeout:  MAE 占入场价 >2% 且最终盈利 → 止损太紧, 差点被噪声震出
  ProfitGiveback: MFE > 2× 实际捕获利润 → 追踪太松, 利润回吐过多
  WellCalibrated: MAE 小 + 无过度回吐 → 参数合适

Created: 2026-06-06
"""
import json
import logging
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from app.core.db_utils import get_main_db

logger = logging.getLogger("ftseek.exit_calibration")

# ── 默认值 ──
_CALIBRATION_OUTPUT = Path(__file__).resolve().parent.parent.parent / "config" / "exit_calibration.json"
_TRADE_DB = get_main_db()

# 分类阈值 (占 entry_price 百分比)
_MAE_NOISE_THRESHOLD = 0.02    # MAE > 2% of entry → 噪声震出风险
_MAE_TIGHT_THRESHOLD = 0.005   # MAE < 0.5% of entry → 止损可能太紧
_GIVEBACK_RATIO = 2.0          # MFE > 2× captured profit → 利润回吐过多


@dataclass
class TradeRecord:
    """从 DB 读取的单笔交易记录"""
    trade_id: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    pnl_pct: float
    exit_reason: str
    signal_confidence: float
    max_drawdown: float         # MAE (负数, 最大浮亏总金额)
    mfe: float                  # MFE (正数, 最大浮盈总金额)
    category: str = ""          # NoiseShakeout / ProfitGiveback / WellCalibrated
    detail: str = ""

    @property
    def total_cost(self) -> float:
        """总持仓成本 = entry_price × quantity"""
        return self.entry_price * max(self.quantity, 1)

    @property
    def mae_pct(self) -> float:
        """MAE 占总成本百分比"""
        return abs(self.max_drawdown) / self.total_cost if self.total_cost > 0 else 0

    @property
    def mfe_pct(self) -> float:
        """MFE 占总成本百分比"""
        return self.mfe / self.total_cost if self.total_cost > 0 else 0


@dataclass
class CalibrationReport:
    """校准分析报告"""
    generated_at: str = ""
    period_days: int = 7
    total_trades: int = 0
    analyzed_trades: int = 0    # 有 MAE/MFE 数据的交易数

    # 分类统计
    noise_shakeout_count: int = 0
    profit_giveback_count: int = 0
    well_calibrated_count: int = 0
    unclassified_count: int = 0

    # 比例
    noise_shakeout_pct: float = 0.0
    profit_giveback_pct: float = 0.0
    well_calibrated_pct: float = 0.0

    # 关键指标
    avg_mae_pct: float = 0.0       # 平均 MAE 占入场价百分比
    avg_mfe_pct: float = 0.0       # 平均 MFE 占入场价百分比
    avg_pnl_pct: float = 0.0       # 平均盈亏百分比
    win_rate: float = 0.0

    # 参数调整建议
    recommendations: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────
#  核心分析器
# ──────────────────────────────────────────────────────────────

class ExitCalibrationAnalyzer:
    """MAE/MFE 退出校准分析器"""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or str(_TRADE_DB)

    def analyze(self, days: int = 7) -> CalibrationReport:
        """分析最近 N 天的已关闭交易, 生成校准报告"""
        report = CalibrationReport(
            generated_at=datetime.now().isoformat(),
            period_days=days,
        )

        # 1. 从 DB 读取交易记录
        trades = self._load_trades(days)
        report.total_trades = len(trades)

        if not trades:
            logger.info("[校准分析] 无交易数据, 跳过")
            return report

        # 2. 分类每笔交易
        analyzed = []
        for t in trades:
            if t.max_drawdown == 0 and t.mfe == 0:
                report.unclassified_count += 1
                continue
            category, detail = self._classify(t)
            t.category = category
            t.detail = detail
            analyzed.append(t)

        report.analyzed_trades = len(analyzed)
        if not analyzed:
            logger.info("[校准分析] 无有效 MAE/MFE 数据, 跳过")
            return report

        # 3. 统计
        for t in analyzed:
            if t.category == "NoiseShakeout":
                report.noise_shakeout_count += 1
            elif t.category == "ProfitGiveback":
                report.profit_giveback_count += 1
            elif t.category == "WellCalibrated":
                report.well_calibrated_count += 1
            else:
                report.unclassified_count += 1

        n = len(analyzed)
        report.noise_shakeout_pct = report.noise_shakeout_count / n * 100
        report.profit_giveback_pct = report.profit_giveback_count / n * 100
        report.well_calibrated_pct = report.well_calibrated_count / n * 100

        # 4. 关键指标
        mae_pcts = [t.mae_pct * 100 for t in analyzed if t.total_cost > 0]
        mfe_pcts = [t.mfe_pct * 100 for t in analyzed if t.total_cost > 0 and t.mfe > 0]
        pnl_pcts = [t.pnl_pct * 100 for t in analyzed]
        wins = sum(1 for t in analyzed if t.realized_pnl > 0)

        report.avg_mae_pct = sum(mae_pcts) / len(mae_pcts) if mae_pcts else 0
        report.avg_mfe_pct = sum(mfe_pcts) / len(mfe_pcts) if mfe_pcts else 0
        report.avg_pnl_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0
        report.win_rate = wins / n if n > 0 else 0

        # 5. 生成建议
        report.recommendations = self._compute_recommendations(report, analyzed)

        # 6. 写入 JSON
        self._save_report(report)

        logger.info(
            f"[校准分析] 完成: {n} 笔交易 | "
            f"NoiseShakeout={report.noise_shakeout_count} ({report.noise_shakeout_pct:.0f}%) | "
            f"ProfitGiveback={report.profit_giveback_count} ({report.profit_giveback_pct:.0f}%) | "
            f"WellCalibrated={report.well_calibrated_count} ({report.well_calibrated_pct:.0f}%) | "
            f"胜率={report.win_rate:.0%} | 建议={len(report.recommendations)} 条"
        )
        return report

    def _load_trades(self, days: int) -> List[TradeRecord]:
        """从 trade_records 读取最近 N 天的已关闭交易"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, symbol, direction, entry_price, exit_price, quantity,
                       realized_pnl, pnl_pct, exit_reason, signal_confidence,
                       max_drawdown, metadata_json
                FROM trade_records
                WHERE exit_price IS NOT NULL
                  AND exit_time > ?
                ORDER BY exit_time DESC
                """,
                (cutoff,),
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"[校准分析] DB 读取失败: {e}")
            return []

        trades = []
        for row in rows:
            mfe = 0.0
            mae = float(row["max_drawdown"] or 0)
            # MFE 在 metadata_json 中
            meta_str = row["metadata_json"]
            if meta_str:
                try:
                    meta = json.loads(meta_str)
                    mfe = float(meta.get("max_favorable_excursion", 0) or 0)
                    # 如果 max_drawdown 列没有 MAE, 从 metadata 取
                    if mae == 0:
                        mae = float(meta.get("max_adverse_excursion", 0) or 0)
                except (json.JSONDecodeError, TypeError):
                    pass

            trades.append(TradeRecord(
                trade_id=row["id"],
                symbol=row["symbol"],
                direction=row["direction"],
                entry_price=float(row["entry_price"] or 0),
                exit_price=float(row["exit_price"] or 0),
                quantity=float(row["quantity"] or 1),
                realized_pnl=float(row["realized_pnl"] or 0),
                pnl_pct=float(row["pnl_pct"] or 0),
                exit_reason=row["exit_reason"] or "",
                signal_confidence=float(row["signal_confidence"] or 0),
                max_drawdown=mae,       # 负数 = 最大浮亏总金额
                mfe=mfe,                # 正数 = 最大浮盈总金额
            ))
        return trades

    def _classify(self, t: TradeRecord) -> Tuple[str, str]:
        """分类单笔交易"""
        if t.total_cost <= 0:
            return "Unclassified", "total_cost=0"

        mae_pct = t.mae_pct   # MAE 占总成本比例
        mfe_pct = t.mfe_pct   # MFE 占总成本比例

        # NoiseShakeout: MAE 大但最终盈利 → 止损太紧, 差点被震出
        if mae_pct > _MAE_NOISE_THRESHOLD and t.realized_pnl > 0:
            return (
                "NoiseShakeout",
                f"MAE={mae_pct*100:.1f}% > {_MAE_NOISE_THRESHOLD*100:.0f}% 但最终盈利 "
                f"PnL={t.pnl_pct*100:.1f}% → 止损可能太紧"
            )

        # ProfitGiveback: MFE 远大于实际捕获利润 → 追踪太松
        if t.mfe > 0 and t.realized_pnl > 0 and t.mfe > abs(t.realized_pnl) * _GIVEBACK_RATIO:
            return (
                "ProfitGiveback",
                f"MFE=${t.mfe:.2f} > {_GIVEBACK_RATIO:.0f}× 实际利润=${t.realized_pnl:.2f} "
                f"→ 利润回吐 ${(t.mfe - t.realized_pnl):.2f}"
            )

        # WellCalibrated: MAE 小 + 无过度回吐
        if mae_pct < _MAE_NOISE_THRESHOLD and (
            t.mfe == 0 or t.mfe < abs(t.realized_pnl) * _GIVEBACK_RATIO or t.realized_pnl <= 0
        ):
            return "WellCalibrated", f"MAE={mae_pct*100:.1f}% 合理"

        return "Unclassified", f"MAE={mae_pct*100:.1f}% MFE={mfe_pct*100:.1f}%"

    def _compute_recommendations(
        self, report: CalibrationReport, trades: List[TradeRecord]
    ) -> Dict[str, Any]:
        """基于分类统计生成参数调整建议"""
        recs = {}
        n = report.analyzed_trades
        if n < 5:
            recs["_note"] = f"样本量不足 ({n} < 5), 建议仅供参考"

        # ── 1. ATR 倍数建议 ──
        noise_pct = report.noise_shakeout_count / max(n, 1)
        if noise_pct > 0.25:
            recs["atr_stop_multiplier"] = {
                "action": "widen",
                "current": 2.5,
                "recommended": 3.0,
                "reason": f"NoiseShakeout 占比 {noise_pct:.0%} > 25%, 止损太紧导致频繁被震出",
                "confidence": "high" if n >= 15 else "medium",
            }
        elif noise_pct < 0.05 and report.avg_mae_pct < 0.5:
            recs["atr_stop_multiplier"] = {
                "action": "tighten",
                "current": 2.5,
                "recommended": 2.0,
                "reason": f"NoiseShakeout 占比仅 {noise_pct:.0%}, 平均 MAE 仅 {report.avg_mae_pct:.1f}%, 可收紧",
                "confidence": "high" if n >= 15 else "medium",
            }

        # ── 2. 追踪止损建议 ──
        giveback_pct = report.profit_giveback_count / max(n, 1)
        if giveback_pct > 0.20:
            recs["trailing_stop"] = {
                "action": "tighten",
                "reason": (
                    f"ProfitGiveback 占比 {giveback_pct:.0%} > 20%, "
                    f"追踪止损太松导致利润大量回吐"
                ),
                "suggestion": "降低 progressive_trailing 的 tier_compact_multiplier",
                "confidence": "high" if n >= 15 else "medium",
            }

        # ── 3. 按退出原因分析 ──
        reason_stats = {}
        for t in trades:
            reason = t.exit_reason.split("_")[0] if t.exit_reason else "unknown"
            if reason not in reason_stats:
                reason_stats[reason] = {"count": 0, "wins": 0, "total_pnl": 0}
            reason_stats[reason]["count"] += 1
            if t.realized_pnl > 0:
                reason_stats[reason]["wins"] += 1
            reason_stats[reason]["total_pnl"] += t.realized_pnl

        bad_reasons = []
        for reason, stats in reason_stats.items():
            if stats["count"] >= 3:
                wr = stats["wins"] / stats["count"]
                if wr < 0.25 and stats["total_pnl"] < 0:
                    bad_reasons.append({
                        "reason": reason,
                        "count": stats["count"],
                        "win_rate": round(wr, 2),
                        "total_pnl": round(stats["total_pnl"], 2),
                    })
        if bad_reasons:
            recs["exit_reason_review"] = {
                "action": "review",
                "bad_reasons": bad_reasons,
                "reason": "以下退出原因胜率低且总亏损, 需检查对应退出规则参数",
            }

        # ── 4. 按置信度分析 ──
        conf_buckets = {"high": [], "medium": [], "low": []}
        for t in trades:
            c = t.signal_confidence
            if c >= 0.80:
                conf_buckets["high"].append(t)
            elif c >= 0.65:
                conf_buckets["medium"].append(t)
            else:
                conf_buckets["low"].append(t)

        conf_stats = {}
        for bucket, bucket_trades in conf_buckets.items():
            if bucket_trades:
                wins = sum(1 for t in bucket_trades if t.realized_pnl > 0)
                conf_stats[bucket] = {
                    "count": len(bucket_trades),
                    "win_rate": round(wins / len(bucket_trades), 2),
                    "avg_pnl": round(sum(t.realized_pnl for t in bucket_trades) / len(bucket_trades), 2),
                }
        if conf_stats:
            recs["confidence_tier_stats"] = conf_stats

        return recs

    def _save_report(self, report: CalibrationReport):
        """写入 config/exit_calibration.json"""
        try:
            _CALIBRATION_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            with open(_CALIBRATION_OUTPUT, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
            logger.info(f"[校准分析] 报告已写入 {_CALIBRATION_OUTPUT}")
        except Exception as e:
            logger.warning(f"[校准分析] 报告写入失败: {e}")


# ──────────────────────────────────────────────────────────────
#  Exit Monitor 消费方: 加载 + 应用校准
# ──────────────────────────────────────────────────────────────

def load_calibration() -> Dict[str, Any]:
    """加载最近的校准报告 (Exit Monitor 启动时调用)"""
    if not _CALIBRATION_OUTPUT.exists():
        return {}
    try:
        with open(_CALIBRATION_OUTPUT, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 过期检查: 超过 14 天的报告不加载
        gen_at = data.get("generated_at", "")
        if gen_at:
            age_days = (datetime.now() - datetime.fromisoformat(gen_at)).days
            if age_days > 14:
                logger.info(f"[校准] 报告已过期 ({age_days} 天), 忽略")
                return {}
        return data
    except Exception as e:
        logger.debug(f"[校准] 加载失败: {e}")
        return {}


def apply_calibration_to_policy(policy_dict: Dict, calibration: Dict) -> Dict:
    """将校准建议应用到 policy_dict (auto_apply=true 时调用)

    目前仅应用 atr_stop_multiplier 调整。
    其他建议 (trailing_stop, exit_reason_review) 需要人工介入。
    """
    if not calibration:
        return policy_dict

    recs = calibration.get("recommendations", {})

    # ATR 倍数调整
    atr_rec = recs.get("atr_stop_multiplier", {})
    if atr_rec and atr_rec.get("confidence") in ("high", "medium"):
        recommended = float(atr_rec.get("recommended", 2.5))
        try:
            policy_dict.setdefault("exit_rules", {}).setdefault("atr_stop", {}).setdefault(
                "multiplier_by_profit", {}
            )["initial"] = recommended
            logger.info(
                f"[校准·应用] atr_stop_multiplier 调整为 {recommended} "
                f"(原因: {atr_rec.get('reason', '')})"
            )
        except Exception as e:
            logger.warning(f"[校准·应用] atr_stop_multiplier 调整失败: {e}")

    return policy_dict
