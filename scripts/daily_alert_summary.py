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


# ================== 日志轮转档覆盖 (2026-09-09) ==================

def resolve_log_sources(log_path: str, since: datetime, until: datetime) -> List[str]:
    """返回覆盖 [since, until) 的日志文件序列, 按时间升序 (轮转档在前, 当前档最后)。

    根因: /etc/logrotate.d/ftseek-us 用 daily + copytruncate + dateext(-%Y%m%d),
    轮转由 /etc/cron.daily/logrotate 触发, 近 7 个交易日实测落在 **03:12-03:48 CST**
    —— 正好在美股会话 (21:30-04:00 CST) 内部 ⇒ 只读当前档 logs/ftseek_us.log 会丢掉
    会话前 ~5.5 小时。实证: 9/8 chase_cap拦截 轮转档 145 条 / 当前档 3 条, 汇总报
    「0 错误 + 6 警告」= 真相的 4%; 9/4 同病 (轮转档 142 条 WARNING|ERROR / 报告 2 条,
    且两条时间戳都是轮转后的 03:52)。

    copytruncate 语义: 轮转档 = 截断前的副本(内容至轮转时刻), 当前档 = 截断后继续写,
    两档内容不重叠 ⇒ 按日期升序拼接即完整时间线。dateext 用**轮转当日**日期命名
    (实测 ftseek_us.log-20260909 首行 2026-09-08 12:00 / 末行 2026-09-09 03:48),
    故候选日期取 since.date()..until.date() 全覆盖, 窗口外的档由 parse 的时间戳过滤兜掉。
    未轮转时 (cron.daily 晚于本脚本, 如 9/6-9/7 的 06:28/06:44) 候选档不存在 ⇒ 只返回当前档。
    """
    p = Path(log_path)
    sources: List[str] = []
    day = since.date()
    while day <= until.date():
        stamp = day.strftime("%Y%m%d")
        for cand in (p.parent / f"{p.name}-{stamp}.gz", p.parent / f"{p.name}-{stamp}"):
            if cand.exists():
                sources.append(str(cand))
                break
        day += timedelta(days=1)
    if p.exists():
        sources.append(str(p))
    return sources


def parse_log_alerts_window(log_path: str, market: str, since: datetime,
                            until: datetime = None) -> Tuple[List[Dict], List[str]]:
    """跨轮转档解析一个会话窗的告警; 返回 (告警列表, 覆盖诊断行)。

    只读, 不改 parse_log_alerts 的判级/噪音过滤逻辑 (单一真源, 逐档复用)。
    跨档按 (time, level, content) 去重: copytruncate 本身不重叠, 此举只防
    「本脚本运行途中正好轮转」的读写竞态把同一行计两次。
    覆盖诊断行随 stdout 落盘 ⇒ 盲区若复发 (只剩当前档 / 覆盖起点远晚于 since)
    在读数上直接可见, 不靠人事后对账。
    同一批诊断行还由 main() 传进 build_summary_report(log_coverage=…) 渲染到
    **推送正文的总结区** (紧邻「✅ 盘中系统运行正常」那句) —— 只进 stdout 的话,
    读者看到的推送正文里那句「无异常告警」仍可能在覆盖不全时说谎 (9/8 实证)。
    纯观测行, 不进任何判据、不改判级。
    """
    sources = resolve_log_sources(log_path, since, until or (since + timedelta(days=1)))
    alerts: List[Dict] = []
    seen = set()
    per_source: List[str] = []
    for src in sources:
        got = parse_log_alerts(src, market, since, until)
        added = 0
        for a in got:
            key = (a["time"], a["level"], a["content"])
            if key in seen:
                continue
            seen.add(key)
            alerts.append(a)
            added += 1
        per_source.append(f"{Path(src).name}={added}")
    alerts.sort(key=lambda a: a["time"])
    span = f"{alerts[0]['time'][11:19]}→{alerts[-1]['time'][11:19]}" if alerts else "无告警"
    diag = (f"{market} 日志源 {len(sources)} 个 ({', '.join(per_source) or '无'}) "
            f"| 告警跨度 {span} | 窗 {since.strftime('%m-%d %H:%M')}→"
            f"{(until or since).strftime('%m-%d %H:%M')}")
    return alerts, [diag]


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
        # 隔夜 UBER -307/ORCL +91/HOOD +3 三笔全部漏记)。净口径 2026-08-20 用户裁"切"
        # (pnl_net 优先)。substr(exit_time,1,10)=出场 ET 日的前提仅对现役窗口成立
        # (调用方传 ET 最后交易日), 遗留 naive/+00:00 行分布见
        # account_circuit_breaker._get_daily_pnl 注释。
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


