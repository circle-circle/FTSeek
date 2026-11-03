"""推荐准确率追踪器 (Accuracy Tracker) — 2026-05-28

职责:
    1. 存储每日推荐 (含 entry_zone / target / stop_loss / direction)
    2. 次日盘前自动验证: 昨日推荐是否命中目标/止损
    3. 计算滚动准确率 (7日/30日)
    4. 为推送卡片提供 rolling_accuracy 数据

数据流:
    推送前 → store_recommendations() → 存 JSON
    次日推送前 → verify_yesterday() → 评分 + 更新滚动统计
    推送时 → get_rolling_accuracy() → 注入卡片

设计原则:
    - 纯文件系统存储 (JSON), 不依赖 DB
    - 验证逻辑简单: 收盘价 vs target/stop_loss
    - 异常返回空, 不影响主流程
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 存储路径 ──────────────────────────────────────────────
DEFAULT_STORE_DIR = "data/recommendations/accuracy"
ROLLING_WINDOW_DAYS = 30


class AccuracyTracker:
    """推荐准确率追踪器"""

    def __init__(self, store_dir: Optional[str] = None):
        if store_dir:
            self._store_dir = Path(store_dir)
        else:
            project_root = Path(__file__).resolve().parent.parent.parent
            self._store_dir = project_root / DEFAULT_STORE_DIR
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[AccuracyTracker] 创建目录失败: {e}")

    # ─── 存储推荐 ──────────────────────────────────────────
    def store_recommendations(self, recommendations: Dict[str, Any]) -> bool:
        """
        存储今日推荐供次日验证。

        Args:
            recommendations: synthesizer 输出的推荐字典

        Returns:
            是否成功存储
        """
        try:
            date_str = recommendations.get("date") or datetime.now().strftime("%Y-%m-%d")
            recs = recommendations.get("recommendations") or []

            if not recs:
                logger.debug(f"[AccuracyTracker] {date_str} 无推荐标的, 跳过存储")
                return False

            # 提取可验证字段
            stored = {
                "date": date_str,
                "system_conviction": recommendations.get("system_conviction"),
                "market_regime": recommendations.get("market_regime"),
                "targets": [],
                "stored_at": datetime.now().isoformat(timespec="seconds"),
            }

            for rec in recs:
                target = {
                    "symbol": rec.get("symbol", "?"),
                    "action": rec.get("action") or rec.get("direction", "BUY"),
                    "entry_zone": rec.get("entry_zone", ""),
                    "target": rec.get("target", ""),
                    "stop_loss": rec.get("stop_loss", ""),
                    "confidence_score": rec.get("confidence_score"),
                    "confidence_level": rec.get("confidence_level"),
                    "risk_reward": rec.get("risk_reward"),
                }
                stored["targets"].append(target)

            file_path = self._store_dir / f"{date_str}.json"
            file_path.write_text(
                json.dumps(stored, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                f"[AccuracyTracker] 已存储 {date_str} 推荐: "
                f"{len(stored['targets'])} 只标的"
            )
            return True

        except Exception as e:
            logger.warning(f"[AccuracyTracker] 存储失败: {e}")
            return False

    # ─── 验证昨日推荐 ──────────────────────────────────────
    def verify_yesterday(
        self, data_router: Any = None
    ) -> Dict[str, Any]:
        """
        验证昨日推荐 vs 实际价格。

        Returns:
            {
                "date": "2026-05-28",
                "results": [
                    {"symbol": "NVDA", "action": "BUY", "entry": 210, "close": 215,
                     "hit_target": True, "hit_stop": False, "return_pct": 2.4, "verdict": "WIN"},
                    ...
                ],
                "correct": 2,
                "total": 3,
                "accuracy_pct": 66.7,
            }
        """
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return self.verify_date(yesterday, data_router)

    def verify_date(
        self, date_str: str, data_router: Any = None
    ) -> Dict[str, Any]:
        """验证指定日期的推荐"""
        file_path = self._store_dir / f"{date_str}.json"
        if not file_path.exists():
            return {}

        try:
            stored = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[AccuracyTracker] 读取 {date_str} 失败: {e}")
            return {}

        targets = stored.get("targets", [])
        if not targets:
            return {}

        router = data_router or self._get_data_router()
        if router is None:
            logger.debug("[AccuracyTracker] 无行情数据源, 跳过验证")
            return {}

        results = []
        correct = 0
        total = 0

        for tgt in targets:
            symbol = tgt.get("symbol", "?")
            action = (tgt.get("action") or "BUY").upper()

            # 获取收盘价 (或最新价)
            try:
                quote = router.get_quote(
                    f"US.{symbol}" if not symbol.startswith("US.") else symbol
                )
                if not quote:
                    continue
                close_price = float(
                    quote.get("prev_close", 0)
                    or quote.get("price", 0)
                    or quote.get("last_price", 0)
                    or 0
                )
                if close_price <= 0:
                    continue
            except Exception:
                continue

            # 解析入场价 (取中位)
            entry_mid = self._parse_entry_mid(tgt.get("entry_zone"))
            target_price = self._safe_float(tgt.get("target"))
            stop_price = self._safe_float(tgt.get("stop_loss"))

            if entry_mid is None or entry_mid <= 0:
                continue

            total += 1

            # 计算收益率
            if action in ("BUY", "LONG"):
                return_pct = (close_price - entry_mid) / entry_mid * 100
                hit_target = target_price and close_price >= target_price
                hit_stop = stop_price and close_price <= stop_price
                is_correct = return_pct > 0
            else:  # SELL / SHORT
                return_pct = (entry_mid - close_price) / entry_mid * 100
                hit_target = target_price and close_price <= target_price
                hit_stop = stop_price and close_price >= stop_price
                is_correct = return_pct > 0

            if is_correct:
                correct += 1

            # 判定
            if hit_target:
                verdict = "WIN (命中目标)"
            elif hit_stop:
                verdict = "LOSS (触及止损)"
            elif is_correct:
                verdict = "WIN (盈利未达目标)"
            else:
                verdict = "LOSS (浮亏)"

            results.append({
                "symbol": symbol,
                "action": action,
                "entry_mid": round(entry_mid, 2),
                "close": round(close_price, 2),
                "target": target_price,
                "stop_loss": stop_price,
                "return_pct": round(return_pct, 2),
                "hit_target": bool(hit_target),
                "hit_stop": bool(hit_stop),
                "is_correct": is_correct,
                "verdict": verdict,
            })

        if not results:
            return {}

        report = {
            "date": date_str,
            "results": results,
            "correct": correct,
            "total": total,
            "accuracy_pct": round(correct / total * 100, 1) if total > 0 else 0,
            "avg_return_pct": round(
                sum(r["return_pct"] for r in results) / total, 2
            ) if total > 0 else 0,
            "verified_at": datetime.now().isoformat(timespec="seconds"),
        }

        # 存储验证结果
        self._save_verification(date_str, report)

        logger.info(
            f"[AccuracyTracker] {date_str} 验证完成: "
            f"{correct}/{total} ({report['accuracy_pct']:.1f}%) "
            f"平均收益: {report['avg_return_pct']:.2f}%"
        )
        return report

    # ─── 滚动统计 ──────────────────────────────────────────
    def get_rolling_accuracy(
        self, window_days: int = ROLLING_WINDOW_DAYS
    ) -> Dict[str, Any]:
        """
        计算最近 N 日的滚动准确率。

        Returns:
            {
                "accuracy_pct": 55.0,
                "correct": 11,
                "total_tracked": 20,
                "days_with_data": 7,
                "avg_return_pct": 0.85,
                "best_day": {"date": "2026-05-25", "accuracy_pct": 100.0},
                "worst_day": {"date": "2026-05-22", "accuracy_pct": 0.0},
            }
        """
        verifications = self._load_recent_verifications(window_days)
        if not verifications:
            return {}

        total_correct = 0
        total_tracked = 0
        total_return = 0.0
        days_with_data = 0
        best_day: Optional[Dict] = None
        worst_day: Optional[Dict] = None

        for v in verifications:
            c = v.get("correct", 0)
            t = v.get("total", 0)
            if t <= 0:
                continue
            days_with_data += 1
            total_correct += c
            total_tracked += t
            total_return += v.get("avg_return_pct", 0) * t

            day_acc = c / t * 100 if t > 0 else 0
            if best_day is None or day_acc > best_day.get("accuracy_pct", 0):
                best_day = {"date": v.get("date"), "accuracy_pct": day_acc}
            if worst_day is None or day_acc < worst_day.get("accuracy_pct", 100):
                worst_day = {"date": v.get("date"), "accuracy_pct": day_acc}

        if total_tracked == 0:
            return {}

        result = {
            "accuracy_pct": round(total_correct / total_tracked * 100, 1),
            "correct": total_correct,
            "total_tracked": total_tracked,
            "days_with_data": days_with_data,
            "avg_return_pct": round(total_return / total_tracked, 2),
        }
        if best_day:
            result["best_day"] = best_day
        if worst_day:
            result["worst_day"] = worst_day

        return result

    # ─── 内部方法 ──────────────────────────────────────────
    def _save_verification(self, date_str: str, report: Dict[str, Any]) -> None:
        """存储验证结果"""
        try:
            file_path = self._store_dir / f"{date_str}_verified.json"
            file_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[AccuracyTracker] 验证结果存储失败: {e}")

    def _load_recent_verifications(
        self, window_days: int
    ) -> List[Dict[str, Any]]:
        """加载最近 N 天的验证结果"""
        results = []
        today = datetime.now()
        for i in range(1, window_days + 1):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            file_path = self._store_dir / f"{date}_verified.json"
            if file_path.exists():
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    results.append(data)
                except Exception:
                    continue
        return results

    def _get_data_router(self) -> Any:
        """懒加载行情数据源 (2026-11-03: 走进程级共享单例).

        原实现每次调用都 new MarketDataRouter() —— 每个实例新建一条 Futu LV3 连接
        + 30s 健康守护线程 + 三个数据源 (各自 requests.Session)。实测一个交易时段
        13 次 "FutuLV3 正在连接 OpenD"、17 次 router 初始化, 全部来自这类旁路构造。
        """
        try:
            from app.data.market_data_router import get_market_data_router
            return get_market_data_router(
                twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
                finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
                alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
                futu_opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
                futu_opend_port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
                enable_futu_lv3=True,
            )
        except Exception as e:
            logger.debug(f"[AccuracyTracker] MarketDataRouter 初始化失败: {e}")
            return None

    @staticmethod
    def _parse_entry_mid(entry_zone: Any) -> Optional[float]:
        """解析入场区间中位价"""
        if isinstance(entry_zone, str) and "-" in entry_zone:
            parts = entry_zone.split("-", 1)
            try:
                lo = float(parts[0].strip())
                hi = float(parts[1].strip())
                return (lo + hi) / 2.0
            except (ValueError, TypeError):
                return None
        if isinstance(entry_zone, (list, tuple)) and len(entry_zone) >= 2:
            try:
                return (float(entry_zone[0]) + float(entry_zone[1])) / 2.0
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _safe_float(v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            f = float(v)
            return f if f == f else None  # NaN check
        except (ValueError, TypeError):
            return None
