#!/usr/bin/env python3
"""FTSeek 每日告警汇总报告 (Daily Alert Summary Report)

运行时间:
  - 港股: 收盘后 1 小时 (HKT 17:00 → CST 17:00)
  - 美股: 收盘后 1 小时 (ET 17:00 → CST 次日 05:00)

功能:
1. 汇总当日所有告警事件 (从 debouncer + 日志分析)
2. 生成 Markdown 格式报告
3. 同时推送到钉钉 + 飞书

Karpathy 原则:
- 纯读取, 不修改任何数据
- 独立脚本, 不修改热路径
- 失败不影响主系统 (bare except 保护)
"""

import os
import sys
import re
import json
import sqlite3
import requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Any
from dotenv import load_dotenv

# ================== 路径配置 ==================
US_ROOT = Path("/opt/ftseek/FTSeek-US")
HK_ROOT = Path("/opt/ftseek/FTSeek")

# 加载 US 环境变量 (DINGTALK/FEISHU webhooks)
load_dotenv(US_ROOT / ".env.us")

sys.path.insert(0, str(US_ROOT))  # US first so app.observability resolves correctly
sys.path.insert(1, str(HK_ROOT))   # HK second (fallback for shared modules)

# ================== 日志解析 ==================

def parse_log_alerts(log_path: str, market: str, since: datetime) -> List[Dict]:
    """从日志文件中提取告警类事件

    匹配的日志模式:
    - [告警观察] / [告警升级]
    - ERROR / WARNING / CRITICAL
    - 熔断 / 拦截 / 拒绝 / 失败
    """
    alerts = []
    try:
        if not os.path.exists(log_path):
            return alerts

        with open(log_path, "r", errors="ignore") as f:
            for line in f:
                # 只取目标日期之后的行
                try:
                    ts_str = line[:19]
                    line_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if line_time < since:
                        continue
                except (ValueError, IndexError):
                    continue

                # 提取告警
                level = "INFO"
                if "ERROR" in line or "CRITICAL" in line or "异常" in line:
                    level = "ERROR"
                elif "WARN" in line or "告警" in line or "拦截" in line or "拒绝" in line:
                    level = "WARN"
                elif "熔断" in line or "失败" in line:
                    level = "WARN"
                else:
                    continue

                # 截断过长行
                content = line.strip()
                if len(content) > 200:
                    content = content[:200] + "..."

                alerts.append({
                    "market": market,
                    "time": ts_str,
                    "level": level,
                    "content": content,
                })
    except Exception as e:
        print(f"[汇总报告] 解析 {log_path} 失败: {e}")

    return alerts


# ================== 交易数据汇总 ==================

def get_us_trade_summary(trade_date: str) -> Dict:
    """获取美股当日交易数据"""
    summary = {"trades": 0, "pnl": 0.0, "positions": 0, "errors": 0}
    try:
        db_path = US_ROOT / "data" / "ftseek_us.db"
        if not db_path.exists():
            return summary
        conn = sqlite3.connect(str(db_path))
        # 交易记录
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0) FROM trade_records WHERE trade_date = ?",
            (trade_date,)
        ).fetchone()
        summary["trades"] = row[0]
        summary["pnl"] = float(row[1])
        # 当前持仓
        row2 = conn.execute("SELECT COUNT(*) FROM open_positions WHERE is_filled = 1").fetchone()
        summary["positions"] = row2[0]
        conn.close()
    except Exception as e:
        print(f"[汇总报告] US 交易数据获取失败: {e}")
    return summary


def get_hk_trade_summary(trade_date: str) -> Dict:
    """获取港股当日交易数据"""
    summary = {"trades": 0, "pnl": 0.0, "positions": 0, "errors": 0}
    try:
        db_path = HK_ROOT / "data" / "ftseek.db"
        if not db_path.exists():
            return summary
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl_pct), 0) FROM signal_outcomes "
            "WHERE timestamp LIKE ?",
            (f"{trade_date}%",)
        ).fetchone()
        summary["trades"] = row[0]
        summary["pnl"] = float(row[1])
        row2 = conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'").fetchone()
        summary["positions"] = row2[0]
        conn.close()
    except Exception as e:
        print(f"[汇总报告] HK 交易数据获取失败: {e}")
    return summary


# ================== 告警防抖器状态 ==================

def get_debouncer_status() -> List[Dict]:
    """获取当前活跃的防抖告警 (内存态, 仅在进程运行时可用)"""
    try:
        from app.observability.alert_debouncer import AlertDebouncer
        # 尝试从运行的进程中获取 (通过共享文件或进程通信)
        # 简化方案: 读取最近日志中的观察记录
        return []
    except Exception:
        return []


# ================== 报告生成 ==================