# ================== 账户权益对账 (观测层, 不进任何判据) ==================
# 背景 (2026-08-29 用户拷问"富途显示今日盈亏5百多, 与你统计 −338.76 有出入"):
#   日报值是"机器人当日已了结净额", 富途 App 是"账户全口径" — 后者含未平仓仓的当日
#   市值变动, 且富途模拟盘的 pl_val/today_pl_val 是**毛额** (NTES 富途 6.24 == 我方毛利,
#   我方净额 1.84)。两数本就不该相等, 但差异必须每晚摊开而不是等人问。
# 桥接恒等式: 权益变动 C ≈ 已了结毛利 G + 未平仓当日市值变动 U
#   残差 R = C − (G + U) 的成分 = 富途实际费用 vs 我方 fees_est 模型 + 成交价 vs 撮合价
#   + U 按**调用时刻**读 (盘后重跑几小时后再跑, 券商侧已是盘后共价 → R 会真变大;
#   调度窗口恰在 16:00 ET, 该项≈0)。R 只在超容差时标 ⚠️ (观测, 不触发任何风控)。
# 关键纪律: 任一输入取不到 → verdict=not_judgeable 并写明原因, 严禁用 0 冒充"已对账"
#   (本日已三次踩到"只能报健康的守卫": journalctl 数 ERROR / /proc mtime / '/ ERROR ' 模式)。
EQUITY_RECON_TOLERANCE_USD = float(os.getenv("EQUITY_RECON_TOLERANCE_USD", "50"))
# 收盘样本容许偏移 15min — 只用作"没样本就报不可判"的守卫, 不是安全边际。近 20 个交易日
# 收盘后首样本 19 个都在 +35s 内 (唯一例外 8/20 重启段 +11min), 所以正常路径下这个宽度
# 不参与任何取值; 真出问题时 (采样断流/重启) 它把"静默拿一个错时刻的样本"换成 not_judgeable。
# 8/28 首次实测残差 $57.06 的来源已定位并修掉: 旧"取最接近 boundary 的样本"选中了收盘前
# 13.5s 的盘中共价 (见 _equity_at), 单项偏差 $40.75。改收盘印后同一交易日残差降到 $4~22
# (随读表时刻浮动 — U 是盘后实时字段: 16:41/16:49/16:59 三次读分别给 −300.17/−288.08/
#  −282.64, 对应残差 21.60/9.51/4.07; 调度恰在 16:00 ET, 取到的是收盘值)。
_EQUITY_SAMPLE_WINDOW_MIN = 15


def _cst_close_boundary(trade_date: str) -> str:
    """ET 收盘 16:00 → 服务器本地 (CST) 时间戳字符串.

    capital_state.last_update 落的是 CST naive ISO (带 'T'), 零填充 ⇒ 字符串序==时间序。
    注: 不可再 +1 天 —— 16:00 加 12/13h 本身就跨过 CST 午夜 (实测 8/28 16:00 ET →
    8/29 04:00 CST); 首版多算一天把边界推到未来, 判据立即退化成"样本缺失"。
    """
    et_dt = datetime.strptime(trade_date, "%Y-%m-%d").replace(hour=16)
    cst_dt = et_dt + timedelta(hours=8 + _get_edt_offset(et_dt))
    return cst_dt.strftime("%Y-%m-%dT%H:%M:%S")


