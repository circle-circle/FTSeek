"""
FTSeek 新闻管线 V2 — 数据收集器 (混合模式)

记录 V2 管线处理结果，与现有系统对比分析。
存储: ~/.hermes/ftseek/data/v2_collection/YYYY-MM-DD.json
"""

import json
import os
import time
import logging
from datetime import datetime, date
from typing import Dict, List

logger = logging.getLogger("ftseek.news_v2.collector")


class V2DataCollector:
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or os.path.expanduser("~/.hermes/ftseek/data/v2_collection")
        os.makedirs(self.data_dir, exist_ok=True)
        self._today = date.today().isoformat()
        self._stats = {
            "total_received": 0,
            "total_passed": 0,
            "total_rejected": 0,
            "geo_events": 0,
            "immediate_news": 0,
            "watchlist_matches": 0,
            "rejected_by_reason": {},
            "processing_times_ms": [],
            "errors": 0,
            "start_time": time.time(),
        }
        self._today_items = []
        self._load_today_data()

    def _today_file(self) -> str:
        return os.path.join(self.data_dir, f"{self._today}.json")

    def _load_today_data(self):
        path = self._today_file()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self._stats = data.get("stats", self._stats)
                self._today_items = data.get("items", [])
            except Exception:
                pass

    def record_received(self, item_dict: Dict, passed: bool, reason: str = ""):
        self._stats["total_received"] += 1
        if passed:
            self._stats["total_passed"] += 1
            if item_dict.get("geo_scan", {}).get("is_geo_event"):
                self._stats["geo_events"] += 1
            if item_dict.get("urgency") in ("IMMEDIATE", "HIGH"):
                self._stats["immediate_news"] += 1
            if item_dict.get("related_stocks"):
                self._stats["watchlist_matches"] += len(item_dict["related_stocks"])
        else:
            self._stats["total_rejected"] += 1
            self._stats["rejected_by_reason"][reason] = self._stats["rejected_by_reason"].get(reason, 0) + 1

        self._today_items.append({
            "timestamp": item_dict.get("timestamp", time.time()),
            "source": item_dict.get("source_name", ""),
            "title": item_dict.get("title", "")[:100],
            "score": item_dict.get("raw_score", 0),
            "urgency": item_dict.get("urgency", "NORMAL"),
            "passed": passed,
            "reason": reason,
            "markets": item_dict.get("market_tags", []),
            "stocks": item_dict.get("related_stocks", [])[:5],
        })
        if len(self._today_items) % 50 == 0:
            self._save()

    def record_processing_time(self, elapsed_ms: float):
        self._stats["processing_times_ms"].append(elapsed_ms)

    def record_error(self, error: str):
        self._stats["errors"] += 1

    def _save(self):
        data = {"date": self._today, "stats": self._stats, "items": self._today_items}
        path = self._today_file()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.error("[V2 Collector] 保存失败: %s", e)

    def get_daily_report(self) -> str:
        s = self._stats
        total = s["total_received"]
        passed = s["total_passed"]
        rejected = s["total_rejected"]
        avg_time = sum(s["processing_times_ms"]) / max(1, len(s["processing_times_ms"]))
        rejected_sorted = sorted(s["rejected_by_reason"].items(), key=lambda x: -x[1])

        lines = [
            f"# FTSeek 新闻管线 V2 — 每日对比报告",
            f"",
            f"**日期**: {self._today}  |  **模式**: 混合模式",
            f"",
            f"## 核心指标",
            f"",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 总接收 | {total} 条 |",
            f"| 通过 | {passed} 条 ({passed/max(1,total)*100:.0f}%) |",
            f"| 拒绝 | {rejected} 条 ({rejected/max(1,total)*100:.0f}%) |",
            f"| 地缘事件 | {s['geo_events']} 条 |",
            f"| 紧急新闻 | {s['immediate_news']} 条 |",
            f"| Watchlist 匹配 | {s['watchlist_matches']} 次 |",
            f"| 平均处理时间 | {avg_time:.0f}ms |",
            f"| 错误数 | {s['errors']} 次 |",
            f"",
            f"## 拒绝原因",
            f"",
            f"| 原因 | 数量 |",
            f"|------|------|",
        ]
        for reason, count in rejected_sorted:
            lines.append(f"| {reason} | {count} |")

        passed_items = [i for i in self._today_items if i["passed"]]
        if passed_items:
            lines.append(f"")
            lines.append(f"## 通过新闻 (最近 {min(15, len(passed_items))} 条)")
            lines.append(f"")
            for i, item in enumerate(passed_items[-15:], 1):
                ts = datetime.fromtimestamp(item["timestamp"]).strftime("%H:%M")
                stocks = ", ".join(item["stocks"][:3]) if item["stocks"] else "无"
                lines.append(f"{i:2d}. [{ts}] [{item['urgency']}] {item['source']}: {item['title'][:50]}")
                lines.append(f"    股票: {stocks} | 分数: {item['score']:.2f}")

        lines.append(f"")
        lines.append(f"## 建议")
        lines.append(f"")
        eff = passed / max(1, total) * 100
        if eff >= 30:
            lines.append(f"- ✅ 有效新闻占比 {eff:.0f}% 达标 (>=30%)")
        else:
            lines.append(f"- ⚠️ 有效新闻占比 {eff:.0f}% 低于目标，建议扩大 watchlist 或增加数据源")
        if s["geo_events"] > 0:
            lines.append(f"- 🌍 检测 {s['geo_events']} 个地缘事件")
        if s["errors"] > 0:
            lines.append(f"- ❌ {s['errors']} 次错误需排查")

        return "\n".join(lines)

    def save_report(self) -> str:
        report = self.get_daily_report()
        path = os.path.join(self.data_dir, f"report_{self._today}.md")
        with open(path, "w") as f:
            f.write(report)
        return path

    def flush(self):
        self._save()