def build_summary_report(
    us_alerts: List[Dict],
    hk_alerts: List[Dict],
    us_summary: Dict,
    hk_summary: Dict,
    report_date: str,
) -> str:
    """构建 Markdown 格式汇总报告"""

    # 统计
    us_error = sum(1 for a in us_alerts if a["level"] == "ERROR")
    us_warn = sum(1 for a in us_alerts if a["level"] == "WARN")
    hk_error = sum(1 for a in hk_alerts if a["level"] == "ERROR")
    hk_warn = sum(1 for a in hk_alerts if a["level"] == "WARN")

    lines = [
        f"## 📊 FTSeek 每日告警汇总 — {report_date}",
        "",
        "---",
        "",
        "### 🇺🇸 美股 (FTSeek-US)",
        "",
        f"- **交易**: {us_summary['trades']} 笔 | 盈亏: ${us_summary['pnl']:+.2f}",
        f"- **持仓**: {us_summary['positions']} 只",
        f"- **告警**: {us_error} 错误 + {us_warn} 警告 (共 {len(us_alerts)} 条)",
    ]

    # 美股 Top 5 告警
    if us_alerts:
        lines.append("")
        lines.append("**最近告警**:")
        for a in us_alerts[-5:]:
            emoji = "🔴" if a["level"] == "ERROR" else "🟡"
            lines.append(f"- {emoji} {a['time']} [{a['level']}] {a['content'][50:]}")

    lines += [
        "",
        "---",
        "",
        "### 🇭🇰 港股 (FTSeek)",
        "",
        f"- **交易**: {hk_summary['trades']} 笔 | 盈亏: {hk_summary['pnl']:+.2f}%",
        f"- **持仓**: {hk_summary['positions']} 只",
        f"- **告警**: {hk_error} 错误 + {hk_warn} 警告 (共 {len(hk_alerts)} 条)",
    ]

    # 港股 Top 5 告警
    if hk_alerts:
        lines.append("")
        lines.append("**最近告警**:")
        for a in hk_alerts[-5:]:
            emoji = "🔴" if a["level"] == "ERROR" else "🟡"
            lines.append(f"- {emoji} {a['time']} [{a['level']}] {a['content'][50:]}")

    # 总结
    total_errors = us_error + hk_error
    total_warns = us_warn + hk_warn
    lines += [
        "",
        "---",
        "",
        "### 📋 总结",
        "",
        f"- 总告警: {total_errors} 错误 + {total_warns} 警告",
    ]

    if total_errors == 0 and total_warns == 0:
        lines.append("- ✅ 当日系统运行正常，无异常告警")
    elif total_errors > 5:
        lines.append("- 🔴 告警较多，建议排查")
    else:
        lines.append("- 🟡 有少量告警，已观察")

    return "\n".join(lines)


# ================== 推送 ==================

def push_to_dingtalk(title: str, content: str) -> bool:
    """推送到钉钉"""
    try:
        webhook = os.getenv("DINGTALK_WEBHOOK_URL_US", "") or os.getenv("DINGTALK_WEBHOOK", "")
        secret = os.getenv("DINGTALK_SECRET_US", "") or os.getenv("DINGTALK_SECRET", "")
        if not webhook:
            print("[汇总报告] 钉钉 webhook 未配置")
            return False

        from app.observability.dingtalk_alert import DingTalkAlerter
        alerter = DingTalkAlerter(webhook, secret)
        return alerter.send_alert("INFO", title, content)
    except Exception as e:
        print(f"[汇总报告] 钉钉推送失败: {e}")
        return False


def push_to_feishu(title: str, content: str) -> bool:
    """推送到飞书 (使用 Webhook 方式, 支持 Markdown)"""
    try:
        webhook_url = os.getenv("FEISHU_WEBHOOK_URL_US", "")
        secret = os.getenv("FEISHU_APP_SECRET", "")
        if not webhook_url:
            # 回退到 API 方式
            app_id = os.getenv("FEISHU_APP_ID", "")
            app_secret = os.getenv("FEISHU_APP_SECRET", "")
            if not app_id or not app_secret:
                print("[汇总报告] 飞书配置未加载")
                return False
            from app.observability.feishu_alert import FeishuAlerter
            alerter = FeishuAlerter(app_id, app_secret)
            return alerter.send_alert("INFO", title, content)

        # Webhook 方式: 构建 Markdown 消息
        import hashlib
        import base64
        import time
        import hmac as hmac_mod
        from urllib.parse import quote_plus

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content
                    }
                ]
            }
        }

        response = requests.post(webhook_url, json=payload, timeout=10)
        result = response.json()

        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print(f"✅ 飞书推送成功：{title}")
            return True
        else:
            print(f"❌ 飞书推送失败：{result}")
            return False
    except Exception as e:
        print(f"[汇总报告] 飞书推送失败: {e}")
        return False


# ================== 主流程 ==================

def main():
    """每日告警汇总报告主入口"""

    # 确定报告日期 (昨日, 因为盘后 1 小时已经是次日)
    now = datetime.now()
    report_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # 日志扫描起点: 昨日 00:00
    since = datetime.strptime(report_date, "%Y-%m-%d")

    print(f"[汇总报告] 生成 {report_date} 告警汇总...")

    # 1. 解析日志
    us_log = str(US_ROOT / "logs" / "ftseek_us.log")
    hk_log = str(HK_ROOT / "logs" / "ftseek.log")

    us_alerts = parse_log_alerts(us_log, "US", since)
    hk_alerts = parse_log_alerts(hk_log, "HK", since)

    print(f"  US: {len(us_alerts)} 条告警, HK: {len(hk_alerts)} 条告警")

    # 2. 交易数据
    us_summary = get_us_trade_summary(report_date)
    hk_summary = get_hk_trade_summary(report_date)

    # 3. 构建报告
    report = build_summary_report(us_alerts, hk_alerts, us_summary, hk_summary, report_date)

    # 4. 推送 (钉钉 + 飞书)
    title = f"📊 FTSeek 每日告警汇总 — {report_date}"

    dt_ok = push_to_dingtalk(title, report)
    fs_ok = push_to_feishu(title, report)

    print(f"  钉钉推送: {'✅' if dt_ok else '❌'}")
    print(f"  飞书推送: {'✅' if fs_ok else '❌'}")

    # 5. 保存到本地
    output_dir = US_ROOT / "data" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"daily_alert_summary_{report_date}.md"
    with open(str(report_path), "w") as f:
        f.write(report)
    print(f"  本地保存: {report_path}")

    return dt_ok or fs_ok


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
