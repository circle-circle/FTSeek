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
from typing import List, Dict, Any, Tuple
from dotenv import load_dotenv

# ================== 路径配置 ==================
US_ROOT = Path("/opt/ftseek/FTSeek-US")
HK_ROOT = Path("/opt/ftseek/FTSeek")

# 加载 US 环境变量 (DINGTALK/FEISHU webhooks)
load_dotenv(US_ROOT / ".env.us")

sys.path.insert(0, str(US_ROOT))  # US first so app.observability resolves correctly
sys.path.insert(1, str(HK_ROOT))   # HK second (fallback for shared modules)

# 时区 SSOT — 统一使用 app.utils.timezone (必须在 sys.path.insert 之后)
from app.utils.timezone import now_et, now_cst, get_last_trade_date

# ================== 交易时段判断 ==================

def _get_edt_offset(dt: datetime) -> int:
    """判断 CST 日期是否处于美国夏令时 (EDT), 返回 ET→CST 偏移小时数
    
    EDT: 3月第二个周日 02:00 → 11月第一个周日 02:00
    EDT 时 ET→CST 偏移 = +4h; EST 时 = +5h
    """
    year = dt.year
    # 3月第二个周日
    d = datetime(year, 3, 8)
    while d.weekday() != 6:
        d += timedelta(days=1)
    dst_start = d  # ~Mar 8-14

    # 11月第一个周日
    d = datetime(year, 11, 1)
    while d.weekday() != 6:
        d += timedelta(days=1)
    dst_end = d  # ~Nov 1-7

    if dst_start <= dt < dst_end:
        return 4  # EDT: ET + 4 = CST
    return 5  # EST: ET + 5 = CST


def is_us_trading_hours(dt: datetime) -> bool:
    """判断 CST (UTC+8) 时间是否处于美股交易时段(从中国视角)
    
    美股交易会话在中国时间跨越午夜:
    - EDT (夏令时): 21:00-05:00 CST (盘前→收盘后)
    - EST (冬令时): 22:00-06:00 CST
    
    逻辑: 检查 CST 小时是否落在当日 evening 或次日 early morning 的交易窗口内。
    周末以 ET 交易日为准: Mon-Fri evening → 交易; Sat-Sun → 不交易。
    """
    offset = _get_edt_offset(dt)
    
    # 交易窗口: 当日 evening 开始, 次日 early morning 结束
    # EDT (offset=4): evening_start=21, morning_end=5  (04:05 ET + 12h CST buffer)
    # EST (offset=5): evening_start=22, morning_end=6  (04:05 ET + 13h CST buffer)
    evening_start = offset + 17   # EDT: 21, EST: 22
    morning_end = 1 + offset      # EDT: 5, EST: 6
    
    h = dt.hour
    wd = dt.weekday()  # Mon=0 ... Sun=6
    
    # Evening 部分 (21:00+ or 22:00+): 交易日是当天 (Mon-Fri)
    if h >= evening_start:
        if wd < 5:  # Mon-Fri evening
            return True
        return False  # Sat/Sun evening → 不交易
    
    # Early morning 部分 (00:00-05:00 or 00:00-06:00): 交易日是前一天
    if h < morning_end:
        if wd == 0:  # Monday morning → Sunday evening → 不交易
            return False
        if wd <= 5:  # Tue-Sat morning → Mon-Fri evening → 交易
            return True
        return False  # Sunday morning → Saturday evening → 不交易
    
    # 其他时间 (06:00-21:00) → 非交易
    return False


def is_hk_trading_hours(dt: datetime) -> bool:
    """判断 CST (UTC+8) 时间是否处于港股交易时段
    
    港股盘中: 09:00-12:00, 13:00-16:00 HKT (= CST)
    周末不交易。
    """
    if dt.weekday() >= 5:
        return False
    h = dt.hour
    if (9 <= h < 12) or (13 <= h < 16):
        return True
    return False


# ================== 噪音过滤 ==================

# 非交易时间的预期性消息 — 不算真正告警
_NOISE_PATTERNS = [
    "初始化",           # 启动消息
    "订阅完成",         # 正常订阅
    "退订完成",         # 正常退订
    "K 线数据不足 (0 条)",  # 非交易时间 K 线为空是预期
    "K线数据不足 (0 条)",   # 同上 (无空格版本)
    "LV3 K 线数据不足 (0 条)",
    "预热",             # 预热阶段
    "无 Watchlist",     # 空 watchlist
]