def _prev_us_trade_date(trade_date: str) -> str:
    """前一交易日 (跳周末; 跳假日内权益不变, 取最近样本即正确前收)."""
    d = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _equity_at(conn, boundary_cst: str):
    """取收盘后第一个权益样本 (收盘印) — boundary 起 +_EQUITY_SAMPLE_WINDOW_MIN 内, 无 → None.

    只取 boundary **之后**的样本: ET 收盘 16:00:00 的收盘价才是当日权益的定价基准, 边界前
    十几秒的样本还是盘中共价。实测 8/28 边界: 03:59:46 → 98377.696 与 04:00:17 → 98336.941
    相差 $40.75 — 旧实现"取最接近 boundary 的样本"会选中边界前那个, 偏差直接灌进残差。
    窗内无样本一律 None (→ not_judgeable), 不做"再往前找个最近的"兜底: 放宽到盘中样本
    量级的漂移远大于费用模型误差, 宁可报不可判也不给一个看着闭合、实则假数拼出来的桥。
    """
    row = conn.execute(
        "SELECT last_update, current_equity FROM capital_state "
        "WHERE last_update >= ? AND last_update <= ? ORDER BY last_update LIMIT 1",
        (
            boundary_cst,
            (datetime.strptime(boundary_cst, "%Y-%m-%dT%H:%M:%S")
             + timedelta(minutes=_EQUITY_SAMPLE_WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M:%S"),
        ),
    ).fetchone()
    return None if row is None else float(row[1])


def _fetch_broker_open_mv_change():
    """只读拉富途持仓, 返回 (未平仓仓当日市值变动合计[毛], None) 或 (None, 不可判原因).

    qty==0 的行是"当日已平"的剩余列表项, 其 today_pl_val 属已实现部分, 已由 trade_records
    侧计入 G ⇒ 这里必须排除, 否则与 G 双计 (桥接会假闭合)。
    账户/环境解析与 scripts/live_reconcile_check.py::_broker_positions 同源 (含 8/11 教训:
    解析错环境 ⇒ 拿真实账户持仓补模拟盘账, 有仓日必假漂移), 故同样走 _read_env_file 实时读
    .env.us 而非 os.environ (cron 无 EnvironmentFile 也能工作), 并同样尊重
    ALLOW_REAL_TRADING 安全闸。今日盈亏 today_pl_val 为富途 App 同源字段 (毛额, 见上注)。
    """
    try:
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket
    except Exception as e:
        return None, f"futu SDK 不可用 ({type(e).__name__})"
    ctx = None
    try:
        from app.core.trading_mode import _read_env_file
        account = _read_env_file("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        if account == "real" and _read_env_file("ALLOW_REAL_TRADING", "").strip().lower() != "true":
            account = "sim"  # 与 di_container/对账脚本一致的安全闸: 强制降级
        trd_env = TrdEnv.REAL if account == "real" else TrdEnv.SIMULATE
        acc_id = int(_read_env_file("REAL_ACCOUNT_ID" if account == "real" else "SIMULATE_ACCOUNT_ID", "0"))
        if not acc_id:
            return None, f"缺 {account} 账户 ID 配置"
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=_read_env_file("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(_read_env_file("FUTU_OPEND_PORT", "11111")),
            is_encrypt=False,
        )
        ret, pos = ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
        if ret != 0:
            return None, f"position_list_query 失败 (acc={acc_id} env={trd_env}): {pos}"
        total = 0.0
        for _, p in pos.iterrows():
            if abs(float(p.get("qty") or 0)) < 1e-9:
                continue  # 当日已平行 → 计入 G, 不在此双计
            v = p.get("today_pl_val")
            try:
                total += float(v)
            except (TypeError, ValueError):
                return None, f"{p.get('code')} today_pl_val 非数值 ({v!r}) → 拒绝部分求和"
        return round(total, 2), None
    except Exception as e:
        return None, f"券商查询异常 {type(e).__name__}: {e}"
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass


def get_us_equity_reconciliation(trade_date: str, db_path: Path = None, broker_fetch=None,
                                 session_date: str = None) -> Dict:
    """桥接当日: 权益变动 vs (已了结毛利 + 未平仓当日波动). 观测用, 失败即声明不可判."""
    out = {"verdict": "not_judgeable", "reason": None, "trade_date": trade_date,
           "realized_net": None, "realized_gross": None, "fees": None,
           "equity_change": None, "open_mv_change": None,
           "residual": None, "within_tolerance": None}
    # 会话守卫: 券商 today_pl_val 只描述"当前会话"的当日波动, 无法回填历史日 —— 拿今天的
    # 未平仓波动去补前天的账, 桥接会"闭合得更好看"而全是假的。回填场景一律不判。
    _sess = session_date or get_last_trade_date(now_et())
    if trade_date != _sess:
        out["reason"] = f"非当前会话 (券商当日波动只对 {_sess} 有效, 请求 {trade_date}) → 不判"
        return out
    db_path = Path(db_path) if db_path else US_ROOT / "data" / "ftseek_us.db"
    fetch = broker_fetch or _fetch_broker_open_mv_change
    conn = None
    try:
        if not db_path.exists():
            out["reason"] = f"库不可读 ({db_path.name})"
            return out
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0), "
            "COALESCE(SUM(realized_pnl), 0), COALESCE(SUM(fees_est), 0) "
            "FROM trade_records WHERE substr(exit_time, 1, 10) = ? AND exit_price > 0",
            (trade_date,),
        ).fetchone()
        gross, fees, net = float(row[2]), float(row[3]), float(row[1])
        out.update(realized_net=round(net, 2), realized_gross=round(gross, 2), fees=round(fees, 2))
        try:
            has_table = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='capital_state'").fetchone()[0]
        except Exception:
            has_table = 0
        if not has_table:
            out["reason"] = "capital_state 表不存在 → 权益侧无载体"
            return out
        # 账户归属守卫: 权益载体带 trade_env 标签 (CapitalSafety._save_state 写入), 券商侧
        # 则按 .env.us 解析账户。AUTO_TRADE_ACCOUNT 一旦翻 real 而库里仍有 sim 历史行,
        # 权益序列就是两个账户混在一起 — C 与 U 不同源, 桥接必然假闭合/假超差 (8/11 对账
        # 拿错账户的同一课)。多于一个 env → 不判, 不去猜哪个是"当前"。
        if conn.execute("SELECT COUNT(DISTINCT COALESCE(trade_env, -1)) FROM capital_state").fetchone()[0] > 1:
            out["reason"] = "capital_state 含多个 trade_env → 权益序列跨账户, 不可归因"
            return out
        # 依赖注: 本对账需要 capital_state 是**时间序列**。capital_safety.py:_save_state 的
        # "INSERT OR REPLACE 保持 1 行/env" P0 注释从未成立 (trade_env 无 UNIQUE, 实测 42k 行),
        # 正因如此才有收盘样本可查。若将来真去实现单行 UPSERT, 必须同时保留历史表, 否则
        # 本桥接静默退化为"样本缺失"。
        today_close = _equity_at(conn, _cst_close_boundary(trade_date))
        prev_close = _equity_at(conn, _cst_close_boundary(_prev_us_trade_date(trade_date)))
        if today_close is None or prev_close is None:
            miss = []
            if prev_close is None:
                # 美股假日后的周一必然走到这里 (实证 7/3 独立日观察日全天仅 86 样本且末条在
                # 22:44 CST, 7/4 仅 16 条 — 休市日采样本就停摆), 明示这是预期而非探针坏了。
                miss.append("前收 (休市/采样停摆时属预期)")
            if today_close is None:
                miss.append("今收")
            out["reason"] = f"权益样本缺失 ({'/'.join(miss)}, 收盘后 {_EQUITY_SAMPLE_WINDOW_MIN}min 窗内无 capital_state)"
            return out
        equity_change = round(today_close - prev_close, 2)
        out["equity_change"] = equity_change
        open_mv, why = fetch()
        if open_mv is None:
            out["reason"] = f"未平仓当日波动不可得 ({why})"
            return out
        out["open_mv_change"] = open_mv
        residual = round(equity_change - (gross + open_mv), 2)
        out.update(residual=residual,
                   within_tolerance=abs(residual) <= EQUITY_RECON_TOLERANCE_USD,
                   verdict="ok")
        return out
    except Exception as e:
        # 检测器自身异常不得读成"已对账/无差异" (§6 规则5 同课)
        out["reason"] = f"对账异常 {type(e).__name__}: {e}"
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _format_equity_recon_line(r: Dict) -> str:
    if r.get("verdict") != "ok":
        return f"- **权益对账**: 不可判 — {r.get('reason')} (不以 0 冒充已对账)"
    flag = "" if r.get("within_tolerance") else f" ⚠️超容差 ${EQUITY_RECON_TOLERANCE_USD:.0f}"
    return (f"- **权益对账** (观测, 不进判据): 账户权益 ${r['equity_change']:+.2f} ≈ "
            f"已了结毛利 ${r['realized_gross']:+.2f} + 未平仓当日 ${r['open_mv_change']:+.2f}"
            f" | 残差 ${r['residual']:+.2f}{flag} (日报净额 ${r['realized_net']:+.2f} = 毛利 − 费用 "
            f"${r['fees']:.2f})")


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
    us_equity_recon: Dict = None,
    log_coverage: List[str] = None,
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

    if us_equity_recon is not None:
        lines.append(_format_equity_recon_line(us_equity_recon))

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

    # 日志覆盖自检 (2026-09-09): 告警计数的可信度前提, 必须与计数同屏。
    # 纯观测, 不参与上面的判级 —— 判级改了就是改判据, 须另立案。
    if log_coverage:
        lines.append("")
        lines.append("**🗂 日志覆盖自检** (logrotate 跨档拼接; 档数=1 且跨度远短于窗口 ⇒ 覆盖不全, 上面的告警计数不可信):")
        for cover_line in log_coverage:
            lines.append(f"- {cover_line}")

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

    # 1. 解析日志 (带截止边界 + 噪音过滤 + 跨轮转档覆盖, 2026-09-09)
    us_log = str(US_ROOT / "logs" / "ftseek_us.log")
    hk_log = str(HK_ROOT / "logs" / "ftseek.log")

    us_alerts, us_cover = parse_log_alerts_window(us_log, "US", since, until)
    hk_alerts, hk_cover = parse_log_alerts_window(hk_log, "HK", since, until)
    for line in us_cover + hk_cover:
        print(f"  [覆盖] {line}")

    # 分类统计: 盘中 vs 非盘中
    us_trading = [a for a in us_alerts if a.get("is_trading_hours")]
    us_offhours = [a for a in us_alerts if not a.get("is_trading_hours")]
    hk_trading = [a for a in hk_alerts if a.get("is_trading_hours")]
    hk_offhours = [a for a in hk_alerts if not a.get("is_trading_hours")]

    print(f"  US: {len(us_alerts)} 条 (盘中 {len(us_trading)}, 非盘中 {len(us_offhours)})")
    print(f"  HK: {len(hk_alerts)} 条 (盘中 {len(hk_trading)}, 非盘中 {len(hk_offhours)})")

    # 2. 交易数据 (US 用 ET 交易日, HK 用 CST 日期)
    us_summary = get_us_trade_summary(us_trade_date)
    us_equity_recon = get_us_equity_reconciliation(us_trade_date)
    hk_summary = get_hk_trade_summary(hk_trade_date)

    # 3. 构建报告
    report = build_summary_report(us_alerts, hk_alerts, us_summary, hk_summary,
                                  report_date, us_equity_recon=us_equity_recon,
                                  log_coverage=us_cover + hk_cover)

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