def _is_noise_alert(content: str, market: str, dt: datetime) -> bool:
    """判断是否为噪音告警 (非交易时间的预期消息)"""
    # 非交易时间: 过滤预期性消息
    if market == "US" and not is_us_trading_hours(dt):
        for pat in _NOISE_PATTERNS:
            if pat in content:
                return True
    elif market == "HK" and not is_hk_trading_hours(dt):
        for pat in _NOISE_PATTERNS:
            if pat in content:
                return True
    return False


# ================== 日志解析 ==================

def parse_log_alerts(log_path: str, market: str, since: datetime, until: datetime = None) -> List[Dict]:
    """从日志文件中提取告警类事件

    匹配的日志模式:
    - ERROR / WARNING / CRITICAL
    - 熔断 / 拦截 / 拒绝 / 失败
    
    改进:
    - 支持 until 截止边界, 防止跨日污染
    - 非交易时间的噪音消息自动过滤
    """
    alerts = []
    try:
        if not os.path.exists(log_path):
            return alerts

        with open(log_path, "r", errors="ignore") as f:
            for line in f:
                # 只取目标日期范围内的行
                try:
                    ts_str = line[:19]
                    line_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if line_time < since:
                        continue
                    if until and line_time >= until:
                        break  # 超过截止时间, 日志是按时间排序的, 可以提前退出
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

                # 过滤非交易时间的噪音
                if _is_noise_alert(content, market, line_time):
                    continue

                alerts.append({
                    "market": market,
                    "time": ts_str,
                    "level": level,
                    "content": content,
                    "is_trading_hours": (
                        is_us_trading_hours(line_time) if market == "US"
                        else is_hk_trading_hours(line_time)
                    ),
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
        # 2026-08-27 出场日记账修复 (同 AccountCircuitBreaker 口径): 原
        # WHERE trade_date = ? 按入场日归集 — 隔夜仓当日出场 (含跳空亏损) 记在
        # 入场日, 当日盈亏失真 (8/27 实证: 报 +332.78 vs 出场日净 +119.37,
        # 隔夜 UBER -307/ORCL +91/HOOD +3 三笔全部漏记)。exit_time 全库 ET ISO,
        # substr 前 10 位 = 出场 ET 日期。净口径 2026-08-20 用户裁"切" (pnl_net 优先)。
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) "
            "FROM trade_records "
            "WHERE substr(exit_time, 1, 10) = ? AND exit_price > 0",
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


# ================== 告警去重 ==================

def _dedup_alerts(alerts: List[Dict]) -> List[Dict]:
    """将告警按归一化内容去重, 返回唯一告警 + 出现次数 + 首次时间
    
    归一化策略: 去掉时间戳和具体数值, 保留告警模板特征。
    例: "🚫 [仓位扫描] US.SNDK 占比 40.5% 超限..." 和 
         "🚫 [仓位扫描] US.SNDK 占比 38.2% 超限..." → 合并为一条
    """
    import re as _re
    
    def _normalize(content: str) -> str:
        """提取告警模板: 去掉百分比、金额、股数等变化数值"""
        s = content[50:] if len(content) > 50 else content  # 和展示逻辑一致截取
        # 替换数字+单位 (40.5%, $12,769, 7/20.0股) 为占位符
        s = _re.sub(r'\d+\.?\d*%', 'N%', s)
        s = _re.sub(r'\$[\d,]+\.?\d*', '$N', s)
        s = _re.sub(r'\d+/\d+\.?\d*股', 'N/N股', s)
        return s
    
    groups = {}  # normalized_key -> {level, content, first_time, count}
    for a in alerts:
        key = (a["level"], _normalize(a["content"]))
        if key not in groups:
            groups[key] = {
                "level": a["level"],
                "content": a["content"],
                "first_time": a["time"],
                "count": 1,
            }
        else:
            groups[key]["count"] += 1
    
    # 按出现次数降序
    result = sorted(groups.values(), key=lambda x: x["count"], reverse=True)
    return result


# ================== 报告生成 ==================

def build_summary_report(
    us_alerts: List[Dict],
    hk_alerts: List[Dict],
    us_summary: Dict,
    hk_summary: Dict,
    report_date: str,
) -> str:
    """构建 Markdown 格式汇总报告
    
    区分盘中/非盘中告警, 非交易时段告警默认静默(仅展示盘中告警)。
    """

    # 盘中告警 vs 非盘中
    us_trading = [a for a in us_alerts if a.get("is_trading_hours")]
    us_offhours = [a for a in us_alerts if not a.get("is_trading_hours")]
    hk_trading = [a for a in hk_alerts if a.get("is_trading_hours")]
    hk_offhours = [a for a in hk_alerts if not a.get("is_trading_hours")]

    # 统计 (仅计盘中告警)
    us_error = sum(1 for a in us_trading if a["level"] == "ERROR")
    us_warn = sum(1 for a in us_trading if a["level"] == "WARN")
    hk_error = sum(1 for a in hk_trading if a["level"] == "ERROR")
    hk_warn = sum(1 for a in hk_trading if a["level"] == "WARN")

    # 非盘中被过滤的数量
    us_offhours_count = len(us_offhours)
    hk_offhours_count = len(hk_offhours)

    lines = [
        f"## 📊 FTSeek 每日告警汇总 — {report_date}",
        "",
        "---",
        "",
        "### 🇺🇸 美股 (FTSeek-US)",
        "",
        f"- **交易**: {us_summary['trades']} 笔 | 盈亏: ${us_summary['pnl']:+.2f}",
        f"- **持仓**: {us_summary['positions']} 只",
        f"- **盘中告警**: {us_error} 错误 + {us_warn} 警告 (共 {len(us_trading)} 条)",
    ]

    if us_offhours_count > 0:
        lines.append(f"- 🔇 非盘中静默: {us_offhours_count} 条 (已过滤)")

    # 美股盘中告警去重: 按内容归并, 展示唯一告警 + 出现次数
    if us_trading:
        lines.append("")
        lines.append("**盘中告警**:")
        _deduped = _dedup_alerts(us_trading)
        for item in _deduped[:8]:  # Top 8 unique
            emoji = "🔴" if item["level"] == "ERROR" else "🟡"
            count_suffix = f" (×{item['count']})" if item["count"] > 1 else ""
            lines.append(f"- {emoji} {item['first_time']} [{item['level']}] {item['content'][50:]}{count_suffix}")

    lines += [
        "",
        "---",
        "",
        "### 🇭🇰 港股 (FTSeek)",
        "",
        f"- **交易**: {hk_summary['trades']} 笔 | 盈亏: {hk_summary['pnl']:+.2f}%",
        f"- **持仓**: {hk_summary['positions']} 只",
        f"- **盘中告警**: {hk_error} 错误 + {hk_warn} 警告 (共 {len(hk_trading)} 条)",
    ]

    if hk_offhours_count > 0:
        lines.append(f"- 🔇 非盘中静默: {hk_offhours_count} 条 (已过滤)")

    # 港股盘中告警去重
    if hk_trading:
        lines.append("")
        lines.append("**盘中告警**:")
        _deduped = _dedup_alerts(hk_trading)
        for item in _deduped[:8]:
            emoji = "🔴" if item["level"] == "ERROR" else "🟡"
            count_suffix = f" (×{item['count']})" if item["count"] > 1 else ""
            lines.append(f"- {emoji} {item['first_time']} [{item['level']}] {item['content'][50:]}{count_suffix}")

    # 总结
    total_errors = us_error + hk_error
    total_warns = us_warn + hk_warn
    total_offhours = us_offhours_count + hk_offhours_count
    lines += [
        "",
        "---",
        "",
        "### 📋 总结",
        "",
        f"- 盘中告警: {total_errors} 错误 + {total_warns} 警告",
    ]

    if total_offhours > 0:
        lines.append(f"- 🔇 非盘中静默: {total_offhours} 条")

    if total_errors == 0 and total_warns == 0:
        lines.append("- ✅ 盘中系统运行正常，无异常告警")
    elif total_errors > 5:
        lines.append("- 🔴 盘中告警较多，建议排查")
    else:
        lines.append("- 🟡 盘中有少量告警，已观察")

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

def _last_us_trade_date(et_now: datetime) -> str:
    """获取最近一个美股交易日 — 代理到 app.utils.timezone.get_last_trade_date"""
    return get_last_trade_date(et_now)


def main():
    """每日告警汇总报告主入口

    时区策略 (SSOT):
    ─────────────────────────────────────────────────────
    | 用途               | 时区基准            | 说明          |
    ├────────────────────┼────────────────────┼───────────────┤
    | 报告标题 & 文件名  | CST (Asia/Shanghai) | 用户日历视角  |
    | 日志扫描范围       | CST                 | 服务器本地时间 |
    | 美股交易数据查询   | ET (美东)           | DB 存储基准    |
    | 港股交易数据查询   | CST                 | DB 存储基准    |
    ─────────────────────────────────────────────────────

    美股交易日跨 CST 午夜:
    - 盘中 21:30 CST (Day N-1) → 04:00 CST (Day N)
    - 报告日期 = CST Day N, 扫描范围 = Day N-1 21:00 → Day N 06:00
    """

    # ── 时区统一: ET 交易日期为主, CST 为辅 ──
    cst_now = now_cst()
    cst_date = cst_now.strftime("%Y-%m-%d")  # CST 日历日期 (仅用于文件名)

    et_now = now_et()
    us_trade_date = _last_us_trade_date(et_now)  # ET 最后交易日 (DB 查询 + 报告标题)

    # 港股: 5AM CST 港股尚未开盘, 查上一个港股交易日
    hk_dt = cst_now - timedelta(days=1)
    while hk_dt.weekday() >= 5:  # 跳过周末
        hk_dt -= timedelta(days=1)
    hk_trade_date = hk_dt.strftime("%Y-%m-%d")

    # 报告标题用 US 交易日期 (核心数据源)
    report_date = us_trade_date

    # 日志扫描范围: 覆盖美股跨午夜的交易会话
    # since = US交易日前一天 21:00 CST (覆盖盘前)
    # until = US交易日 06:00 CST (覆盖盘后收尾)
    # US交易日对应的CST日期 = ET日期 + 1天 (因为跨午夜)
    report_dt = datetime.strptime(report_date, "%Y-%m-%d")
    cst_trade_day = report_dt + timedelta(days=1)  # ET周一交易 → CST周二凌晨
    since = report_dt.replace(hour=21, minute=0)  # US交易日 21:00 CST (盘前)
    until = cst_trade_day.replace(hour=6, minute=0)  # US交易日次日 06:00 CST (盘后收尾)

    print(f"[汇总报告] 生成 {report_date} 告警汇总...")
    print(f"  时区: CST={cst_now.strftime('%H:%M')} | ET={et_now.strftime('%H:%M')}")
    print(f"  US 交易日: {us_trade_date} | HK 交易日: {hk_trade_date}")
    print(f"  扫描范围: {since} → {until}")

    # 1. 解析日志 (带截止边界 + 噪音过滤)
    us_log = str(US_ROOT / "logs" / "ftseek_us.log")
    hk_log = str(HK_ROOT / "logs" / "ftseek.log")

    us_alerts = parse_log_alerts(us_log, "US", since, until)
    hk_alerts = parse_log_alerts(hk_log, "HK", since, until)

    # 分类统计: 盘中 vs 非盘中
    us_trading = [a for a in us_alerts if a.get("is_trading_hours")]
    us_offhours = [a for a in us_alerts if not a.get("is_trading_hours")]
    hk_trading = [a for a in hk_alerts if a.get("is_trading_hours")]
    hk_offhours = [a for a in hk_alerts if not a.get("is_trading_hours")]

    print(f"  US: {len(us_alerts)} 条 (盘中 {len(us_trading)}, 非盘中 {len(us_offhours)})")
    print(f"  HK: {len(hk_alerts)} 条 (盘中 {len(hk_trading)}, 非盘中 {len(hk_offhours)})")

    # 2. 交易数据 (US 用 ET 交易日, HK 用 CST 日期)
    us_summary = get_us_trade_summary(us_trade_date)
    hk_summary = get_hk_trade_summary(hk_trade_date)

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
